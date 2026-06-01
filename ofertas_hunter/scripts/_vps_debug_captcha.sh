#!/usr/bin/env bash
set -uo pipefail
cd /opt/deal-agent/ofertas_hunter

echo "=== Flag en .env ==="
grep '^AMAZON_HUNTER_LEGACY' .env || echo "(no encontrado)"

echo
echo "=== Settings cargados ==="
./.venv/bin/python - <<'PY'
from ofertas_hunter.config import get_settings
s = get_settings()
print(f"  amazon_hunter_legacy = {s.amazon_hunter_legacy}")
print(f"  amazon_headless      = {s.amazon_headless}")
print(f"  amazon_user_data_dir = {s.amazon_user_data_dir}")
PY

echo
echo "=== Hunter que se instanciaría ==="
./.venv/bin/python - <<'PY'
import asyncio
from ofertas_hunter.config import get_settings
from ofertas_hunter.db import init_db, connect
from ofertas_hunter.mcp.context import ServerContext

async def main():
    init_db()
    db = connect()
    settings = get_settings()
    ctx = ServerContext.build(db=db, settings=settings)
    try:
        hunter = await ctx.get_amazon_hunter()
        print(f"  hunter type   = {type(hunter).__name__}")
        print(f"  hunter module = {type(hunter).__module__}")
    except Exception as exc:
        print(f"  hunter error: {type(exc).__name__}: {exc}")
    finally:
        await ctx.aclose()

asyncio.run(main())
PY

echo
echo "=== Ultimos captchas Amazon en runtime_events ==="
sqlite3 -readonly data/ofertas_hunter.db <<'SQL'
.mode line
SELECT substr(created_at,1,19) AS ts,
       kind,
       substr(payload_json, 1, 300) AS payload
FROM runtime_events
WHERE kind LIKE '%captcha%'
ORDER BY id DESC
LIMIT 5;
SQL

echo
echo "=== Ultimos eventos amazon_legacy_captcha ==="
sqlite3 -readonly data/ofertas_hunter.db <<'SQL'
SELECT substr(created_at,1,19) AS ts, kind, substr(payload_json,1,200) AS p
FROM runtime_events
WHERE kind LIKE '%amazon_legacy%'
ORDER BY id DESC
LIMIT 5;
SQL
