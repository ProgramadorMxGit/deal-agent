"""Limpia `caption_override` de items pending del outbox cuando NO respetan
el formato canónico (asteriscos en *título*, *X% de descuento*, *AHORA: $..*,
*Ver oferta:*, tildes ~$..~ para Antes).

Cuando se quita el `caption_override`, el publisher cae al formato estricto
de `format_normal_offer` / `format_price_error`.

Uso:
    .\.venv\Scripts\python.exe scripts\fix_caption_overrides.py            # dry-run
    .\.venv\Scripts\python.exe scripts\fix_caption_overrides.py --apply   # aplica
"""

from __future__ import annotations

import argparse
import json

from ofertas_hunter.db import connect
from ofertas_hunter.publishing.whatsapp_publisher import (
    _caption_respects_canonical_format,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Aplica los cambios. Sin esto sólo muestra qué se haría.",
    )
    args = ap.parse_args()

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, type, state, message_payload_json "
            "FROM outbox "
            "WHERE message_payload_json LIKE '%caption_override%' "
            "AND state IN ('pending', 'in_flight')"
        ).fetchall()
        print(f"Items pending con caption_override: {len(rows)}")
        kept = 0
        cleared = 0
        for r in rows:
            try:
                payload = json.loads(r["message_payload_json"])
            except Exception:
                continue
            override = payload.get("caption_override") or ""
            ok = _caption_respects_canonical_format(
                text=override,
                item_type=r["type"],
                payload=payload,
            )
            mark = "OK " if ok else "BAD"
            title = (payload.get("title") or "")[:50]
            print(f"  [{mark}] id={r['id']:<5} {r['type']:<13} title={title!r}")
            if ok:
                kept += 1
                continue
            cleared += 1
            if args.apply:
                payload.pop("caption_override", None)
                conn.execute(
                    "UPDATE outbox SET message_payload_json=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), r["id"]),
                )
        if args.apply:
            conn.commit()
        print(f"\n  conservados (formato OK): {kept}")
        print(f"  limpiados (BAD format):    {cleared}")
        if not args.apply and cleared:
            print("\n  (dry-run; corre con --apply para aplicar)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
