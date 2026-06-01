#!/usr/bin/env python3
"""Backfill SEGURO de metadata normalizada de categoría en el outbox.

Recalcula y persiste los 6 campos en message_payload_json:
  category_raw, category_normalized, category_source, category_confidence,
  brand_normalized, product_family

REGLAS DE SEGURIDAD (no negociables):
- SOLO toca filas en estado 'pending' y 'deferred'.
- NUNCA toca 'sent', 'discarded', 'failed', 'in_flight'.
- NUNCA toca published_messages.
- NO cambia precios, descuentos, urls, gates ni estado de la fila.
- Solo agrega/actualiza los 6 campos de metadata dentro del JSON del payload.
- Idempotente: re-ejecutar no cambia nada si ya está al día.

Uso:
  python scripts/backfill_category_metadata.py --dry-run   # solo reporta
  python scripts/backfill_category_metadata.py --apply     # escribe cambios
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "src")

from ofertas_hunter.dispatching.diversity_metadata import compute_diversity_metadata

DB = "data/ofertas_hunter.db"
TARGET_STATES = ("pending", "deferred")
META_FIELDS = (
    "category_raw", "category_normalized", "category_source",
    "category_confidence", "brand_normalized", "product_family",
)


def connect(write: bool):
    if write:
        c = sqlite3.connect(DB, timeout=60)
    else:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    c.row_factory = sqlite3.Row
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="escribir cambios")
    ap.add_argument("--dry-run", action="store_true", help="solo reportar (default)")
    args = ap.parse_args()
    write = bool(args.apply)
    mode = "APPLY (escritura)" if write else "DRY-RUN (solo lectura)"
    print(f"== Backfill category metadata == modo: {mode}")
    print(f"   Estados objetivo: {TARGET_STATES}  (NUNCA toca sent/discarded/failed)")

    c = connect(write)

    placeholders = ",".join("?" for _ in TARGET_STATES)
    rows = c.execute(
        f"SELECT id, state, message_payload_json FROM outbox "
        f"WHERE state IN ({placeholders}) ORDER BY id",
        TARGET_STATES,
    ).fetchall()
    print(f"   Filas candidatas: {len(rows)}")

    before_cat = Counter()
    after_cat = Counter()
    changed = 0
    unchanged = 0
    errors = 0
    updates = []

    for r in rows:
        rid = r["id"]
        try:
            p = json.loads(r["message_payload_json"])
        except Exception:
            errors += 1
            continue

        old_norm = p.get("category_normalized")
        before_cat[str(old_norm)] += 1

        meta = compute_diversity_metadata(p)
        after_cat[meta["category_normalized"]] += 1

        # ¿cambia algo?
        needs = any(p.get(k) != meta[k] for k in META_FIELDS)
        if not needs:
            unchanged += 1
            continue

        merged = dict(p)
        merged.update(meta)
        # marca de auditoría (no afecta lógica)
        merged["category_backfilled"] = True
        updates.append((rid, json.dumps(merged, ensure_ascii=False)))
        changed += 1

    print(f"\n   A recalcular -> cambia: {changed} | sin cambio: {unchanged} | errores: {errors}")

    print("\n   ANTES (category_normalized almacenado):")
    for k, v in before_cat.most_common():
        print(f"     {k:28} {v}")
    print("\n   DESPUES (category_normalized recalculado):")
    for k, v in after_cat.most_common():
        print(f"     {k:28} {v}")

    if write and updates:
        cur = c.cursor()
        cur.executemany(
            "UPDATE outbox SET message_payload_json=? "
            "WHERE id=? AND state IN ('pending','deferred')",
            [(j, rid) for (rid, j) in updates],
        )
        c.commit()
        print(f"\n   ESCRITAS {cur.rowcount} filas (solo pending/deferred).")
    elif write:
        print("\n   Nada que escribir (todo al día).")
    else:
        print("\n   DRY-RUN: no se escribió nada. Re-ejecuta con --apply para aplicar.")

    c.close()


if __name__ == "__main__":
    main()
