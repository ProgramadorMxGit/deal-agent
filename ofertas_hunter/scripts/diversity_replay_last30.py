#!/usr/bin/env python3
"""Replay del nuevo selector contra las últimas 30 publicaciones reales.

READ-ONLY sobre la DB. NO publica, NO escribe estados, NO emite eventos.
Reconstruye el feed publicado y simula: en cada paso, dado el historial
acumulado, ¿el nuevo selector habría PUBLICADO o SALTADO este item?

Esto demuestra que los hard caps habrían roto las rachas (25 proteínas,
14 BHP, variantes de sabor, etc.). No reordena el pool real (no tenemos
snapshot del pool elegible por ciclo); evalúa cada item publicado contra
la política de diversidad con el historial real previo.
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "src")

from ofertas_hunter.dispatching.diversity_filters import (
    CandidateMeta, HardCapConfig, HistoryItem, apply_hard_caps,
)
from ofertas_hunter.dispatching.diversity_metadata import (
    infer_offer_category, normalize_brand, product_family, title_fingerprint,
)

DB = "data/ofertas_hunter.db"

cfg = HardCapConfig(
    enabled=True, window_size=10, max_same_category=3, max_same_brand=2,
    max_same_product_family=1, max_same_marketplace=7,
    fuzzy_title_threshold=0.85, reject_similar_hours=24,
    allow_override_if_no_alternative=True,
)

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=15000")

rows = c.execute(
    """
    SELECT pm.id pm_id, pm.outbox_id, pm.sent_at, o.message_payload_json,
           p.title p_title, p.brand p_brand
    FROM published_messages pm
    LEFT JOIN outbox o ON pm.outbox_id = o.id
    LEFT JOIN offers f ON f.id = pm.offer_id
    LEFT JOIN products p ON p.id = f.product_id
    WHERE pm.success = 1
    ORDER BY pm.id DESC
    LIMIT 30
    """
).fetchall()
rows = list(reversed(rows))  # cronológico

def meta_from(row, idx):
    try:
        payload = json.loads(row["message_payload_json"]) if row["message_payload_json"] else {}
    except Exception:
        payload = {}
    title = payload.get("title") or row["p_title"] or ""
    mkt = payload.get("marketplace") or "unknown"
    cat = infer_offer_category(title, payload, mkt, payload.get("source"))
    brand = normalize_brand(payload.get("brand") or row["p_brand"], title)
    fam = product_family(title, payload.get("brand") or row["p_brand"])
    fp = title_fingerprint(title)
    item_id = payload.get("item_id") or payload.get("asin")

    class _It:
        id = idx
    return CandidateMeta(
        item=_It(), marketplace=mkt, category=cat.normalized, brand=brand,
        product_family=fam, title_fingerprint=fp, title=title, item_id=item_id,
    ), payload, cat


published_history: list[HistoryItem] = []  # más reciente primero
published_count = 0
skipped_count = 0
print(f"{'#':>2} {'decision':>9} {'cat':<22} {'brand':<14} reason / title")
print("-" * 110)
for idx, row in enumerate(rows, 1):
    meta, payload, cat = meta_from(row, idx)
    sent_at = datetime.fromisoformat(row["sent_at"].replace("Z", "+00:00"))
    # evaluar este item contra el historial ya "publicado" en el replay
    res = apply_hard_caps([meta], published_history, cfg, sent_at,
                          has_marketplace_alternative=True)
    if res.kept:
        decision = "PUBLISH"
        published_count += 1
        published_history.insert(0, HistoryItem(
            marketplace=meta.marketplace, category=meta.category, brand=meta.brand,
            product_family=meta.product_family, title_fingerprint=meta.title_fingerprint,
            item_id=meta.item_id, sent_at=sent_at,
        ))
        reason = ""
    else:
        decision = "SKIP"
        skipped_count += 1
        reason = res.rejected[0].reason
    print(f"{idx:>2} {decision:>9} {(meta.category or '-'):<22} {(meta.brand or '-'):<14} "
          f"{reason}  | {meta.title[:46]}")

print("-" * 110)
print(f"PUBLICADAS (replay): {published_count}/30   SALTADAS por diversidad: {skipped_count}/30")
# stats de lo que SÍ se habría publicado
cats = {}
brands = {}
for h in published_history:
    cats[h.category] = cats.get(h.category, 0) + 1
    if h.brand:
        brands[h.brand] = brands.get(h.brand, 0) + 1
print("Distribución categorías publicadas (replay):", dict(sorted(cats.items(), key=lambda x: -x[1])))
print("Distribución marcas publicadas (replay):", dict(sorted(brands.items(), key=lambda x: -x[1])))
print()
print("NOTA: en producción los SKIP NO se descartan; quedan pending y el bot")
print("habría elegido OTRA categoría/ marca del pool real para ese turno. Este")
print("replay demuestra que las repeticiones habrían sido bloqueadas.")
