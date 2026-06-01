"""Tests del wrapper `MLSessionRecoveryRuntime` que orquesta los 4
componentes (manager + monitor + reloader + inbound) y se integra con el ctx.

Cubre el flujo end-to-end: cookie_expiry → alerta WhatsApp →
admin manda ``/cookies_ml`` + JSON → server lo recibe → manager valida en
Chromium temporal (mockeado) → promociona y rota → ctx.reload_ml_cookies.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.session.ml_session_inbound import COMMAND_TOKEN
from ofertas_hunter.session.ml_session_manager import (
    MLSessionStatus,
    ValidationOutcome,
)
from ofertas_hunter.session.ml_session_recovery import (
    ALERT_KIND,
    MLRecoveryConfig,
    RELOAD_OK_KIND,
)
from ofertas_hunter.session.ml_session_runtime import (
    MLSessionRecoveryRuntime,
)


def _emit_event(db, kind: str, severity: str = "warning"):
    from datetime import datetime, timezone

    db.execute(
        "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            kind,
            severity,
            "{}",
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        ),
    )
    db.commit()


def _free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


class FakeServerContext:
    """Stub mínimo del ServerContext con `reload_ml_cookies(cookies)`."""

    def __init__(self):
        self.reload_called_with: list[list[dict]] = []
        self.reload_should_succeed = True
        self.paused = True

    async def reload_ml_cookies(self, cookies: list[dict]) -> bool:
        self.reload_called_with.append(cookies)
        self.paused = False
        return self.reload_should_succeed


async def _validation_factory_ok(cookies, url):
    return ValidationOutcome(ok=True, final_url=url, detected_signals=("mock_ok",))


async def _validation_factory_fail(cookies, url):
    return ValidationOutcome(
        ok=False, reason="login_redirect", final_url=url + "/login"
    )


@pytest.mark.asyncio
async def test_e2e_recovery_flow(tmp_path, db):
    """Flow E2E: expiry → alert → /cookies_ml → validate ok → reload → unpause."""
    port = _free_port()
    cookies_path = str(tmp_path / "ml_cookies.json")

    config = MLRecoveryConfig(
        enabled=True,
        admin_numbers=("528338498692",),
        alert_cooldown_seconds=60,
        inbound_enabled=True,
        inbound_host="127.0.0.1",
        inbound_port=port,
        inbound_secret="testsecret",
        cookie_backup_count=3,
        cookies_path=cookies_path,
    )

    sent: list[tuple[str, str]] = []

    async def fake_send(number, text):
        sent.append((number, text))
        return True

    ctx = FakeServerContext()
    runtime = MLSessionRecoveryRuntime(
        config=config,
        db=db,
        evolution_send=fake_send,
        ctx=ctx,
        validation_factory=_validation_factory_ok,
    )
    await runtime.start()
    try:
        # Paso 1: simular cookie_expiry
        _emit_event(db, "cookie_expiry")
        result = await runtime.monitor_tick()
        assert result == ALERT_KIND
        assert len(sent) == 1
        assert "Mercado Libre" in sent[0][1]

        # Paso 2: admin responde con /cookies_ml + cookies
        valid_cookies = json.dumps([
            {"name": "_d2id", "value": "abc", "domain": ".mercadolibre.com.mx"},
        ])
        body = {"from": "528338498692", "text": f"{COMMAND_TOKEN}\n{valid_cookies}"}

        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{port}/wa/inbound",
                json=body,
                headers={"X-Webhook-Secret": "testsecret"},
            )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        # Paso 3: ctx.reload_ml_cookies fue llamado (vía manager.rotate_context)
        assert len(ctx.reload_called_with) == 1
        assert ctx.reload_called_with[0][0]["name"] == "_d2id"
        assert ctx.paused is False

        # Paso 4: archivo escrito en disco (active path)
        saved = json.loads(Path(cookies_path).read_text(encoding="utf-8"))
        assert saved[0]["name"] == "_d2id"

        # Paso 5: manager en estado VALID
        assert runtime.manager.status == MLSessionStatus.VALID

        # Paso 6: mensaje de confirmación enviado al admin
        success_msgs = [t for _, t in sent if "aceptadas" in t.lower()]
        assert len(success_msgs) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_skips_inbound_when_disabled(db, tmp_path):
    """Si `inbound_enabled=False`, el server NO arranca."""
    config = MLRecoveryConfig(
        enabled=True,
        admin_numbers=("528338498692",),
        inbound_enabled=False,
        cookies_path=str(tmp_path / "ml_cookies.json"),
    )

    async def fake_send(number, text):
        return True

    ctx = FakeServerContext()
    runtime = MLSessionRecoveryRuntime(
        config=config,
        db=db,
        evolution_send=fake_send,
        ctx=ctx,
        validation_factory=_validation_factory_ok,
    )
    await runtime.start()
    try:
        assert runtime.server.is_running is False
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_handles_ctx_without_reload_method(db, tmp_path):
    """Si el ctx NO tiene `reload_ml_cookies`, el manager promueve cookies
    igual y devuelve ok (las cookies quedan a disposición del próximo
    arranque del browser).
    """
    port = _free_port()
    cookies_path = str(tmp_path / "ml_cookies.json")

    config = MLRecoveryConfig(
        enabled=True,
        admin_numbers=("528338498692",),
        inbound_enabled=True,
        inbound_host="127.0.0.1",
        inbound_port=port,
        inbound_secret="s",
        cookies_path=cookies_path,
    )

    async def fake_send(number, text):
        return True

    class CtxNoReload:
        pass

    runtime = MLSessionRecoveryRuntime(
        config=config,
        db=db,
        evolution_send=fake_send,
        ctx=CtxNoReload(),
        validation_factory=_validation_factory_ok,
    )
    await runtime.start()
    try:
        valid = json.dumps([
            {"name": "x", "value": "y", "domain": ".mercadolibre.com.mx"},
        ])
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{port}/wa/inbound",
                json={
                    "from": "528338498692",
                    "text": f"{COMMAND_TOKEN}\n{valid}",
                },
                headers={"X-Webhook-Secret": "s"},
            )
        assert r.status_code == 200
        # active cookies promoted
        assert Path(cookies_path).exists()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_telegram_mode_skips_whatsapp_inbound_and_uses_telegram_poller(
    db, tmp_path
):
    config = MLRecoveryConfig(
        enabled=True,
        alert_channel="telegram",
        inbound_enabled=True,
        telegram_bot_token="bot-token",
        telegram_chat_id="5054325626",
        cookies_path=str(tmp_path / "ml_cookies.json"),
    )

    async def fake_send(number, text):
        return True

    class _FakeTelegramPoller:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    runtime = MLSessionRecoveryRuntime(
        config=config,
        db=db,
        evolution_send=fake_send,
        ctx=FakeServerContext(),
        validation_factory=_validation_factory_ok,
    )
    runtime.telegram_poller = _FakeTelegramPoller()
    await runtime.start()
    try:
        assert runtime.server.is_running is False
        assert runtime.telegram_poller.started is True
    finally:
        await runtime.stop()
        assert runtime.telegram_poller.stopped is True


@pytest.mark.asyncio
async def test_runtime_start_tolerates_none_ctx_in_orchestrator_mode(db, tmp_path):
    config = MLRecoveryConfig(
        enabled=True,
        alert_channel="telegram",
        inbound_enabled=False,
        telegram_bot_token="bot-token",
        telegram_chat_id="5054325626",
        cookies_path=str(tmp_path / "ml_cookies.json"),
    )

    async def fake_send(number, text):
        return True

    class _FakeTelegramPoller:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    runtime = MLSessionRecoveryRuntime(
        config=config,
        db=db,
        evolution_send=fake_send,
        ctx=None,
        validation_factory=_validation_factory_ok,
    )
    runtime.telegram_poller = _FakeTelegramPoller()

    await runtime.start()
    try:
        assert runtime.telegram_poller.started is True
    finally:
        await runtime.stop()
        assert runtime.telegram_poller.stopped is True


@pytest.mark.asyncio
async def test_runtime_validation_failure_does_not_promote(tmp_path, db):
    """Test obligatorio C: cookies con formato OK pero sesión inválida.

    Si el validation_factory devuelve ``ok=False``, el manager NO debe:

    - escribir las cookies al archivo activo
    - llamar a ``ctx.reload_ml_cookies``
    - dejar el estado en VALID
    """
    port = _free_port()
    active_path = tmp_path / "ml_cookies.json"
    # Sembrar cookies "viejas" en el activo
    active_path.write_text(
        json.dumps([{"name": "OLD", "value": "v", "domain": ".mercadolibre.com.mx"}]),
        encoding="utf-8",
    )

    config = MLRecoveryConfig(
        enabled=True,
        admin_numbers=("528338498692",),
        inbound_enabled=True,
        inbound_host="127.0.0.1",
        inbound_port=port,
        inbound_secret="s",
        cookies_path=str(active_path),
    )

    async def fake_send(number, text):
        return True

    ctx = FakeServerContext()
    runtime = MLSessionRecoveryRuntime(
        config=config,
        db=db,
        evolution_send=fake_send,
        ctx=ctx,
        validation_factory=_validation_factory_fail,
    )
    await runtime.start()
    try:
        valid = json.dumps([
            {"name": "NEW", "value": "y", "domain": ".mercadolibre.com.mx"},
        ])
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{port}/wa/inbound",
                json={
                    "from": "528338498692",
                    "text": f"{COMMAND_TOKEN}\n{valid}",
                },
                headers={"X-Webhook-Secret": "s"},
            )

        assert r.status_code == 200
        # No "ok"; el sistema reporta sesión inválida tras reload.
        assert r.json()["status"] == "session_invalid_after_reload"

        # Las cookies activas NO fueron sobreescritas.
        on_disk = json.loads(active_path.read_text(encoding="utf-8"))
        assert on_disk[0]["name"] == "OLD"

        # ctx NO recibió reload (manager no lo llamó porque validate
        # falló antes de promote/rotate).
        assert len(ctx.reload_called_with) == 0

        # Manager NO está VALID
        assert runtime.manager.status != MLSessionStatus.VALID

        # Pero las cookies SI están en staging (incoming_cookies/...)
        staging = active_path.parent / "incoming_cookies" / "mercadolibre_latest.json"
        assert staging.exists()
        staged = json.loads(staging.read_text(encoding="utf-8"))
        assert staged[0]["name"] == "NEW"
    finally:
        await runtime.stop()
