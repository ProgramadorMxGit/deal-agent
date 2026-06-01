#!/usr/bin/env python3
"""Validación de diversidad en vivo (READ-ONLY).

Audita publicaciones reales POST-FIX y las decisiones del curator.
NO publica, NO escribe estados, NO emite eventos. Solo lee la DB en
modo ?mode=ro con busy_timeout.

Uso:
    python diversity_validation.py [SINCE_ISO]

SINCE_ISO por defecto = 2026-05-30T22:45:00Z (primer deploy del fix).
Calcula métricas para últimas 30, últimas 50 y ventanas móviles de 10.
"""
import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import (
    infer_offer_category, normalize_brand, product_family,
    title_fingerprint, title_similarity,
)

DB = "data/ofertas_hunter.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-30T22:45:00Z"

DECISION_KIND = "diversity_curator_decision"
REASONS = [
    "diversity_same_category_window",
    "diversity_same_brand_window",
    "diversity_same_product_family",
    "diversity_same_product_family_recent",
    "diversity_duplicate_fuzzy_title",
    "diversity_duplicate_exact",
    "diversity_same_marketplace_window",
]


def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    return c


def load_publications(c, since):
    rows = c.execute(
        """
        SELECT pm.id pm_id, pm.outbox_id, pm.sent_at, o.message_payload_json,
               p.title p_title, p.brand p_brand
        FROM published_messages pm
        LEFT JOIN outbox o ON pm.outbox_id = o.id
        LEFT JOIN offers f ON f.id = pm.offer_id
        LEFT JOIN products p ON p.id = f.product_id
        WHERE pm.success = 1 AND pm.sent_at >= ?
        ORDER BY pm.id ASC
        """,
        (since,),
    ).fetchall()
    feed = []
    for r in rows:
        try:
            payload = json.loads(r["message_payload_json"]) if r["message_payload_json"] else {}
        except Exception:
            payload = {}
        title = payload.get("title") or r["p_title"] or ""
        mkt = payload.get("marketplace") or "?"
        cat = infer_offer_category(title, payload, mkt, payload.get("source")).normalized
        brand = normalize_brand(payload.get("brand") or r["p_brand"], title)
        fam = product_family(title, payload.get("brand") or r["p_brand"])
        feed.append({
            "pm_id": r["pm_id"], "outbox_id": r["outbox_id"], "sent_at": r["sent_at"],
            "marketplace": mkt, "category": cat, "brand": brand or "-",
            "family": fam, "fp": title_fingerprint(title), "title": title,
            "item_id": payload.get("item_id") or payload.get("asin"),
        })
    return feed


def max_streak(seq):
    best = 0; best_val = None; cur = 0; cur_val = None
    for v in seq:
        if v == cur_val:
            cur += 1
        else:
            cur_val = v; cur = 1
        if cur > best:
            best = cur; best_val = v
    return best, best_val


def max_in_sliding_window(seq, w=10):
    best = Counter()
    for i in range(len(seq)):
        win = seq[max(0, i - w + 1):i + 1]
        cc = Counter(x for x in win if x and x != "-")
        for k, v in cc.items():
            best[k] = max(best[k], v)
    return best


def fuzzy_pairs(feed, thr=0.85):
    out = []
    n = len(feed)
    for i in range(n):
        for j in range(i + 1, n):
            if len(feed[i]["fp"].split()) < 3 or len(feed[j]["fp"].split()) < 3:
                continue
            s = title_similarity(feed[i]["title"], feed[j]["title"])
            if s >= thr:
                out.append((feed[i]["pm_id"], feed[j]["pm_id"], round(s, 2),
                            feed[i]["title"][:40], feed[j]["title"][:40]))
    return out


def analyze_window(feed, label):
    print(f"\n{'='*70}\n  VENTANA: {label}  (n={len(feed)})\n{'='*70}")
    if not feed:
        print("  (sin publicaciones)")
        return
    cats = Counter(f["category"] for f in feed)
    brands = Counter(f["brand"] for f in feed if f["brand"] != "-")
    mkts = Counter(f["marketplace"] for f in feed)
    fams = Counter(f["family"] for f in feed)
    n = len(feed)
    print(f"  Por categoría: " + ", ".join(f"{k}={v} ({100*v//n}%)" for k, v in cats.most_common()))
    print(f"  Por marketplace: " + ", ".join(f"{k}={v} ({100*v//n}%)" for k, v in mkts.most_common()))
    print(f"  Top marcas: " + ", ".join(f"{k}={v}" for k, v in brands.most_common(6)))
    # rachas consecutivas
    cs, cv = max_streak([f["category"] for f in feed])
    bs, bv = max_streak([f["brand"] for f in feed])
    ms, mv = max_streak([f["marketplace"] for f in feed])
    print(f"  Racha máx categoría: {cs} ({cv}) | marca: {bs} ({bv}) | marketplace: {ms} ({mv})")
    # familias repetidas (variantes)
    var = {k: v for k, v in fams.items() if v > 1}
    print(f"  Familias repetidas (variantes mismo producto): {var or 'ninguna'}")
    # dup exactos
    iid = Counter(f["item_id"] for f in feed if f["item_id"])
    dup_exact = {k: v for k, v in iid.items() if v > 1}
    print(f"  Duplicados exactos (item_id): {dup_exact or 'ninguno'}")
    # fuzzy
    fp = fuzzy_pairs(feed)
    print(f"  Pares título similitud >=0.85: {len(fp)}")
    for a, b, s, ta, tb in fp[:5]:
        print(f"     pm{a}~pm{b} sim={s} | '{ta}' ~ '{tb}'")
    # sliding window of 10
    if n >= 1:
        mc = max_in_sliding_window([f["category"] for f in feed])
        mb = max_in_sliding_window([f["brand"] for f in feed])
        mf = max_in_sliding_window([f["family"] for f in feed])
        print(f"  [Ventana móvil de 10] máx misma categoría: {dict(mc.most_common(3))} (límite 3)")
        print(f"  [Ventana móvil de 10] máx misma marca: {dict(mb.most_common(3))} (límite 2)")
        print(f"  [Ventana móvil de 10] máx misma familia: {dict(mf.most_common(3))} (límite 1)")


def analyze_decisions(c, since):
    print(f"\n{'='*70}\n  DECISIONES DEL CURATOR (desde {since})\n{'='*70}")
    rows = c.execute(
        "SELECT created_at, payload_json FROM runtime_events "
        "WHERE kind=? AND created_at >= ? ORDER BY id ASC",
        (DECISION_KIND, since),
    ).fetchall()
    total = 0; new_schema = 0
    llm_used = 0; fallback = 0; override = 0; hardcap_active = 0
    reason_counts = Counter()
    pool_mono = 0; pool_varied = 0
    override_examples = []
    reject_examples = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except Exception:
            continue
        total += 1
        if "candidates_after_hard_caps" not in p:
            continue
        new_schema += 1
        if p.get("llm_used"):
            llm_used += 1
        if p.get("fallback_used"):
            fallback += 1
        if p.get("override_used"):
            override += 1
            if len(override_examples) < 6:
                override_examples.append((r["created_at"], p))
        cand = p.get("candidates_count") or 0
        after = p.get("candidates_after_hard_caps")
        if after is not None and cand and after < cand:
            hardcap_active += 1
        rej = p.get("rejected_candidates") or []
        for x in rej:
            reason_counts[x.get("reason")] += 1
        if rej and not reject_examples:
            reject_examples = [(r["created_at"], p.get("selected_outbox_id"), rej)]
        # pool mono vs variado: mirar categorías de rechazados + seleccionado
        ws = p.get("window_stats") or {}
        # heurística: si todos los candidatos eran 1 categoría -> mono
        cats_in_decision = set()
        for x in rej:
            if x.get("category"):
                cats_in_decision.add(x["category"])
        if p.get("selected_category_normalized"):
            cats_in_decision.add(p["selected_category_normalized"])
        if cand >= 3:
            if len(cats_in_decision) <= 1:
                pool_mono += 1
            else:
                pool_varied += 1

    print(f"  Total decisiones: {total} (esquema nuevo: {new_schema})")
    print(f"  Usaron LLM: {llm_used} | fallback determinístico: {fallback} | override: {override}")
    print(f"  Decisiones donde hard caps filtraron candidatos: {hardcap_active}")
    print(f"  Pool variado (>=2 cat, >=3 cand): {pool_varied} | Pool mono-categoría: {pool_mono}")
    print(f"\n  Candidatas RECHAZADAS por razón:")
    for reason in REASONS:
        print(f"    {reason}: {reason_counts.get(reason, 0)}")
    other = {k: v for k, v in reason_counts.items() if k not in REASONS}
    if other:
        print(f"    (otras): {other}")

    print(f"\n  Ejemplos de OVERRIDE (todos los candidatos violaban diversidad):")
    for ts, p in override_examples:
        print(f"    {ts} sel_outbox={p.get('selected_outbox_id')} "
              f"cat={p.get('selected_category_normalized')} brand={p.get('selected_brand_normalized')} "
              f"cand={p.get('candidates_count')}->{p.get('candidates_after_hard_caps')} "
              f"reason={p.get('override_reason')}")
    if not override_examples:
        print("    (ninguno)")

    print(f"\n  Ejemplo de rejected_candidates (1 decisión):")
    for ts, sel, rej in reject_examples:
        print(f"    decisión {ts} (seleccionó outbox={sel}):")
        for x in rej[:8]:
            print(f"      - outbox={x.get('outbox_id')} reason={x.get('reason')} "
                  f"cat={x.get('category')} brand={x.get('brand')} fam={x.get('product_family')}")
    return {
        "total": total, "llm_used": llm_used, "override": override,
        "hardcap_active": hardcap_active, "reasons": dict(reason_counts),
        "pool_mono": pool_mono, "pool_varied": pool_varied,
    }


def print_feed_table(feed):
    print(f"\n{'='*70}\n  TABLA DE PUBLICACIONES POST-FIX (n={len(feed)})\n{'='*70}")
    print(f"{'#':>3} {'hora':>8} {'mkt':>4} {'categoria':<22} {'marca':<14} titulo")
    for i, f in enumerate(feed, 1):
        print(f"{i:>3} {f['sent_at'][11:19]:>8} {f['marketplace'][:4]:>4} "
              f"{f['category']:<22} {str(f['brand'])[:13]:<14} {f['title'][:44]}")


def main():
    c = conn()
    feed = load_publications(c, SINCE)
    print(f"### VALIDACIÓN DE DIVERSIDAD — publicaciones desde {SINCE}")
    print(f"### total post-fix: {len(feed)}")
    print_feed_table(feed)
    analyze_window(feed[-30:], "ÚLTIMAS 30")
    analyze_window(feed[-50:], "ÚLTIMAS 50")
    analyze_window(feed, "TODAS POST-FIX")
    analyze_decisions(c, SINCE)


if __name__ == "__main__":
    main()
