#!/usr/bin/env python3
"""Exporta TODO el outbox a JSON (READ-ONLY).

Incluye por item: id, offer_id, estado, tipo, marketplace, categoría
normalizada, marca normalizada, product_family, precio, descuento, título,
url, item_id, fecha. Y agregados con porcentajes (categoría, marketplace,
marca, estado) + qué categoría/marca domina.

NO modifica nada. Abre la DB en modo ?mode=ro con busy_timeout.
"""
import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import (
    infer_offer_category, normalize_brand, product_family,
)

DB = "data/ofertas_hunter.db"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/outbox_export.json"


def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c


def pct(counter, total):
    return {k: {"count": v, "pct": round(100 * v / total, 1)} for k, v in counter.most_common()}


def main():
    c = conn()
    rows = c.execute(
        """
        SELECT o.id, o.offer_id, o.type, o.state, o.enqueued_at, o.scheduled_for,
               o.attempts, o.message_payload_json,
               p.title AS p_title, p.brand AS p_brand, p.url_canonical AS p_url,
               p.category AS p_category, f.discount_percent AS f_disc,
               f.classification AS f_class
        FROM outbox o
        LEFT JOIN offers f ON f.id = o.offer_id
        LEFT JOIN products p ON p.id = f.product_id
        ORDER BY o.id ASC
        """
    ).fetchall()

    items = []
    for r in rows:
        try:
            payload = json.loads(r["message_payload_json"]) if r["message_payload_json"] else {}
        except Exception:
            payload = {}
        title = payload.get("title") or r["p_title"] or ""
        mkt = payload.get("marketplace") or "unknown"
        cat = infer_offer_category(title, payload, mkt, payload.get("source"))
        brand = normalize_brand(payload.get("brand") or r["p_brand"], title)
        fam = product_family(title, payload.get("brand") or r["p_brand"])
        items.append({
            "id": r["id"],
            "offer_id": r["offer_id"],
            "state": r["state"],
            "type": r["type"],
            "marketplace": mkt,
            "category_normalized": cat.normalized,
            "category_raw": cat.raw,
            "category_source": cat.source,
            "brand_normalized": brand,
            "product_family": fam,
            "title": title,
            "current_price": payload.get("current_price"),
            "previous_price": payload.get("previous_price"),
            "discount_percent": payload.get("discount_percent") if payload.get("discount_percent") is not None else r["f_disc"],
            "classification": r["f_class"],
            "url": payload.get("final_url") or payload.get("url") or payload.get("canonical_url") or r["p_url"],
            "affiliate_url": payload.get("affiliate_url"),
            "item_id": payload.get("item_id") or payload.get("asin"),
            "enqueued_at": r["enqueued_at"],
            "scheduled_for": r["scheduled_for"],
            "attempts": r["attempts"],
        })

    total = len(items)

    # Agregados globales
    by_state = Counter(i["state"] for i in items)
    by_cat = Counter(i["category_normalized"] for i in items)
    by_mkt = Counter(i["marketplace"] for i in items)
    by_brand = Counter(i["brand_normalized"] or "(sin marca)" for i in items)
    by_family = Counter(i["product_family"] for i in items)

    # Agregados solo del pool publicable (pending)
    pend = [i for i in items if i["state"] == "pending"]
    np = len(pend) or 1
    pend_cat = Counter(i["category_normalized"] for i in pend)
    pend_brand = Counter(i["brand_normalized"] or "(sin marca)" for i in pend)
    pend_mkt = Counter(i["marketplace"] for i in pend)

    top_cat = by_cat.most_common(1)[0] if by_cat else ("-", 0)
    top_brand = by_brand.most_common(1)[0] if by_brand else ("-", 0)
    top_mkt = by_mkt.most_common(1)[0] if by_mkt else ("-", 0)

    result = {
        "generated_at_utc": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "db_path": DB,
        "total_outbox_items": total,
        "summary": {
            "by_state": pct(by_state, total),
            "by_category": pct(by_cat, total),
            "by_marketplace": pct(by_mkt, total),
            "by_brand_top20": pct(Counter(dict(by_brand.most_common(20))), total),
            "most_common_category": {"value": top_cat[0], "count": top_cat[1],
                                      "pct": round(100 * top_cat[1] / total, 1) if total else 0},
            "most_common_brand": {"value": top_brand[0], "count": top_brand[1],
                                  "pct": round(100 * top_brand[1] / total, 1) if total else 0},
            "most_common_marketplace": {"value": top_mkt[0], "count": top_mkt[1],
                                        "pct": round(100 * top_mkt[1] / total, 1) if total else 0},
        },
        "pending_pool_summary": {
            "total_pending": len(pend),
            "by_category": pct(pend_cat, np),
            "by_brand_top20": pct(Counter(dict(pend_brand.most_common(20))), np),
            "by_marketplace": pct(pend_mkt, np),
        },
        "items": items,
    }

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, default=str)

    print(f"WROTE {OUT}")
    print(f"total items: {total}")
    print(f"by_state: {dict(by_state)}")
    print(f"most_common_category: {top_cat[0]} ({top_cat[1]}, {round(100*top_cat[1]/total,1) if total else 0}%)")
    print(f"most_common_brand: {top_brand[0]} ({top_brand[1]})")
    print(f"pending total: {len(pend)} | pending top cat: {pend_cat.most_common(3)}")


if __name__ == "__main__":
    main()
