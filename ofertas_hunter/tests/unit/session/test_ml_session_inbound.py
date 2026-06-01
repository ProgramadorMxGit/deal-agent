"""Tests del webhook HTTP entrante para ML session recovery.

Cubre:

- Auth via header X-Webhook-Secret.
- Filtro por número admin.
- Validación del body (formato Evolution variantes).
- Comando explícito ``/cookies_ml`` (sin él, los mensajes se ignoran).
- Soporte de attachment ``.json`` (documentMessage con base64).
- Flujo end-to-end: ``/cookies_ml`` + JSON → reload callback → success.
- Rechazo de payloads sin comando o con cuerpo vacío.
- Healthcheck GET.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.session.ml_session_inbound import (
    COMMAND_TOKEN,
    MLInboundServer,
    extract_admin_payload,
    extract_attachment_json,
    parse_command_and_payload,
)
from ofertas_hunter.session.ml_session_recovery import (
    MLCookieValidator,
    MLRecoveryConfig,
    RECEIVED_KIND,
    VALIDATED_KIND,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def cookies_path(tmp_path):
    return str(tmp_path / "ml_cookies.json")


def _free_port() -> int:
    """Obtiene un puerto libre para evitar choques en CI."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def running_server(db, cookies_path):
    """Lanza el servidor con dependencias stub controlables por el test."""
    port = _free_port()
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

    cookies_received: list[list[dict]] = []
    sent_messages: list[tuple[str, str]] = []

    async def on_cookies(cookies):
        cookies_received.append(cookies)
        return True

    async def evolution_send(number, text):
        sent_messages.append((number, text))
        return True

    server = MLInboundServer(
        config=config,
        validator=MLCookieValidator(),
        on_cookies=on_cookies,
        evolution_send=evolution_send,
        db=db,
    )
    await server.start()
    yield {
        "server": server,
        "url": f"http://127.0.0.1:{port}",
        "cookies_received": cookies_received,
        "sent_messages": sent_messages,
    }
    await server.stop()


def _wrap_text(text: str) -> dict:
    """Helper: payload simple ``{from, text}``."""
    return {"from": "528338498692", "text": text}


def _wrap_with_command(json_body: str) -> dict:
    return _wrap_text(f"{COMMAND_TOKEN}\n{json_body}")


# ---------------------------------------------------------------------------
# extract_admin_payload (parser de payloads de Evolution)
# ---------------------------------------------------------------------------


def test_extract_payload_plain_format():
    body = {"from": "+528338498692", "text": "[]"}
    f, t = extract_admin_payload(body)
    assert f == "528338498692"
    assert t == "[]"


def test_extract_payload_evolution_v2_format():
    body = {
        "key": {"remoteJid": "528338498692@s.whatsapp.net"},
        "message": {"conversation": "hello"},
    }
    f, t = extract_admin_payload(body)
    # extract_admin_payload ahora preserva el JID/LID completo para que
    # el filtro de admin pueda distinguir formatos anonimizados.
    assert f == "528338498692@s.whatsapp.net"
    assert t == "hello"


def test_extract_payload_evolution_wrapped_in_data():
    body = {
        "event": "messages.upsert",
        "data": {
            "key": {"remoteJid": "528338498692@s.whatsapp.net"},
            "message": {"extendedTextMessage": {"text": "[\"x\"]"}},
        },
    }
    f, t = extract_admin_payload(body)
    assert f == "528338498692@s.whatsapp.net"
    assert t == "[\"x\"]"


def test_extract_payload_returns_none_on_garbage():
    f, t = extract_admin_payload({"foo": "bar"})
    assert f is None and t is None


# ---------------------------------------------------------------------------
# parse_command_and_payload
# ---------------------------------------------------------------------------


def test_parse_command_recognizes_cookies_ml():
    cmd, rest = parse_command_and_payload("/cookies_ml\n[1]")
    assert cmd == COMMAND_TOKEN
    assert rest.strip() == "[1]"


def test_parse_command_case_insensitive():
    cmd, rest = parse_command_and_payload("/COOKIES_ML [1]")
    assert cmd == COMMAND_TOKEN
    assert rest.strip() == "[1]"


def test_parse_command_returns_none_when_no_command():
    cmd, rest = parse_command_and_payload("hola, no es comando")
    assert cmd is None
    assert rest == "hola, no es comando"


def test_parse_command_returns_none_for_empty_string():
    cmd, rest = parse_command_and_payload("")
    assert cmd is None


# ---------------------------------------------------------------------------
# extract_attachment_json
# ---------------------------------------------------------------------------


def test_extract_attachment_json_decodes_base64_documentMessage():
    raw = json.dumps([{"name": "x", "value": "y", "domain": ".mercadolibre.com.mx"}])
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    body = {
        "data": {
            "message": {
                "documentMessage": {
                    "fileName": "cookies.json",
                    "mimetype": "application/json",
                    "data": b64,
                }
            }
        }
    }
    out = extract_attachment_json(body)
    assert out is not None
    assert json.loads(out)[0]["name"] == "x"


def test_extract_attachment_json_returns_none_when_no_attachment():
    assert extract_attachment_json({"data": {"message": {"conversation": "hi"}}}) is None
    assert extract_attachment_json({}) is None


def test_extract_attachment_json_handles_data_uri_prefix():
    raw = json.dumps([{"name": "a", "value": "b", "domain": "mercadolibre.com"}])
    b64 = base64.b64encode(raw.encode()).decode()
    body = {
        "attachment": {
            "name": "cookies.json",
            "contentType": "application/json",
            "data": f"data:application/json;base64,{b64}",
        }
    }
    out = extract_attachment_json(body)
    assert out is not None
    assert "mercadolibre" in out


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_server_healthcheck(running_server):
    base = running_server["url"]
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{base}/wa/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_rejects_missing_secret(running_server):
    base = running_server["url"]
    body = _wrap_with_command("[]")
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{base}/wa/inbound", json=body)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_inbound_rejects_wrong_secret(running_server):
    base = running_server["url"]
    body = _wrap_with_command("[]")
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "wrong"},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_rejects_non_admin_number(running_server):
    base = running_server["url"]
    body = {"from": "529999999999", "text": f"{COMMAND_TOKEN}\n[]"}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )
    assert r.status_code == 403
    assert r.json()["error"] == "not_admin"


@pytest.mark.asyncio
async def test_inbound_ignores_message_without_command(running_server):
    """Sin ``/cookies_ml`` nada se procesa, ni siquiera si parece JSON."""
    base = running_server["url"]
    body = {"from": "528338498692", "text": "hola, cómo estás?"}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored_no_command"
    assert len(running_server["cookies_received"]) == 0


@pytest.mark.asyncio
async def test_inbound_auto_detects_ml_cookies_from_admin_without_command(running_server):
    """Si el admin pega JSON crudo de cookies ML (sin /cookies_ml), igual
    se procesa. Esto cubre el caso real: admin responde al mensaje del
    bot pegando el JSON tal cual.
    """
    base = running_server["url"]
    cookies_json = json.dumps([
        {"name": "_d2id", "value": "abc", "domain": ".mercadolibre.com.mx"},
    ])
    body = {"from": "528338498692", "text": cookies_json}
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json().get("source") == "auto_detected"
    assert len(running_server["cookies_received"]) == 1


@pytest.mark.asyncio
async def test_inbound_ignores_non_ml_json_without_command(running_server):
    """Un JSON cualquiera (sin dominio ML) sin /cookies_ml se ignora."""
    base = running_server["url"]
    body = {
        "from": "528338498692",
        "text": '[{"name":"x","value":"y","domain":".google.com"}]',
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored_no_command"
    assert len(running_server["cookies_received"]) == 0


# ---------------------------------------------------------------------------
# Flujo end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_valid_cookies_triggers_reload(running_server, db, cookies_path):
    """Test obligatorio: ``/cookies_ml`` + JSON válido → on_cookies + success msg."""
    base = running_server["url"]
    cookies_json = json.dumps([
        {"name": "_d2id", "value": "abc", "domain": ".mercadolibre.com.mx"},
        {"name": "_csrf", "value": "x", "domain": ".mercadolibre.com.mx"},
    ])
    body = _wrap_with_command(cookies_json)

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["cookies_count"] == 2

    # on_cookies fue llamado con las cookies
    assert len(running_server["cookies_received"]) == 1
    assert running_server["cookies_received"][0][0]["name"] == "_d2id"

    # Mensaje de confirmación enviado al admin
    assert len(running_server["sent_messages"]) == 1
    number, text = running_server["sent_messages"][0]
    assert number == "528338498692"
    assert "aceptadas" in text.lower() or "reactivada" in text.lower()

    # Eventos en DB
    events = db.execute(
        "SELECT kind FROM runtime_events WHERE kind IN (?, ?)",
        (RECEIVED_KIND, VALIDATED_KIND),
    ).fetchall()
    kinds = [e["kind"] for e in events]
    assert RECEIVED_KIND in kinds
    assert VALIDATED_KIND in kinds


@pytest.mark.asyncio
async def test_inbound_attachment_json_triggers_reload(running_server):
    """Adjuntar un archivo .json en documentMessage debe disparar el flujo."""
    base = running_server["url"]
    cookies_json = json.dumps([
        {"name": "x", "value": "y", "domain": ".mercadolibre.com.mx"},
    ])
    b64 = base64.b64encode(cookies_json.encode()).decode()
    body = {
        "data": {
            "key": {"remoteJid": "528338498692@s.whatsapp.net"},
            "message": {
                "documentMessage": {
                    "fileName": "ml.json",
                    "mimetype": "application/json",
                    "data": b64,
                    # captionado del attachment puede traer el comando, pero
                    # si trae cookie file ya basta con eso.
                    "caption": COMMAND_TOKEN,
                }
            },
        }
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert running_server["cookies_received"][0][0]["name"] == "x"


@pytest.mark.asyncio
async def test_inbound_command_without_body_returns_400(running_server):
    """``/cookies_ml`` sin JSON → 400 + mensaje al admin."""
    base = running_server["url"]
    body = _wrap_text(COMMAND_TOKEN)

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )
    assert r.status_code == 400
    assert r.json()["error"] == "command_without_body"
    # Aviso al admin
    assert any("sin cookies" in t.lower() for _, t in running_server["sent_messages"])


@pytest.mark.asyncio
async def test_inbound_invalid_cookies_returns_400_and_notifies_admin(running_server):
    base = running_server["url"]
    body = _wrap_with_command("[invalid json")

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )

    assert r.status_code == 400
    # Mensaje de error enviado al admin
    assert any("no son válidas" in t.lower() or "no es válido" in t.lower()
               for _, t in running_server["sent_messages"])
    # NO se invocó on_cookies
    assert len(running_server["cookies_received"]) == 0


@pytest.mark.asyncio
async def test_inbound_supports_evolution_v2_envelope(running_server):
    """Versiones de Evolution mandan envueltas en `data`."""
    base = running_server["url"]
    cookies_json = json.dumps([
        {"name": "x", "value": "y", "domain": ".mercadolibre.com.mx"},
    ])
    body = {
        "event": "messages.upsert",
        "data": {
            "key": {"remoteJid": "528338498692@s.whatsapp.net"},
            "message": {
                "extendedTextMessage": {"text": f"{COMMAND_TOKEN}\n{cookies_json}"}
            },
        },
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{base}/wa/inbound",
            json=body,
            headers={"X-Webhook-Secret": "testsecret"},
        )

    assert r.status_code == 200
    assert running_server["cookies_received"][0][0]["name"] == "x"
