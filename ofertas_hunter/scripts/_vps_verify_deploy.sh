#!/usr/bin/env bash
# Verificación post-deploy: flag legacy, módulos ML Session Recovery,
# configs cargables.

set -uo pipefail
cd /opt/deal-agent/ofertas_hunter

echo "=== .env relevante ==="
grep -E '^AMAZON_HUNTER_LEGACY|^ML_SESSION' .env 2>/dev/null || echo "(sin claves ML_SESSION_*)"

echo
echo "=== Archivos session ML ==="
ls -la src/ofertas_hunter/session/ml_session_*.py 2>/dev/null

echo
echo "=== MLRecoveryConfig.from_settings ==="
./.venv/bin/python - <<'PY'
from ofertas_hunter.session.ml_session_runtime import MLSessionRecoveryRuntime
from ofertas_hunter.session.ml_session_recovery import MLRecoveryConfig
from ofertas_hunter.config import get_settings

cfg = MLRecoveryConfig.from_settings(get_settings())
print(f"  enabled         = {cfg.enabled}")
print(f"  admin_numbers   = {cfg.admin_numbers}")
print(f"  cooldown_secs   = {cfg.alert_cooldown_seconds}")
print(f"  inbound_enabled = {cfg.inbound_enabled}")
print(f"  inbound_host    = {cfg.inbound_host}")
print(f"  inbound_port    = {cfg.inbound_port}")
print(f"  inbound_secret  = {'***' if cfg.inbound_secret else '(vacio)'}")
print(f"  cookies_path    = {cfg.cookies_path}")
print(f"  backup_count    = {cfg.cookie_backup_count}")
PY

echo
echo "=== get_amazon_hunter switch end-to-end ==="
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
    except Exception as exc:
        print(f"  hunter error: {type(exc).__name__}: {exc}")
    finally:
        await ctx.aclose()

asyncio.run(main())
PY
