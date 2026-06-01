from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from ..notifications.telegram import TelegramNotifier


logger = logging.getLogger(__name__)


AlertSender = Callable[[str, str], Awaitable[bool]]


def build_ml_session_alert_sender(
    settings: Any,
    *,
    whatsapp_send: AlertSender,
    telegram_notifier: Optional[TelegramNotifier] = None,
) -> AlertSender:
    channel = str(getattr(settings, "ml_session_alert_channel", "whatsapp") or "whatsapp")
    channel = channel.strip().lower()

    if channel == "telegram":
        notifier = telegram_notifier or TelegramNotifier(
            bot_token=getattr(settings, "ml_session_telegram_bot_token", None),
            chat_id=getattr(settings, "ml_session_telegram_chat_id", None),
            timeout_seconds=float(
                getattr(settings, "ml_session_telegram_timeout_seconds", 15.0)
            ),
        )

        async def _telegram_send(_recipient: str, text: str) -> bool:
            return await notifier.send_message(text)

        return _telegram_send

    if channel != "whatsapp":
        logger.warning(
            "ML session alert channel desconocido=%s; usando whatsapp temporalmente",
            channel,
        )

    async def _whatsapp_send(recipient: str, text: str) -> bool:
        return await whatsapp_send(recipient, text)

    return _whatsapp_send
