"""Diagnóstico profundo: ¿por qué se publicaron duplicados?

Revisa para los item_id duplicados:
- ¿Se crearon múltiples outbox items para el mismo MLM?
- ¿El guard `_recently_published` los ve correctamente?
- ¿El payload tiene `item_id` o `asin`?
"""
import json
import sqlite3
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

DUPS = ["MLM45941195", "MLM65041187"]

c = sqlite3.connect(str(DB))
c.row_factory = sqlite3.Row

for mlm in DUPS:
    print(f"\n{'='*70}\nINVESTIGANDO {mlm}\n{'='*70}")

    # Outbox items para este MLM
    rows = c.execute(
        """
        SELECT o.id, o.offer_id, o.type, o.state, o.enqueued_at,
               json_extract(o.message_payload_json, '$.item_id') iid,
               json_extract(o.message_payload_json, '$.asin') asin,
               json_extract(o.message_payload_json, '$.title') title,
               json_extract(o.message_payload_json, '$.url') url,
               json_extract(o.message_payload_json, '$.canonical_url') canonical
        FROM outbox o
        WHERE json_extract(o.message_payload_json, '$.item_id') = ?
           OR json_extract(o.message_payload_json, '$.asin') = ?
        ORDER BY o.id
        """,
        (mlm, mlm),
    ).fetchall()

    print(f"\nOutbox items con item_id={mlm}: {len(rows)}")
    for r in rows:
        print(
            f"  outbox#{r['id']} offer={r['offer_id']} type={r['type']} "
            f"state={r['state']} enqueued={r['enqueued_at']}"
        )
        print(f"    iid={r['iid']} asin={r['asin']}")
        print(f"    title={(r['title'] or '')[:70]}")

    # Published messages
    pub_rows = c.execute(
        """
        SELECT pm.id, pm.outbox_id, pm.sent_at, pm.success
        FROM published_messages pm
        JOIN outbox o ON pm.outbox_id = o.id
        WHERE json_extract(o.message_payload_json, '$.item_id') = ?
           OR json_extract(o.message_payload_json, '$.asin') = ?
        ORDER BY pm.sent_at
        """,
        (mlm, mlm),
    ).fetchall()
    print(f"\nPublished_messages para {mlm}: {len(pub_rows)}")
    for r in pub_rows:
        print(
            f"  pm#{r['id']} outbox#{r['outbox_id']} sent_at={r['sent_at']} "
            f"success={r['success']}"
        )

    # Products: ¿hay múltiples filas para el mismo MLM?
    prod_rows = c.execute(
        "SELECT id, marketplace, marketplace_id, url_canonical, title FROM products "
        "WHERE marketplace_id = ?",
        (mlm,),
    ).fetchall()
    print(f"\nProducts con marketplace_id={mlm}: {len(prod_rows)}")
    for r in prod_rows:
        print(f"  product#{r['id']} mkt={r['marketplace']} url={r['url_canonical'][:80]}")

    # Test del guard _recently_published manualmente
    print("\nProbando guard _recently_published (48h)...")
    from datetime import datetime, timezone, timedelta
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=48)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    test = c.execute(
        """
        SELECT pm.id FROM published_messages pm
        JOIN outbox o ON pm.outbox_id = o.id
        WHERE pm.success = 1
          AND pm.sent_at >= ?
          AND (
            json_extract(o.message_payload_json, '$.item_id') = ?
            OR json_extract(o.message_payload_json, '$.asin') = ?
          )
        LIMIT 1
        """,
        (cutoff, mlm, mlm),
    ).fetchone()
    print(f"  guard ve duplicado: {bool(test)}  (cutoff={cutoff})")
