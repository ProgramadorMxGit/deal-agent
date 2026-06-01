from __future__ import annotations

import json
import logging
from typing import Optional

import httpx


logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        *,
        bot_token: Optional[str],
        chat_id: Optional[str],
        timeout_seconds: float = 15.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def send_message(self, text: str) -> bool:
        if not self.configured:
            logger.error(
                "Telegram notifier misconfigured: missing bot token or chat id"
            )
            return False
        if not text:
            logger.error("Telegram notifier message is empty")
            return False

        client = await self._ensure_client()

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            response = await client.post(
                self._api_url("sendMessage"),
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("Telegram send_message failed: %s", exc)
            return False

        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            data = {"text": response.text}

        if response.status_code != 200 or not bool(data.get("ok", False)):
            logger.warning(
                "Telegram sendMessage non-success status=%s body=%s",
                response.status_code,
                str(data)[:300],
            )
            return False

        return True

    async def get_updates(
        self,
        *,
        offset: Optional[int] = None,
        limit: int = 20,
    ) -> list[dict]:
        if not self.configured:
            logger.error(
                "Telegram notifier misconfigured: missing bot token or chat id"
            )
            return []
        client = await self._ensure_client()
        payload: dict[str, object] = {"limit": limit}
        if offset is not None:
            payload["offset"] = offset
        try:
            response = await client.post(
                self._api_url("getUpdates"),
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Telegram getUpdates failed: %s", exc)
            return []
        result = data.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []

    async def download_file_text(self, file_id: str) -> Optional[str]:
        if not self.configured:
            logger.error(
                "Telegram notifier misconfigured: missing bot token or chat id"
            )
            return None
        if not file_id:
            return None
        client = await self._ensure_client()
        try:
            meta = await client.post(
                self._api_url("getFile"),
                json={"file_id": file_id},
                timeout=self.timeout_seconds,
            )
            meta.raise_for_status()
            meta_data = meta.json()
            file_path = (
                meta_data.get("result", {}).get("file_path")
                if isinstance(meta_data, dict)
                else None
            )
            if not isinstance(file_path, str) or not file_path:
                return None
            download = await client.get(
                f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}",
                timeout=self.timeout_seconds,
            )
            download.raise_for_status()
            return download.text
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Telegram download_file_text failed: %s", exc)
            return None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
