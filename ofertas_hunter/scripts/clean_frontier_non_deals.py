#!/usr/bin/env python
"""Limpia del frontier ML los listados de CATEGORÍA GENÉRICA (no-ofertas).

Causa raíz: el frontier ML se llenó de ~27K listados genéricos
(`listado.mercadolibre.com.mx/categoria/...`) que traen productos a precio
normal, desplazando a las páginas de ofertas. Este script los borra,
conservando:
  - `product` (PDPs ya descubiertos)
  - `deals` (páginas de ofertas)
  - listings que SON de ofertas (contienen /ofertas, _Descuento, etc.)

Seguro en DB viva (WAL). Idempotente. Soporta --dry-run.

Uso:
    .venv/bin/python scripts/clean_frontier_non_deals.py [--dry-run] [--marketplace mercadolibre]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

DB = str(Path(__file__).resolve().parents[1] / "data" / "ofertas_hunter.db")

# Señales de que un listado SÍ es de ofertas (se conserva).
_DEAL_SIGNAL_RE = re.compile(
    r"/ofertas|_Descuento_|_Deal|tier=deal|promociones|mas-vendidos",
    re.IGNORECASE,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--marketplace", default="mercadolibre")
    args = ap.parse_args()

    db = sqlite3.connect(DB, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")

    mp = args.marketplace
    total = db.execute(
        "SELECT COUNT(*) FROM frontier WHERE marketplace=?", (mp,)
    ).fetchone()[0]

    # Candidatos a borrar: listing/category que NO son de ofertas.
    rows = db.execute(
        "SELECT id, url_canonical, url_type FROM frontier "
        "WHERE marketplace=? AND url_type IN ('listing','category')",
        (mp,),
    ).fetchall()

    to_delete = [
        r["id"] for r in rows
        if not _DEAL_SIGNAL_RE.search(r["url_canonical"] or "")
    ]
    keep_offers = len(rows) - len(to_delete)

    print(f"frontier {mp}: total={total}")
    print(f"  listing/category: {len(rows)}")
    print(f"  -> son de ofertas (CONSERVAR): {keep_offers}")
    print(f"  -> genéricos (BORRAR): {len(to_delete)}")

    if args.dry_run:
        print("[DRY-RUN] no se borró nada")
        # muestra
        sample = [r["url_canonical"][:80] for r in rows
                  if not _DEAL_SIGNAL_RE.search(r["url_canonical"] or "")][:8]
        for s in sample:
            print("   borraría:", s)
        return 0

    deleted = 0
    BATCH = 5000
    for i in range(0, len(to_delete), BATCH):
        chunk = to_delete[i:i + BATCH]
        ph = ",".join("?" for _ in chunk)
        cur = db.execute(f"DELETE FROM frontier WHERE id IN ({ph})", chunk)
        db.commit()
        deleted += cur.rowcount

    remaining = db.execute(
        "SELECT COUNT(*) FROM frontier WHERE marketplace=?", (mp,)
    ).fetchone()[0]
    print(f"borrados: {deleted}")
    print(f"frontier {mp} restante: {remaining}")
    # desglose restante por tipo
    for r in db.execute(
        "SELECT url_type, count(*) c FROM frontier WHERE marketplace=? GROUP BY url_type",
        (mp,),
    ):
        print(f"  {r['url_type']}: {r['c']}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
