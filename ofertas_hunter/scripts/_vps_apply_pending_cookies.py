"""Procesa AHORA cualquier /cookies_ml o JSON-ML del admin que esté en
Evolution API, sin esperar al poller del bot.

Uso (en el VPS, con el bot detenido o corriendo):

    .venv/bin/python scripts/_vps_apply_pending_cookies.py [--debug]

Aplica el pipeline completo: stage → validate (Chromium temporal) →
promote → emit runtime_event. NO necesita el browser persistente del
bot; sólo necesita Playwright instalado.
"""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

# Permitir imports del paquete del bot
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ofertas_hunter.config import get_settings  # noqa: E402
from ofertas_hunter.db import connect, init_db  # noqa: E402
from ofertas_hunter.publishing.evolution_client import EvolutionClient  # noqa: E402
from ofertas_hunter.session.ml_session_inbound import (  # noqa: E402
    COMMAND_TOKEN,
    extract_attachment_json,
    looks_like_cookies_json,
    parse_command_and_payload,
)
from ofertas_hunter.session.ml_session_manager import (  # noqa: E402
    MLSessionPaths,
    MercadoLibreSessionManager,
)
from ofertas_hunter.session.ml_session_recovery import (  # noqa: E402
    MLCookieValidator,
    MLRecoveryConfig,
    build_admin_success_message,
    build_admin_validation_error_message,
    build_admin_validation_failed_session_message,
)


DEBUG = "--debug" in sys.argv


def _digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def _extract_text(m: dict) -> str:
    msg = m.get("message") or {}
    return (
        msg.get("conversation")
        or msg.get("extendedTextMessage", {}).get("text")
        or msg.get("imageMessage", {}).get("caption")
        or msg.get("documentMessage", {}).get("caption")
        or ""
    )


async def main() -> int:
    s = get_settings()
    init_db()
    conn = connect()

    rec_cfg = MLRecoveryConfig.from_settings(s)
    if not rec_cfg.admin_numbers:
        print("ERROR: ML_SESSION_ADMIN_NUMBERS vacío en .env")
        return 2

    print(f"Admin configurado(s): {rec_cfg.admin_numbers}")
    print(f"Base: {s.evolution_base_url}  Instance: {s.evolution_instance}")
    print()

    import httpx

    async with httpx.AsyncClient(
        timeout=15.0, headers={"apikey": s.evolution_api_key}
    ) as client:
        url = f"{s.evolution_base_url.rstrip('/')}/chat/findMessages/{s.evolution_instance}"
        r = await client.post(
            url,
            json={
                "where": {"key": {"fromMe": False}},
                "limit": 100,
                "order": "desc",
            },
        )
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:200]}")
            return 3
        records = (
            r.json().get("messages", {}).get("records", [])
            if isinstance(r.json(), dict)
            else []
        )

    print(f"Total mensajes recibidos del API: {len(records)}")

    # Filtrar:
    # - admin (acepta JID, LID y número con prefijo `1`)
    # - NOT fromMe (por si el filtro del where no se respetó)
    admin_msgs = []
    fromme_skipped = 0
    for m in records:
        key = m.get("key", {}) or {}
        if key.get("fromMe") is True:
            fromme_skipped += 1
            continue
        rj = key.get("remoteJid") or ""
        # Pasamos el JID completo (incluyendo `@lid` o `@s.whatsapp.net`)
        # para que `is_admin` decida según el formato configurado.
        if rec_cfg.is_admin(rj):
            admin_msgs.append((rj, m))

    print(
        f"Filtrados: fromMe=true→ {fromme_skipped} ignorados | "
        f"admin inbound → {len(admin_msgs)}"
    )
    admin_msgs.sort(key=lambda x: int(x[1].get("messageTimestamp") or 0))

    if DEBUG:
        print("\n=== Mensajes admin (debug) ===")
        for i, (fn, m) in enumerate(admin_msgs):
            ts = m.get("messageTimestamp")
            mtype = m.get("messageType")
            text = _extract_text(m)
            attach = extract_attachment_json({"data": m})
            preview = (text or "").replace("\n", " | ")[:120]
            print(
                f"  [{i}] ts={ts} type={mtype} from={fn} "
                f"attach={'YES' if attach else 'no'}"
            )
            print(f"      text: {preview!r}")
            if attach:
                print(f"      attach_preview: {attach[:120]!r}")
        print()

    candidates = []
    for from_id, m in admin_msgs:
        text = _extract_text(m)
        attach = extract_attachment_json({"data": m})
        cmd, rest = parse_command_and_payload(text)
        body = None
        src = None
        if attach:
            body = attach.strip()
            src = "attachment"
        elif cmd == COMMAND_TOKEN and rest and rest.strip():
            body = rest.strip()
            src = "inline_command"
        elif looks_like_cookies_json(text):
            body = text.strip()
            src = "auto_detected"
        if body:
            candidates.append((from_id, m, body, src))

    print(f"Candidatos con cookies ML: {len(candidates)}")

    if not candidates:
        print("\nNada que aplicar.")
        if not DEBUG:
            print("Si esperabas que hubiera, vuelve a correr con --debug.")
        return 0

    from_id, msg, body, src = candidates[-1]
    print(
        f"\nProcesando el más reciente: from_id={from_id} "
        f"ts={msg.get('messageTimestamp')} source={src} bytes={len(body)}"
    )

    # Destino para responder al admin (Evolution no acepta `@lid`).
    reply_to = rec_cfg.reply_target(from_id) or from_id
    print(f"reply_to: {reply_to}")

    validator = MLCookieValidator()
    result = validator.validate(body)
    if not result.ok:
        print(f"VALIDACIÓN FORMATO FALLÓ: {result.reason}")
        async with EvolutionClient(
            base_url=s.evolution_base_url,
            api_key=s.evolution_api_key,
            instance=s.evolution_instance,
            dry_run=False,
        ) as ev:
            await ev.send_text(reply_to, build_admin_validation_error_message(result.reason or "?")
            )
        return 4

    print(f"Formato OK: {result.sanitized_count} cookies")

    secrets_dir = Path(s.mercadolibre_cookies_path).resolve().parent
    paths = MLSessionPaths(
        staging_path=secrets_dir / "incoming_cookies" / "mercadolibre_latest.json",
        active_cookies_path=Path(s.mercadolibre_cookies_path).resolve(),
        profile_cookies_path=secrets_dir
        / "browser_profiles"
        / "mercadolibre"
        / "cookies.json",
    )

    manager = MercadoLibreSessionManager(
        db=conn, paths=paths, rotation_callback=None
    )

    print("Pipeline: stage → validate (Chromium efímero) → promote ...")
    ok, outcome = await manager.reload_from_cookies(result.cookies)
    print()
    print(f"Resultado pipeline: ok={ok}")
    print(
        f"Validation: ok={getattr(outcome, 'ok', None)} "
        f"reason={getattr(outcome, 'reason', None)} "
        f"final_url={getattr(outcome, 'final_url', None)}"
    )
    print(f"Active cookies path: {paths.active_cookies_path}")
    print(f"Active exists: {paths.active_cookies_path.exists()}")

    async with EvolutionClient(
        base_url=s.evolution_base_url,
        api_key=s.evolution_api_key,
        instance=s.evolution_instance,
        dry_run=False,
    ) as ev:
        if ok:
            await ev.send_text(reply_to, build_admin_success_message(len(result.cookies))
            )
            print("\n>> Confirmación enviada al admin: cookies aceptadas.")
        else:
            reason = getattr(outcome, "reason", None) or "validation_failed"
            await ev.send_text(reply_to, build_admin_validation_failed_session_message(reason)
            )
            print("\n>> Aviso enviado al admin: sesión sigue inválida.")

    conn.close()
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
