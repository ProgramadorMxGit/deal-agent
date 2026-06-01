"""Tests del MLEvolutionPoller (pull contra Evolution API)."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.session.ml_session_inbound import COMMAND_TOKEN
from ofertas_hunter.session.ml_session_poller import (
    MLEvolutionPoller,
    MLPollerConfig,
    POLLER_KIND_PROCESSED,
)
from ofertas_hunter.session.ml_session_recovery import (
    MLCookieValidator,
    MLRecoveryConfig,
    RECEIVED_KIND,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "x.db"
    init_db(p)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _make_response(records: list[dict]) -> dict:
    return {
        "messages": {
            "total": len(records),
            "pages": 1,
            "currentPage": 1,
            "records": records,
        }
    }


def _msg(*, mid: str, ts: int, text: str, from_num: str = "528338498692") -> dict:
    return {
        "id": mid,
        "key": {
            "id": mid,
            "fromMe": False,
            "remoteJid": f"{from_num}@s.whatsapp.net",
        },
        "messageType": "conversation",
        "messageTimestamp": ts,
        "message": {"conversation": text},
    }


def _build(db, transport: httpx.MockTransport, *, on_cookies, sent: list):
    rec_cfg = MLRecoveryConfig(
        enabled=True,
        admin_numbers=("528338498692",),
        inbound_enabled=False,
        cookies_path="/tmp/x.json",
    )
    pol_cfg = MLPollerConfig(
        enabled=True,
        base_url="http://evo.test",
        api_key="K",
        instance="inst",
        poll_interval_seconds=1,
        page_size=20,
    )

    async def fake_send(num, text):
        sent.append((num, text))
        return True

    client = httpx.AsyncClient(transport=transport, headers={"apikey": "K"})
    poller = MLEvolutionPoller(
        recovery_config=rec_cfg,
        poller_config=pol_cfg,
        validator=MLCookieValidator(),
        on_cookies=on_cookies,
        evolution_send=fake_send,
        db=db,
        http_client=client,
    )
    return poller


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poller_processes_cookies_ml_command(db):
    """Test obligatorio: si llega un mensaje con /cookies_ml + JSON ML
    válido, el poller invoca on_cookies y manda confirmación."""
    cookies_text = json.dumps([
        {"name": "x", "value": "y", "domain": ".mercadolibre.com.mx"},
    ])

    # Primera llamada: vacío (init cursor), segunda: el mensaje nuevo.
    state = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        assert "/chat/findMessages/inst" in req.url.path
        if state["calls"] == 1:
            return httpx.Response(200, json=_make_response([
                _msg(mid="OLD", ts=1000, text="hola"),
            ]))
        return httpx.Response(200, json=_make_response([
            _msg(
                mid="NEW1",
                ts=2000,
                text=f"{COMMAND_TOKEN}\n{cookies_text}",
            ),
            _msg(mid="OLD", ts=1000, text="hola"),  # ya cursoreado
        ]))

    received: list[list[dict]] = []
    sent: list = []

    async def on_cookies(cookies):
        received.append(cookies)
        return True

    poller = _build(db, httpx.MockTransport(handler), on_cookies=on_cookies, sent=sent)
    try:
        await poller.start()
        # Esperar 2 ticks reales
        await asyncio.sleep(1.6)
    finally:
        await poller.stop()

    assert len(received) == 1
    assert received[0][0]["name"] == "x"
    # Mensaje de éxito al admin
    assert any("aceptadas" in t.lower() for _, t in sent)

    # Eventos emitidos
    kinds = {
        r["kind"]
        for r in db.execute(
            "SELECT kind FROM runtime_events WHERE kind LIKE 'ml_%'"
        ).fetchall()
    }
    assert RECEIVED_KIND in kinds
    assert POLLER_KIND_PROCESSED in kinds


@pytest.mark.asyncio
async def test_poller_ignores_messages_without_command(db):
    state = {"calls": 0}

    def handler(req):
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(200, json=_make_response([
                _msg(mid="OLD", ts=1000, text="x"),
            ]))
        return httpx.Response(200, json=_make_response([
            _msg(mid="NEW", ts=2000, text="hola, sólo un saludo"),
            _msg(mid="OLD", ts=1000, text="x"),
        ]))

    received: list = []
    sent: list = []

    async def on_cookies(c):
        received.append(c)
        return True

    poller = _build(db, httpx.MockTransport(handler), on_cookies=on_cookies, sent=sent)
    try:
        await poller.start()
        await asyncio.sleep(1.6)
    finally:
        await poller.stop()

    assert received == []
    assert sent == []


@pytest.mark.asyncio
async def test_poller_ignores_non_admin_messages(db):
    cookies_text = json.dumps([
        {"name": "x", "value": "y", "domain": ".mercadolibre.com.mx"},
    ])
    state = {"calls": 0}

    def handler(req):
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(200, json=_make_response([]))
        return httpx.Response(200, json=_make_response([
            _msg(
                mid="NEW1",
                ts=2000,
                text=f"{COMMAND_TOKEN}\n{cookies_text}",
                from_num="529999999999",  # NO admin
            ),
        ]))

    received: list = []
    sent: list = []

    async def on_cookies(c):
        received.append(c)
        return True

    poller = _build(db, httpx.MockTransport(handler), on_cookies=on_cookies, sent=sent)
    try:
        await poller.start()
        await asyncio.sleep(1.6)
    finally:
        await poller.stop()

    assert received == []  # NO procesado


@pytest.mark.asyncio
async def test_poller_resilient_to_evolution_errors(db):
    """Si Evolution responde 500, el poller no crashea."""
    state = {"calls": 0}

    def handler(req):
        state["calls"] += 1
        return httpx.Response(500, text="boom")

    received: list = []
    sent: list = []

    async def on_cookies(c):
        received.append(c)
        return True

    poller = _build(db, httpx.MockTransport(handler), on_cookies=on_cookies, sent=sent)
    try:
        await poller.start()
        await asyncio.sleep(1.6)
    finally:
        await poller.stop()

    # El poller siguió vivo a pesar del error
    assert received == []
    # Hubo algún error registrado
    rows = db.execute(
        "SELECT 1 FROM runtime_events WHERE kind = 'ml_poller_error'"
    ).fetchall()
    assert len(rows) >= 1
