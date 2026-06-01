"""Tests del cliente de Evolution API."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from ofertas_hunter.publishing.evolution_client import (
    EvolutionClient,
    EvolutionConfigError,
)


@pytest.mark.asyncio
async def test_evolution_client_send_text_dry_run():
    client = EvolutionClient(
        base_url="http://example.test:8080",
        api_key="dev-key",
        instance="mi-inst",
        dry_run=True,
    )
    resp = await client.send_text("120363@g.us", "Hola")
    assert resp.success is True
    assert resp.dry_run is True
    assert resp.raw["payload"] == {"number": "120363@g.us", "text": "Hola"}


@pytest.mark.asyncio
async def test_evolution_client_send_media_dry_run(tmp_path: Path):
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    client = EvolutionClient(
        base_url="http://example.test:8080",
        api_key="dev-key",
        instance="mi-inst",
        dry_run=True,
    )
    resp = await client.send_media(
        "120363@g.us", img, caption="oferta", file_name="oferta.jpg"
    )
    assert resp.success is True
    assert resp.dry_run is True
    payload = resp.raw["payload"]
    assert payload["number"] == "120363@g.us"
    assert payload["mediatype"] == "image"
    assert payload["mimetype"] == "image/jpeg"
    assert payload["caption"] == "oferta"
    # En dry-run el campo media se reemplaza por placeholder por longitud
    assert "<" in payload["media"] and "chars>" in payload["media"]


@pytest.mark.asyncio
async def test_evolution_client_send_media_dry_run_accepts_https_url():
    client = EvolutionClient(
        base_url="http://example.test:8080",
        api_key="dev-key",
        instance="mi-inst",
        dry_run=True,
    )
    resp = await client.send_media(
        "5218338498692",
        "https://m.media-amazon.com/images/I/abc.jpg",
        caption="oferta",
    )
    assert resp.success is True
    payload = resp.raw["payload"]
    assert payload["media"].startswith("https://")


@pytest.mark.asyncio
async def test_evolution_client_send_text_real_uses_post(monkeypatch):
    """Modo real (dry_run=False): debe POSTear al endpoint correcto con headers."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["apikey"] = request.headers.get("apikey")
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"key": {"id": "msg1"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as session:
        client = EvolutionClient(
            base_url="http://example.test:8080",
            api_key="dev-key",
            instance="mi-inst",
            api_key_header="apikey",
            dry_run=False,
            client=session,
        )
        resp = await client.send_text("120363@g.us", "Hola")

    assert resp.success is True
    assert resp.status_code == 200
    assert resp.dry_run is False
    assert captured["method"] == "POST"
    assert captured["url"] == "http://example.test:8080/message/sendText/mi-inst"
    assert captured["apikey"] == "dev-key"
    assert '"number": "120363@g.us"' in captured["body"]


@pytest.mark.asyncio
async def test_evolution_client_send_text_real_uses_configured_header_name():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["custom-auth"] = request.headers.get("custom-auth")
        captured["apikey"] = request.headers.get("apikey")
        return httpx.Response(200, json={"key": {"id": "msg1"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as session:
        client = EvolutionClient(
            base_url="http://example.test:8080",
            api_key="dev-key",
            instance="mi-inst",
            api_key_header="custom-auth",
            dry_run=False,
            client=session,
        )
        resp = await client.send_text("120363@g.us", "Hola")

    assert resp.success is True
    assert captured["custom-auth"] == "dev-key"
    assert captured["apikey"] is None


@pytest.mark.asyncio
async def test_evolution_client_send_text_real_records_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as session:
        client = EvolutionClient(
            base_url="http://example.test:8080",
            api_key="bad-key",
            instance="mi-inst",
            dry_run=False,
            client=session,
        )
        resp = await client.send_text("120363@g.us", "Hola")

    assert resp.success is False
    assert resp.status_code == 401
    assert resp.error == "http_status_401"


@pytest.mark.asyncio
async def test_evolution_client_real_unconfigured_raises():
    client = EvolutionClient(
        base_url="", api_key="", instance="", dry_run=False
    )
    with pytest.raises(EvolutionConfigError):
        await client.send_text("x", "y")


@pytest.mark.asyncio
async def test_evolution_client_send_media_with_bytes():
    client = EvolutionClient(
        base_url="http://x", api_key="k", instance="i", dry_run=True
    )
    resp = await client.send_media("123", b"hello-bytes", caption="x")
    assert resp.success is True


@pytest.mark.asyncio
async def test_evolution_client_send_media_with_data_url():
    payload_b64 = base64.b64encode(b"hi").decode("ascii")
    client = EvolutionClient(
        base_url="http://x", api_key="k", instance="i", dry_run=True
    )
    resp = await client.send_media(
        "123", f"data:image/png;base64,{payload_b64}", caption=""
    )
    assert resp.success is True
    assert resp.raw["payload"]["mimetype"] == "image/png"


@pytest.mark.asyncio
async def test_evolution_client_send_image_uses_send_media_contract():
    client = EvolutionClient(
        base_url="http://x",
        api_key="k",
        instance="i",
        dry_run=True,
    )
    resp = await client.send_image(
        "5218338498692",
        "https://example.test/image.jpg",
        "caption",
    )
    assert resp.success is True
    assert resp.raw["payload"]["number"] == "5218338498692"
    assert resp.raw["payload"]["mediatype"] == "image"
    assert resp.raw["payload"]["caption"] == "caption"


@pytest.mark.asyncio
async def test_evolution_client_connection_state_reads_open():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["apikey"] = request.headers.get("apikey")
        return httpx.Response(200, json={"instance": {"state": "open"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as session:
        client = EvolutionClient(
            base_url="http://example.test:8080",
            api_key="dev-key",
            instance="mi-inst",
            api_key_header="apikey",
            dry_run=False,
            client=session,
        )
        state = await client.connection_state()

    assert state == "open"
    assert captured["method"] == "GET"
    assert captured["url"] == "http://example.test:8080/instance/connectionState/mi-inst"
    assert captured["apikey"] == "dev-key"
