#!/usr/bin/env python3
"""Auditoría READ-ONLY de los MENSAJES REALES enviados al grupo de WhatsApp.

Une published_messages (texto real + media) con outbox (payload) y con el
runtime_event diversity_curator_decision correspondiente (por selected_outbox_id).
Calcula diversidad percibida sobre últimas 30/50/100 y métricas de override.

NO modifica nada. Escribe JSON a /tmp y un resumen a stdout.
"""
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import (
    infer_offer_category, normalize_brand, product_family,
    title_fingerprint, title_similarity,
)

DB = "data/ofertas_hunter.db"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/whatsapp_audit.json"
DECISION_KIND = "diversity_curator_decision"


def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c


def load_decisions(c):
    """Mapa selected_outbox_id -> decision payload (la más reciente por outbox)."""
    rows = c.execute(
        "SELECT created_at, payload_json FROM runtime_events WHERE kind=? ORDER BY id ASC",
        (DECISION_KIND,),
    ).fetchall()
    by_outbox = {}
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except Exception:
            continue
        if "candidates_after_hard_caps" not in p:
            continue
        sel = p.get("selected_outbox_id")
        if sel is not None:
            p["_created_at"] = r["created_at"]
            by_outbox[sel] = p
    return by_outbox


def build_feed(c, limit=100):
    decisions = load_decisions(c)
    rows = c.execute(
        """
        SELECT pm.id pm_id, pm.outbox_id, pm.sent_at, pm.message_text, pm.media_url,
               o.type otype, o.message_payload_json, p.title p_title, p.brand p_brand,
               p.url_canonical p_url, f.discount_percent f_disc
        FROM published_messages pm
        LEFT JOIN outbox o ON pm.outbox_id=o.id
        LEFT JOIN offers f ON f.id=pm.offer_id
        LEFT JOIN products p ON p.id=f.product_id
        WHERE pm.success=1
        ORDER BY pm.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    feed = []
    for r in rows:
        try:
            payload = json.loads(r["message_payload_json"]) if r["message_payload_json"] else {}
        except Exception:
            payload = {}
        title = payload.get("title") or r["p_title"] or ""
        mkt = payload.get("marketplace") or "?"
        cat = infer_offer_category(title, payload, mkt, payload.get("source"))
        brand = normalize_brand(payload.get("brand") or r["p_brand"], title)
        fam = product_family(title, payload.get("brand") or r["p_brand"])
        dec = decisions.get(r["outbox_id"], {})
        feed.append({
            "pm_id": r["pm_id"], "outbox_id": r["outbox_id"], "sent_at": r["sent_at"],
            "marketplace": mkt, "source": payload.get("source"),
            "category_normalized": cat.normalized, "category_raw": cat.raw,
            "brand_normalized": brand or "-", "product_family": fam,
            "title": title, "fp": title_fingerprint(title),
            "current_price": payload.get("current_price"),
            "previous_price": payload.get("previous_price"),
            "discount_percent": payload.get("discount_percent") if payload.get("discount_percent") is not None else r["f_disc"],
            "url": payload.get("final_url") or payload.get("url") or payload.get("canonical_url") or r["p_url"],
            "affiliate_url": payload.get("affiliate_url"),
            "item_id": payload.get("item_id") or payload.get("asin"),
            "message_text": (r["message_text"] or "")[:400],
            "llm_used": dec.get("llm_used"),
            "fallback_used": dec.get("fallback_used"),
            "override_used": dec.get("override_used"),
            "override_reason": dec.get("override_reason"),
            "candidates_count": dec.get("candidates_count"),
            "candidates_after_hard_caps": dec.get("candidates_after_hard_caps"),
            "rejected_count": len(dec.get("rejected_candidates") or []),
            "decision_found": bool(dec),
        })
    feed.reverse()  # cronológico ascendente
    return feed


def max_streak(seq):
    best = 0; cur = 0; cur_val = None; best_val = None
    for v in seq:
        cur = cur + 1 if v == cur_val else 1
        cur_val = v
        if cur > best:
            best, best_val = cur, v
    return best, best_val


def rolling_distinct(seq, w):
    vals = []
    for i in range(len(seq)):
        win = seq[max(0, i - w + 1):i + 1]
        vals.append(len(set(win)))
    return round(sum(vals) / len(vals), 2) if vals else 0


def window_metrics(feed, label):
    n = len(feed)
    if n == 0:
        return {"label": label, "n": 0}
    cats = Counter(f["category_normalized"] for f in feed)
    brands = Counter(f["brand_normalized"] for f in feed if f["brand_normalized"] != "-")
    mkts = Counter(f["marketplace"] for f in feed)
    fams = Counter(f["product_family"] for f in feed)
    cs, cv = max_streak([f["category_normalized"] for f in feed])
    bs, bv = max_streak([f["brand_normalized"] for f in feed])
    ms, mv = max_streak([f["marketplace"] for f in feed])
    fs, fv = max_streak([f["product_family"] for f in feed])
    overrides = sum(1 for f in feed if f["override_used"])
    # fuzzy pairs
    fpairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            if len(feed[i]["fp"].split()) >= 3 and len(feed[j]["fp"].split()) >= 3:
                if title_similarity(feed[i]["title"], feed[j]["title"]) >= 0.85:
                    fpairs += 1
    dup_exact = sum(1 for k, v in Counter(f["item_id"] for f in feed if f["item_id"]).items() if v > 1)
    return {
        "label": label, "n": n,
        "by_category_pct": {k: f"{v} ({round(100*v/n)}%)" for k, v in cats.most_common()},
        "by_marketplace_pct": {k: f"{v} ({round(100*v/n)}%)" for k, v in mkts.most_common()},
        "top_brands": dict(brands.most_common(8)),
        "variant_families_repeated": {k: v for k, v in fams.items() if v > 1},
        "max_streak_category": [cs, cv],
        "max_streak_brand": [bs, bv],
        "max_streak_marketplace": [ms, mv],
        "max_streak_family": [fs, fv],
        "rolling5_distinct_categories": rolling_distinct([f["category_normalized"] for f in feed], 5),
        "rolling10_distinct_categories": rolling_distinct([f["category_normalized"] for f in feed], 10),
        "dup_exact": dup_exact,
        "fuzzy_pairs_ge_085": fpairs,
        "overrides": overrides,
        "override_pct": round(100 * overrides / n, 1),
    }


def main():
    c = conn()
    feed100 = build_feed(c, 100)
    out = {
        "generated_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "total_in_feed": len(feed100),
        "metrics_last30": window_metrics(feed100[-30:], "last30"),
        "metrics_last50": window_metrics(feed100[-50:], "last50"),
        "metrics_last100": window_metrics(feed100, "last100"),
        "feed": feed100,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)
    print(f"WROTE {OUT} (feed n={len(feed100)})")
    for m in (out["metrics_last30"], out["metrics_last50"], out["metrics_last100"]):
        print(f"\n--- {m['label']} (n={m['n']}) ---")
        print("  cat:", m["by_category_pct"])
        print("  mkt:", m["by_marketplace_pct"])
        print("  top_brands:", m["top_brands"])
        print("  streak cat:", m["max_streak_category"], "| brand:", m["max_streak_brand"],
              "| family:", m["max_streak_family"], "| mkt:", m["max_streak_marketplace"])
        print("  rolling5_distinct:", m["rolling5_distinct_categories"],
              "| rolling10_distinct:", m["rolling10_distinct_categories"])
        print("  dup_exact:", m["dup_exact"], "| fuzzy>=0.85:", m["fuzzy_pairs_ge_085"],
              "| overrides:", m["overrides"], f"({m['override_pct']}%)")


if __name__ == "__main__":
    main()
