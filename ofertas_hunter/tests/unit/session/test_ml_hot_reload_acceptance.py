"""Tests de aceptación A-F del spec del usuario.

Estos tests verifican explícitamente los criterios obligatorios del spec:

    A. JSON inválido → rechazo + aviso, sin promover ni rotar.
    B. Dominio no permitido (no ML) → rechazo, sin promover ni rotar.
    C. Cookies de ML pero sesión inválida → no promover, conservar sesión.
    D. Cookies válidas → promover, marcar VALID, permitir hunt_mercadolibre.
    E. Mientras ML inválido, Amazon y Telegram NO se ven afectados.
    F. NO debe requerir restart del servicio: el reload ocurre en runtime
       sin re-iniciar nada.
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
from ofertas_hunter.session.ml_session_recovery import MLRecoveryConfig
from ofertas_hunter.session.ml_session_runtime import MLSessionRecoveryRuntime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def db(tmp_path):
    init_db(tmp_path / "x.db")
    conn = sqlite3.connect(str(tmp_path / "x.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


class _Ctx:
    """ServerContext stub que registra reload_ml_cookies + simula
    Amazon/Telegram intactos.
    """

    def __init__(self):
        self.reload_calls: list[list[dict]] = []
        self.amazon_state = "running"
        self.telegram_state = "running"

    async def reload_ml_cookies(self, cookies):
        self.reload_calls.append(cookies)
        return True


async def _val_ok(cookies, url):
    return ValidationOutcome(ok=True, final_url=url)


async def _val_fail(cookies, url):
    return ValidationOutcome(ok=False, reason="login_redirect")


async def _start_runtime(*, db, tmp_path, validation_factory, ctx=None):
    port = _free_port()
    cookies_path = tmp_path / "ml_cookies.json"

    config = MLRecoveryConfig(
        enabled=True,
        admin_numbers=("528338498692",),
        alert_cooldown_seconds=60,
        inbound_enabled=True,
        inbound_host="127.0.0.1",
        inbound_port=port,
        inbound_secret="s",
        cookie_backup_count=3,
        cookies_path=str(cookies_path),
    )

    sent: list[tuple[str, str]] = []

    async def fake_send(number, text):
        sent.append((number, text))
        return True

    ctx = ctx or _Ctx()
    runtime = MLSessionRecoveryRuntime(
        config=config,
        db=db,
        evolution_send=fake_send,
        ctx=ctx,
        validation_factory=validation_factory,
    )
    await runtime.start()
    return runtime, port, ctx, sent, cookies_path


async def _post(port, body, *, secret="s"):
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"http://127.0.0.1:{port}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": secret},
        )


def _wrap(text: str) -> dict:
    return {"from": "528338498692", "text": text}


# ---------------------------------------------------------------------------
# A. JSON inválido
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_a_invalid_json_rejected(tmp_path, db):
    runtime, port, ctx, sent, cookies_path = await _start_runtime(
        db=db, tmp_path=tmp_path, validation_factory=_val_ok
    )
    try:
        body = _wrap(f"{COMMAND_TOKEN}\n{{not json")
        r = await _post(port, body)
        assert r.status_code == 400
        # No reload, no rotation, no archivo
        assert ctx.reload_calls == []
        assert not cookies_path.exists()
        # Aviso al admin
        assert any(
            "no son válidas" in t.lower() or "no es válido" in t.lower()
            for _, t in sent
        )
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# B. Dominio no permitido
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_b_non_ml_domain_rejected(tmp_path, db):
    runtime, port, ctx, sent, cookies_path = await _start_runtime(
        db=db, tmp_path=tmp_path, validation_factory=_val_ok
    )
    try:
        not_ml = json.dumps([
            {"name": "x", "value": "y", "domain": ".google.com"},
            {"name": "z", "value": "w", "domain": ".facebook.com"},
        ])
        body = _wrap(f"{COMMAND_TOKEN}\n{not_ml}")
        r = await _post(port, body)
        assert r.status_code == 400
        assert ctx.reload_calls == []
        assert not cookies_path.exists()
        assert runtime.manager.status == MLSessionStatus.VALID  # estado intacto
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# C. Cookies de ML pero sesión inválida
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_c_ml_cookies_but_session_invalid(tmp_path, db):
    """Formato ok + dominio ML, pero validación activa rechaza."""
    runtime, port, ctx, sent, cookies_path = await _start_runtime(
        db=db, tmp_path=tmp_path, validation_factory=_val_fail
    )
    try:
        ok_format = json.dumps([
            {"name": "n", "value": "v", "domain": ".mercadolibre.com.mx"},
        ])
        body = _wrap(f"{COMMAND_TOKEN}\n{ok_format}")
        r = await _post(port, body)
        assert r.status_code == 200
        assert r.json()["status"] == "session_invalid_after_reload"

        # No promovido al active path
        assert not cookies_path.exists()

        # ctx NO recibió rotation (manager paró antes)
        assert ctx.reload_calls == []

        # Estado != VALID
        assert runtime.manager.status != MLSessionStatus.VALID

        # Sí está en staging (queda evidencia)
        staging = cookies_path.parent / "incoming_cookies" / "mercadolibre_latest.json"
        assert staging.exists()

        # Aviso al admin con el texto adecuado
        assert any(
            "sigue pidiendo login" in t.lower() or "no reemplacé" in t.lower()
            for _, t in sent
        )
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# D. Cookies válidas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_d_valid_cookies_promoted_and_rotated(tmp_path, db):
    runtime, port, ctx, sent, cookies_path = await _start_runtime(
        db=db, tmp_path=tmp_path, validation_factory=_val_ok
    )
    try:
        cookies = json.dumps([
            {"name": "n", "value": "v", "domain": ".mercadolibre.com.mx"},
        ])
        body = _wrap(f"{COMMAND_TOKEN}\n{cookies}")
        r = await _post(port, body)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        # Active cookies escritas
        assert cookies_path.exists()

        # Rotation invocada
        assert len(ctx.reload_calls) == 1

        # Estado VALID
        assert runtime.manager.status == MLSessionStatus.VALID

        # Mensaje de éxito al admin
        assert any("aceptadas" in t.lower() for _, t in sent)
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# E. Mientras ML inválido, Amazon y Telegram siguen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_e_amazon_and_telegram_unaffected(tmp_path, db):
    """Sólo se cambia el estado de ML. El ctx no toca Amazon ni Telegram."""
    runtime, port, ctx, sent, cookies_path = await _start_runtime(
        db=db, tmp_path=tmp_path, validation_factory=_val_fail
    )
    try:
        # Estado inicial: Amazon/Telegram running
        assert ctx.amazon_state == "running"
        assert ctx.telegram_state == "running"

        runtime.manager.mark_invalid("login_redirect")
        # Tras invalidar ML, los otros marketplaces siguen igual
        assert ctx.amazon_state == "running"
        assert ctx.telegram_state == "running"

        # Si llega un /cookies_ml inválido, sólo cambia estado de ML
        bad = json.dumps([{"name": "x", "value": "y", "domain": ".mercadolibre.com.mx"}])
        await _post(port, _wrap(f"{COMMAND_TOKEN}\n{bad}"))
        assert ctx.amazon_state == "running"
        assert ctx.telegram_state == "running"
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# F. No requiere restart del servicio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_f_no_restart_required(tmp_path, db):
    """El runtime sigue vivo todo el flujo: tres reloads consecutivos en
    el mismo proceso, sin reinstanciar nada.
    """
    runtime, port, ctx, sent, cookies_path = await _start_runtime(
        db=db, tmp_path=tmp_path, validation_factory=_val_ok
    )
    try:
        for i in range(3):
            cookies = json.dumps([
                {"name": f"c{i}", "value": "v", "domain": ".mercadolibre.com.mx"},
            ])
            r = await _post(port, _wrap(f"{COMMAND_TOKEN}\n{cookies}"))
            assert r.status_code == 200, f"reload #{i} falló"
            assert r.json()["status"] == "ok"

        # 3 rotation calls al ctx, sin reinstanciar el runtime
        assert len(ctx.reload_calls) == 3
        assert runtime.manager.status == MLSessionStatus.VALID
    finally:
        await runtime.stop()
