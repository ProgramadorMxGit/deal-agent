"""Inspecciona items en outbox/published que usaron caption_override.

Eso revela si la IA estuvo re-escribiendo la copia.
"""

from __future__ import annotations

import json
from ofertas_hunter.db import connect


def main() -> None:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, type, state, message_payload_json "
            "FROM outbox "
            "WHERE message_payload_json LIKE '%caption_override%' "
            "ORDER BY id DESC LIMIT 30"
        ).fetchall()
        print(f"=== Outbox items con caption_override: {len(rows)}")
        for r in rows:
            try:
                p = json.loads(r["message_payload_json"])
            except Exception:
                p = {}
            title = (p.get("title") or "")[:50]
            override = (p.get("caption_override") or "")[:80].replace("\n", " | ")
            print(
                f"  id={r['id']:<5} {r['type']:<13} {r['state']:<10} "
                f"title={title!r}\n"
                f"     override={override!r}"
            )

        rows = conn.execute(
            "SELECT id, outbox_id, sent_at, message_text "
            "FROM published_messages "
            "ORDER BY id DESC LIMIT 10"
        ).fetchall()
        print(f"\n=== Últimos {len(rows)} published_messages:")
        for r in rows:
            t = (r["message_text"] or "").replace("\n", " | ")[:100]
            print(f"  pid={r['id']} oid={r['outbox_id']} at={r['sent_at']} text={t!r}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
