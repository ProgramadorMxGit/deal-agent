from __future__ import annotations

import json
import sqlite3

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.session.ml_session_recovery import MLRecoveryConfig
from ofertas_hunter.session.ml_session_telegram_poller import (
    MLTelegramPoller,
    MLTelegramPollerConfig,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


class _FakeTelegramClient:
    def __init__(self, updates: list[dict], file_text: str | None = None) -> None:
        self._updates = updates
        self._file_text = file_text
        self.sent_messages: list[str] = []
        self.offsets: list[int | None] = []
        self.file_ids: list[str] = []

    async def get_updates(self, *, offset: int | None = None, limit: int = 20) -> list[dict]:
        self.offsets.append(offset)
        return self._updates

    async def download_file_text(self, file_id: str) -> str | None:
        self.file_ids.append(file_id)
        return self._file_text

    async def send_message(self, text: str) -> bool:
        self.sent_messages.append(text)
        return True


@pytest.mark.asyncio
async def test_telegram_poller_processes_inline_cookies_from_configured_chat(db, tmp_path):
    cookie_text = json.dumps(
        [{"name": "_d2id", "value": "abc", "domain": ".mercadolibre.com.mx"}]
    )
    updates = [
        {
            "update_id": 1001,
            "message": {
                "chat": {"id": 5054325626},
                "text": cookie_text,
                "date": 1710000000,
            },
        }
    ]
    telegram = _FakeTelegramClient(updates)
    received: list[list[dict]] = []

    async def on_cookies(cookies: list[dict]) -> bool:
        received.append(cookies)
        return True

    poller = MLTelegramPoller(
        recovery_config=MLRecoveryConfig(
            enabled=True,
            alert_channel="telegram",
            telegram_chat_id="5054325626",
            cookies_path=str(tmp_path / "ml_cookies.json"),
        ),
        poller_config=MLTelegramPollerConfig(
            enabled=True,
            chat_id="5054325626",
            poll_interval_seconds=15,
        ),
        validator=None,
        on_cookies=on_cookies,
        telegram_client=telegram,
        db=db,
    )

    await poller._tick_once()

    assert len(received) == 1
    assert received[0][0]["name"] == "_d2id"
    assert any("aceptadas" in msg.lower() for msg in telegram.sent_messages)


@pytest.mark.asyncio
async def test_telegram_poller_processes_json_document_attachment(db, tmp_path):
    updates = [
        {
            "update_id": 1002,
            "message": {
                "chat": {"id": 5054325626},
                "document": {
                    "file_id": "file-123",
                    "file_name": "cookies.json",
                    "mime_type": "application/json",
                },
                "date": 1710000001,
            },
        }
    ]
    telegram = _FakeTelegramClient(
        updates,
        file_text=json.dumps(
            [{"name": "_csrf", "value": "x", "domain": ".mercadolibre.com.mx"}]
        ),
    )
    received: list[list[dict]] = []

    async def on_cookies(cookies: list[dict]) -> bool:
        received.append(cookies)
        return True

    poller = MLTelegramPoller(
        recovery_config=MLRecoveryConfig(
            enabled=True,
            alert_channel="telegram",
            telegram_chat_id="5054325626",
            cookies_path=str(tmp_path / "ml_cookies.json"),
        ),
        poller_config=MLTelegramPollerConfig(
            enabled=True,
            chat_id="5054325626",
            poll_interval_seconds=15,
        ),
        validator=None,
        on_cookies=on_cookies,
        telegram_client=telegram,
        db=db,
    )

    await poller._tick_once()

    assert telegram.file_ids == ["file-123"]
    assert len(received) == 1
    assert received[0][0]["name"] == "_csrf"


@pytest.mark.asyncio
async def test_telegram_poller_processes_txt_document_attachment(db, tmp_path):
    updates = [
        {
            "update_id": 1003,
            "message": {
                "chat": {"id": 5054325626},
                "document": {
                    "file_id": "file-456",
                    "file_name": "cookies.txt",
                    "mime_type": "text/plain",
                },
                "date": 1710000002,
            },
        }
    ]
    telegram = _FakeTelegramClient(
        updates,
        file_text=json.dumps(
            [{"name": "ssid", "value": "x", "domain": ".mercadolibre.com.mx"}]
        ),
    )
    received: list[list[dict]] = []

    async def on_cookies(cookies: list[dict]) -> bool:
        received.append(cookies)
        return True

    poller = MLTelegramPoller(
        recovery_config=MLRecoveryConfig(
            enabled=True,
            alert_channel="telegram",
            telegram_chat_id="5054325626",
            cookies_path=str(tmp_path / "ml_cookies.json"),
        ),
        poller_config=MLTelegramPollerConfig(
            enabled=True,
            chat_id="5054325626",
            poll_interval_seconds=15,
        ),
        validator=None,
        on_cookies=on_cookies,
        telegram_client=telegram,
        db=db,
    )

    await poller._tick_once()

    assert telegram.file_ids == ["file-456"]
    assert len(received) == 1
    assert received[0][0]["name"] == "ssid"


@pytest.mark.asyncio
async def test_telegram_poller_ignores_old_split_json_when_new_document_arrives(db, tmp_path):
    updates = [
        {
            "update_id": 2001,
            "message": {
                "chat": {"id": 5054325626},
                "text": '[{"domain": ".mercadolibre.com.mx",',
            },
        },
        {
            "update_id": 2002,
            "message": {
                "chat": {"id": 5054325626},
                "text": "/cookies_ml",
            },
        },
        {
            "update_id": 2003,
            "message": {
                "chat": {"id": 5054325626},
                "document": {
                    "file_id": "file-789",
                    "file_name": "cookies.json",
                    "mime_type": "application/json",
                },
            },
        },
    ]
    telegram = _FakeTelegramClient(
        updates,
        file_text=json.dumps(
            [{"name": "_csrf", "value": "x", "domain": ".mercadolibre.com.mx"}]
        ),
    )
    received: list[list[dict]] = []

    async def on_cookies(cookies: list[dict]) -> bool:
        received.append(cookies)
        return True

    poller = MLTelegramPoller(
        recovery_config=MLRecoveryConfig(
            enabled=True,
            alert_channel="telegram",
            telegram_chat_id="5054325626",
            cookies_path=str(tmp_path / "ml_cookies.json"),
        ),
        poller_config=MLTelegramPollerConfig(
            enabled=True,
            chat_id="5054325626",
            poll_interval_seconds=15,
        ),
        validator=None,
        on_cookies=on_cookies,
        telegram_client=telegram,
        db=db,
    )

    await poller._tick_once()

    assert telegram.file_ids == ["file-789"]
    assert len(received) == 1
    assert received[0][0]["name"] == "_csrf"
