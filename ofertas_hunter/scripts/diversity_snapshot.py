#!/usr/bin/env python3
"""Snapshot horario READ-ONLY de diversidad. Append a JSONL.

Diseñado para correr por cron cada hora durante 24h. NO toca el bot.
Escribe una línea JSON por ejecución a logs/diversity_validation.jsonl con:
- timestamp del snapshot
- total publicaciones post-fix
- distribución de categorías/marcas/marketplace en últimas 30
- racha máx categoría / marca en últimas 30
- max ventana móvil de 10 (categoría/marca/familia)
- pares fuzzy >=0.85, duplicados exactos, variantes
- conteo de decisiones: llm/override/hardcap y razones de rechazo
"""
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import (
    infer_offer_category, normalize_brand, product_family,
    title_fingerprint, title_similarity,
)

DB = "data/ofertas_hunter.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-30T23:14:00Z"
OUT = "logs/diversity_validation.jsonl"
DECISION_KIND = "diversity_curator_decision"


def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=20000")
    return c


def max_streak(seq):
    best = 0; cur = 0; cur_val = None; best_val = None
    for v in seq:
        cur = cur + 1 if v == cur_val else 1
        cur_val = v
        if cur > best:
            best, best_val = cur, v
    return best, best_val


def max_window(seq, w=10):
    best = Counter()
    for i in range(len(seq)):
        cc = Counter(x for x in seq[max(0, i - w + 1):i + 1] if x and x != "-")
        for k, v in cc.items():
            best[k] = max(best[k], v)
    return best


def main():
    c = conn()
    rows = c.execute(
        """
        SELECT pm.id pm_id, pm.sent_at, o.message_payload_json, p.title p_title, p.brand p_brand
        FROM published_messages pm
        LEFT JOIN outbox o ON pm.outbox_id=o.id
        LEFT JOIN offers f ON f.id=pm.offer_id
        LEFT JOIN products p ON p.id=f.product_id
        WHERE pm.success=1 AND pm.sent_at >= ?
        ORDER BY pm.id ASC
        """,
        (SINCE,),
    ).fetchall()
    feed = []
    for r in rows:
        try:
            payload = json.loads(r["message_payload_json"]) if r["message_payload_json"] else {}
        except Exception:
            payload = {}
        title = payload.get("title") or r["p_title"] or ""
        feed.append({
            "marketplace": payload.get("marketplace") or "?",
            "category": infer_offer_category(title, payload, payload.get("marketplace"), payload.get("source")).normalized,
            "brand": normalize_brand(payload.get("brand") or r["p_brand"], title) or "-",
            "family": product_family(title, payload.get("brand") or r["p_brand"]),
            "fp": title_fingerprint(title), "title": title,
            "item_id": payload.get("item_id") or payload.get("asin"),
        })

    last30 = feed[-30:]
    cats = Counter(f["category"] for f in last30)
    brands = Counter(f["brand"] for f in last30 if f["brand"] != "-")
    mkts = Counter(f["marketplace"] for f in last30)
    fams = Counter(f["family"] for f in last30)
    cs, cv = max_streak([f["category"] for f in last30])
    bs, bv = max_streak([f["brand"] for f in last30])
    n30 = len(last30)
    # fuzzy
    fpairs = 0
    for i in range(len(last30)):
        for j in range(i + 1, len(last30)):
            if len(last30[i]["fp"].split()) >= 3 and len(last30[j]["fp"].split()) >= 3:
                if title_similarity(last30[i]["title"], last30[j]["title"]) >= 0.85:
                    fpairs += 1
    dup_exact = sum(1 for k, v in Counter(f["item_id"] for f in last30 if f["item_id"]).items() if v > 1)

    # decisiones
    drows = c.execute(
        "SELECT payload_json FROM runtime_events WHERE kind=? AND created_at >= ?",
        (DECISION_KIND, SINCE),
    ).fetchall()
    dec_total = 0; llm = 0; override = 0; hardcap = 0
    reasons = Counter()
    for r in drows:
        try:
            p = json.loads(r["payload_json"])
        except Exception:
            continue
        if "candidates_after_hard_caps" not in p:
            continue
        dec_total += 1
        if p.get("llm_used"):
            llm += 1
        if p.get("override_used"):
            override += 1
        cand = p.get("candidates_count") or 0
        after = p.get("candidates_after_hard_caps")
        if after is not None and cand and after < cand:
            hardcap += 1
        for x in (p.get("rejected_candidates") or []):
            reasons[x.get("reason")] += 1

    snap = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "since": SINCE,
        "total_post_fix": len(feed),
        "last30": {
            "n": n30,
            "categories": dict(cats.most_common()),
            "top_brands": dict(brands.most_common(6)),
            "marketplaces": dict(mkts.most_common()),
            "top_category_pct": (100 * cats.most_common(1)[0][1] // n30) if n30 else 0,
            "max_streak_category": [cs, cv],
            "max_streak_brand": [bs, bv],
            "variant_families_repeated": {k: v for k, v in fams.items() if v > 1},
            "dup_exact": dup_exact,
            "fuzzy_pairs_ge_085": fpairs,
            "window10_max_category": dict(max_window([f["category"] for f in last30]).most_common(3)),
            "window10_max_brand": dict(max_window([f["brand"] for f in last30]).most_common(3)),
            "window10_max_family": dict(max_window([f["family"] for f in last30]).most_common(3)),
        },
        "decisions": {
            "total": dec_total, "llm_used": llm, "override": override,
            "hardcap_filtered": hardcap, "reject_reasons": dict(reasons),
        },
    }
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
    print(json.dumps(snap, ensure_ascii=False))


if __name__ == "__main__":
    main()
