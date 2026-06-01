#!/usr/bin/env bash
# Smoke-test end-to-end del flag AMAZON_HUNTER_LEGACY en VPS.
# Confirma que get_amazon_hunter devuelve LegacyAmazonHunterAgent
# cuando el flag está ON.

set -uo pipefail
cd /opt/deal-agent/ofertas_hunter

echo "=== Sin flag (default) ==="
unset AMAZON_HUNTER_LEGACY
./.venv/bin/python - <<'PY'
import asyncio
from ofertas_hunter.config import get_settings
from ofertas_hunter.db import init_db, connect
from ofertas_hunter.mcp.context import ServerContext

async def main():
    init_db()
    db = connect()
    settings = get_settings()
    print(f"  amazon_hunter_legacy = {settings.amazon_hunter_legacy}")
    ctx = ServerContext.build(db=db, settings=settings)
    try:
        hunter = await ctx.get_amazon_hunter()
        print(f"  hunter type = {type(hunter).__name__}")
    except Exception as exc:
        print(f"  hunter init error (esperado si no hay browser): {type(exc).__name__}: {exc}")
    finally:
        await ctx.aclose()

asyncio.run(main())
PY

echo
echo "=== Con AMAZON_HUNTER_LEGACY=true ==="
export AMAZON_HUNTER_LEGACY=true
./.venv/bin/python - <<'PY'
import asyncio
from ofertas_hunter.config import get_settings
from ofertas_hunter.db import init_db, connect
from ofertas_hunter.mcp.context import ServerContext

async def main():
    init_db()
    db = connect()
    settings = get_settings()
    print(f"  amazon_hunter_legacy = {settings.amazon_hunter_legacy}")
    ctx = ServerContext.build(db=db, settings=settings)
    try:
        hunter = await ctx.get_amazon_hunter()
        print(f"  hunter type   = {type(hunter).__name__}")
        print(f"  hunter module = {type(hunter).__module__}")
    finally:
        await ctx.aclose()

asyncio.run(main())
PY
