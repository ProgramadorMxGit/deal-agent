"""Inspecciona ofertas recientes con sus precios y descuentos calculados.

Útil para detectar:
- previous_price inventado o mal extraído
- discount_percent que no cuadra matemáticamente
"""
import json
import sqlite3
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

c = sqlite3.connect(str(DB))
c.row_factory = sqlite3.Row

print("=== Últimas 25 publicaciones exitosas ===\n")
rows = c.execute("""
    SELECT pm.id pm_id, pm.sent_at, pm.message_text,
           o.type, o.message_payload_json payload,
           json_extract(o.message_payload_json, '$.title') title,
           json_extract(o.message_payload_json, '$.current_price') cur,
           json_extract(o.message_payload_json, '$.previous_price') prev,
           json_extract(o.message_payload_json, '$.discount_percent') disc,
           json_extract(o.message_payload_json, '$.marketplace') mkt,
           json_extract(o.message_payload_json, '$.item_id') iid,
           json_extract(o.message_payload_json, '$.asin') asin,
           json_extract(o.message_payload_json, '$.url') url
    FROM published_messages pm
    JOIN outbox o ON pm.outbox_id = o.id
    WHERE pm.success = 1
    ORDER BY pm.sent_at DESC
    LIMIT 25
""").fetchall()

problemas = 0
for r in rows:
    cur = r["cur"]
    prev = r["prev"]
    disc = r["disc"]
    title = (r["title"] or "")[:60]
    iid = r["iid"] or r["asin"] or "?"

    # Calcular descuento real si hay ambos precios
    real_disc = None
    if cur and prev and prev > 0:
        real_disc = round((prev - cur) / prev * 100, 1)

    flag = ""
    # Casos sospechosos:
    if prev is None and disc is not None and disc > 0:
        flag = " ⚠️ DISCOUNT_SIN_PREV"
        problemas += 1
    elif real_disc is not None and disc is not None:
        diff = abs(real_disc - disc)
        if diff > 5:
            flag = f" ⚠️ DISCOUNT_NO_CUADRA (real={real_disc}%, reportado={disc}%)"
            problemas += 1
    elif prev is not None and (prev <= cur if cur else False):
        flag = " ⚠️ PREV<=CUR"
        problemas += 1

    print(f"[{r['mkt']}] {iid}: {title}")
    print(f"  precios: cur={cur} prev={prev} disc_reportado={disc}% disc_real={real_disc}%{flag}")
    print(f"  url={r['url'][:90] if r['url'] else 'N/A'}")
    print()

print(f"\n=== Resumen: {problemas}/{len(rows)} ofertas con problemas ===")
