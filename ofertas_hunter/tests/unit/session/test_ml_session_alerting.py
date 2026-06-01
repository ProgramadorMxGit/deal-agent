from __future__ import annotations

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.session.ml_session_alerting import build_ml_session_alert_sender


class _FakeTelegramNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, text: str) -> bool:
        self.messages.append(text)
        return True


@pytest.mark.asyncio
async def test_ml_session_alert_sender_uses_telegram_when_channel_is_telegram():
    settings = Settings(
        ml_session_alert_channel="telegram",
        ml_session_telegram_bot_token="bot-token",
        ml_session_telegram_chat_id="5054325626",
    )
    notifier = _FakeTelegramNotifier()
    whatsapp_calls: list[tuple[str, str]] = []

    async def whatsapp_send(number: str, text: str) -> bool:
        whatsapp_calls.append((number, text))
        return True

    sender = build_ml_session_alert_sender(
        settings,
        whatsapp_send=whatsapp_send,
        telegram_notifier=notifier,
    )

    ok = await sender("528338498692", "alerta ml")
    assert ok is True
    assert notifier.messages == ["alerta ml"]
    assert whatsapp_calls == []


@pytest.mark.asyncio
async def test_ml_session_alert_sender_falls_back_to_whatsapp_channel():
    settings = Settings(ml_session_alert_channel="whatsapp")
    notifier = _FakeTelegramNotifier()
    whatsapp_calls: list[tuple[str, str]] = []

    async def whatsapp_send(number: str, text: str) -> bool:
        whatsapp_calls.append((number, text))
        return True

    sender = build_ml_session_alert_sender(
        settings,
        whatsapp_send=whatsapp_send,
        telegram_notifier=notifier,
    )

    ok = await sender("528338498692", "alerta ml")
    assert ok is True
    assert whatsapp_calls == [("528338498692", "alerta ml")]
    assert notifier.messages == []
