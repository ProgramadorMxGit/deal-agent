from __future__ import annotations

import json

import httpx
import pytest

from ofertas_hunter.notifications.telegram import TelegramNotifier


@pytest.mark.asyncio
async def test_telegram_notifier_builds_send_message_request():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as session:
        notifier = TelegramNotifier(
            bot_token="test-token",
            chat_id="5054325626",
            timeout_seconds=15,
            client=session,
        )
        ok = await notifier.send_message("hola admin")

    assert ok is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.telegram.org/bottest-token/sendMessage"
    payload = json.loads(captured["body"])
    assert payload["chat_id"] == "5054325626"
    assert payload["text"] == "hola admin"
    assert payload["disable_web_page_preview"] is True


@pytest.mark.asyncio
async def test_telegram_notifier_returns_false_when_missing_config(caplog):
    notifier = TelegramNotifier(bot_token="", chat_id="", timeout_seconds=15)

    with caplog.at_level("ERROR"):
        ok = await notifier.send_message("hola")

    assert ok is False
    assert "telegram notifier misconfigured" in caplog.text.lower()
