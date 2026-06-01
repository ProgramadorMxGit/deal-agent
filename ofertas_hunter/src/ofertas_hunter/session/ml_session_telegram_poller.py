from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..notifications.telegram import TelegramNotifier
from .ml_session_inbound import looks_like_cookies_json, parse_command_and_payload
from .ml_session_recovery import (
    MLCookieValidator,
    MLRecoveryConfig,
    RECEIVED_KIND,
    VALIDATED_KIND,
    build_admin_success_message,
    build_admin_validation_error_message,
    build_admin_validation_failed_session_message,
)


logger = logging.getLogger(__name__)

COMMAND_TOKEN = "/cookies_ml"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass
class MLTelegramPollerConfig:
    enabled: bool = True
    chat_id: str = ""
    poll_interval_seconds: int = 15
    page_size: int = 20

    @classmethod
    def from_settings(cls, settings: Any) -> "MLTelegramPollerConfig":
        channel = str(
            getattr(settings, "ml_session_alert_channel", "whatsapp") or "whatsapp"
        ).strip().lower()
        return cls(
            enabled=channel == "telegram",
            chat_id=str(getattr(settings, "ml_session_telegram_chat_id", "") or ""),
            poll_interval_seconds=int(
                getattr(settings, "ml_session_telegram_poll_interval_seconds", 15)
            ),
            page_size=20,
        )


class MLTelegramPoller:
    def __init__(
        self,
        *,
        recovery_config: MLRecoveryConfig,
        poller_config: MLTelegramPollerConfig,
        validator: Optional[MLCookieValidator],
        on_cookies: Callable[[list[dict]], Awaitable[bool]],
        telegram_client: TelegramNotifier,
        db: Any,
    ) -> None:
        self.recovery_config = recovery_config
        self.config = poller_config
        self.validator = validator or MLCookieValidator()
        self._on_cookies = on_cookies
        self._telegram = telegram_client
        self.db = db
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._offset: Optional[int] = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self.config.enabled:
            logger.info("ml_session_telegram_poller: deshabilitado por config")
            return
        if not self.config.chat_id:
            logger.warning("ml_session_telegram_poller: chat_id vacio; no arranco")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="ml-session-telegram")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ml_session_telegram_poller: tick raised")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.config.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _tick_once(self) -> None:
        updates = await self._telegram.get_updates(
            offset=self._offset,
            limit=self.config.page_size,
        )
        updates = self._trim_to_recent_cookie_attempt(updates)

        max_update = self._offset
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                max_update = max(max_update or update_id, update_id)
            await self._process_update(update)
        if max_update is not None:
            self._offset = max_update + 1

    async def _process_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat_id = str((message.get("chat") or {}).get("id") or "")
        if chat_id != self.config.chat_id:
            return

        text = message.get("text") or message.get("caption") or ""
        command, remaining_text = parse_command_and_payload(text)
        text_looks_like_cookies = looks_like_cookies_json(text)

        attachment_json: Optional[str] = None
        if self._is_document_candidate(message):
            document = message.get("document") or {}
            attachment_json = await self._telegram.download_file_text(
                str(document.get("file_id") or "")
            )

        if command != COMMAND_TOKEN and not attachment_json and not text_looks_like_cookies:
            return

        cookie_text: Optional[str] = None
        source = "telegram_unknown"
        if attachment_json:
            cookie_text = attachment_json.strip()
            source = "telegram_attachment"
        elif command == COMMAND_TOKEN and remaining_text and remaining_text.strip():
            cookie_text = remaining_text.strip()
            source = "telegram_inline"
        elif text_looks_like_cookies and text:
            cookie_text = text.strip()
            source = "telegram_auto_detected"

        if not cookie_text:
            # Permitir comando solo, seguido por adjunto en el siguiente mensaje.
            return

        self._emit(RECEIVED_KIND, "info", {"from": chat_id, "source": source})
        result = self.validator.validate(cookie_text)
        self._emit(
            VALIDATED_KIND,
            "info" if result.ok else "warning",
            {
                "from": chat_id,
                "ok": result.ok,
                "reason": result.reason,
                "count": result.sanitized_count,
                "source": source,
            },
        )

        if not result.ok:
            await self._telegram.send_message(
                build_admin_validation_error_message(result.reason or "?")
            )
            return

        reload_ok = bool(await self._on_cookies(result.cookies))
        if reload_ok:
            await self._telegram.send_message(
                build_admin_success_message(len(result.cookies))
            )
            return

        await self._telegram.send_message(
            build_admin_validation_failed_session_message(
                "validacion activa rechazo la sesion"
            )
        )

    def _trim_to_recent_cookie_attempt(self, updates: list[dict]) -> list[dict]:
        anchor_idx: Optional[int] = None
        for idx, update in enumerate(updates):
            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue
            chat_id = str((message.get("chat") or {}).get("id") or "")
            if chat_id != self.config.chat_id:
                continue
            text = message.get("text") or message.get("caption") or ""
            command, _remaining_text = parse_command_and_payload(text)
            if command == COMMAND_TOKEN or self._is_document_candidate(message):
                anchor_idx = idx
        if anchor_idx is None:
            return updates
        return updates[anchor_idx:]

    @staticmethod
    def _is_document_candidate(message: dict) -> bool:
        document = message.get("document")
        if not isinstance(document, dict):
            return False
        file_name = str(document.get("file_name") or "").lower()
        mime_type = str(document.get("mime_type") or "").lower()
        return (
            file_name.endswith((".json", ".txt"))
            or "json" in mime_type
            or mime_type == "text/plain"
        )

    def _emit(self, kind: str, severity: str, payload: dict) -> None:
        if self.db is None:
            return
        try:
            import json

            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (kind, severity, json.dumps(payload, ensure_ascii=False), _now_iso()),
            )
            self.db.commit()
        except Exception:
            logger.exception("ml_session_telegram_poller: emit fallo")
