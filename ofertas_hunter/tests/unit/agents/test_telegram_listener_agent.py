"""Tests del TelegramListenerAgent con FakeTelegramAdapter."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterable

import pytest

from ofertas_hunter.agents.telegram_listener_agent import (
    IncomingMessage,
    MissingCredentialsError,
    ProcessingOutcome,
    TelegramListenerAgent,
    TelegramListenerConfig,
)
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.telegram.candidate_builder import (
    LISTENER_DEAL,
    LISTENER_IGNORED_ML,
    LISTENER_NOISE,
    LISTENER_PRICE_ERROR,
)
from ofertas_hunter.telegram.channel_config import ChannelEntry, parse_channels
from ofertas_hunter.telegram.link_resolver import LinkResolver, ResolvedLink


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "telegram"


def _now() -> datetime:
    return datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTelegramAdapter:
    """Adapter en memoria para tests, no toca red ni Telethon."""

    def __init__(self, messages_by_chat: dict[int, list[IncomingMessage]]):
        self.messages = messages_by_chat
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def resolve_channels(
        self, entries: Iterable[ChannelEntry]
    ):
        # Resuelve cada entrada al primer chat_id disponible (en orden de
        # inserción). Mantiene 1:1 con `messages_by_chat`.
        chat_ids = list(self.messages.keys())
        out = []
        for i, entry in enumerate(entries):
            if i >= len(chat_ids):
                break
            chat_id = chat_ids[i]
            out.append((entry, chat_id, entry.normalized()))
        return out

    async def fetch_history(self, chat_id: int, channel: str, limit: int):
        for msg in self.messages.get(chat_id, [])[:limit]:
            yield msg

    async def listen(self, chat_ids, channel_names):
        for chat_id in chat_ids:
            for msg in self.messages.get(chat_id, []):
                yield msg


class FakeResolver:
    """Resolver que no hace red. Devuelve `final_url=original_url`."""

    def __init__(self, mapping: dict[str, ResolvedLink] | None = None):
        self.mapping = mapping or {}

    async def resolve(self, url: str) -> ResolvedLink:
        if url in self.mapping:
            return self.mapping[url]
        return ResolvedLink(
            original_url=url, final_url=url, http_status=200,
            resolved=True, is_mercadolibre="mercadolibre" in url or "meli.la" in url,
        )

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _msg(chat_id: int, message_id: int, *, fixture: str, channel: str = "ofertonesmexico"):
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    return IncomingMessage(
        chat_id=chat_id,
        channel=channel,
        message_id=message_id,
        text=text,
        date=_now(),
        image_path=f"/fake/{message_id}.jpg",
    )


def _config(enabled: bool = True) -> TelegramListenerConfig:
    return TelegramListenerConfig(
        enabled=enabled,
        api_id=12345 if enabled else None,
        api_hash="fakehash" if enabled else None,
        session_path="/tmp/x.session" if enabled else None,
        target_channels=parse_channels("ofertonesmexico,OFERTAS PREMIUM MX"),
        backfill_limit_per_channel=10,
        backfill_process_budget_per_channel=10,
    )


# ---------------------------------------------------------------------------
# Tests obligatorios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_creates_candidate_signal(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    adapter = FakeTelegramAdapter(
        {
            -1001: [_msg(-1001, 100, fixture="iphone_16_pro_max_liverpool_3899.txt")],
        }
    )
    agent = TelegramListenerAgent(
        config=_config(),
        adapter=adapter,
        db_conn=conn,
        resolver=FakeResolver(),
    )

    outcomes = await agent.backfill_once()
    assert len(outcomes) == 1
    out = outcomes[0]
    assert out.candidate.internal_classification == LISTENER_PRICE_ERROR
    assert out.outbox_id is not None

    rows = conn.execute("SELECT type, state, message_payload_json FROM outbox").fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    payload = rows[0]["message_payload_json"]
    assert "requires_live_validation" in payload

    tg_rows = conn.execute("SELECT channel, message_id, skip_reason FROM telegram_messages").fetchall()
    assert len(tg_rows) == 1
    assert tg_rows[0]["channel"] == "ofertonesmexico"
    assert tg_rows[0]["skip_reason"] is None

    conn.close()


@pytest.mark.asyncio
async def test_telegram_message_dedupe_by_chat_and_message_id(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    msg = _msg(-1001, 200, fixture="dell_pro_16_1544.txt")
    adapter = FakeTelegramAdapter({-1001: [msg]})

    agent = TelegramListenerAgent(
        config=_config(),
        adapter=adapter,
        db_conn=conn,
        resolver=FakeResolver(),
    )

    # Primera pasada: lo procesa y crea outbox.
    out1 = await agent.backfill_once()
    assert len(out1) == 1
    assert out1[0].duplicate is False
    assert out1[0].outbox_id is not None

    # Segunda pasada: el mismo (channel, message_id) ya está en
    # `telegram_messages`, debe marcarse como duplicate.
    out2 = await agent.backfill_once()
    assert len(out2) == 1
    assert out2[0].duplicate is True
    # No se duplica en outbox.
    rows = conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()
    assert rows["n"] == 1

    conn.close()


@pytest.mark.asyncio
async def test_telegram_backfill_does_not_duplicate_messages(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    msgs = [
        _msg(-1001, 1, fixture="iphone_16_pro_max_liverpool_3899.txt"),
        _msg(-1001, 2, fixture="airpods_officedepot_599.txt"),
        _msg(-1002, 1, fixture="galaxy_s24_sears_1399.txt", channel="OFERTAS PREMIUM MX"),
    ]
    adapter = FakeTelegramAdapter(
        {
            -1001: msgs[:2],
            -1002: msgs[2:],
        }
    )

    agent = TelegramListenerAgent(
        config=_config(),
        adapter=adapter,
        db_conn=conn,
        resolver=FakeResolver(),
    )

    out1 = await agent.backfill_once()
    assert sum(1 for o in out1 if not o.duplicate) == 3

    out2 = await agent.backfill_once()
    assert all(o.duplicate for o in out2)

    rows = conn.execute("SELECT COUNT(*) as n FROM outbox").fetchone()
    assert rows["n"] == 3
    rows = conn.execute("SELECT COUNT(*) as n FROM telegram_messages").fetchone()
    assert rows["n"] == 3

    conn.close()


@pytest.mark.asyncio
async def test_telegram_missing_credentials_fails_gracefully(tmp_path: Path):
    """Si TELEGRAM_ENABLED=true pero faltan credenciales, debe lanzar
    MissingCredentialsError al intentar arrancar (sin reventar el paquete)."""
    config = TelegramListenerConfig(
        enabled=True,
        api_id=None,
        api_hash=None,
        session_path=None,
        target_channels=[ChannelEntry("ofertonesmexico", "username")],
    )
    adapter = FakeTelegramAdapter({})
    agent = TelegramListenerAgent(config=config, adapter=adapter, resolver=FakeResolver())

    with pytest.raises(MissingCredentialsError):
        await agent.backfill_once()


@pytest.mark.asyncio
async def test_telegram_disabled_returns_empty():
    config = TelegramListenerConfig(enabled=False)
    adapter = FakeTelegramAdapter({})
    agent = TelegramListenerAgent(config=config, adapter=adapter, resolver=FakeResolver())

    outcomes = await agent.backfill_once()
    assert outcomes == []


@pytest.mark.asyncio
async def test_telegram_ignores_mercadolibre_links_via_agent(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    msg = _msg(-1001, 1, fixture="mercadolibre_link_should_be_ignored.txt")
    adapter = FakeTelegramAdapter({-1001: [msg]})

    agent = TelegramListenerAgent(
        config=_config(), adapter=adapter, db_conn=conn, resolver=FakeResolver()
    )

    outcomes = await agent.backfill_once()
    assert len(outcomes) == 1
    assert outcomes[0].candidate.internal_classification == LISTENER_IGNORED_ML
    assert outcomes[0].outbox_id is None

    discarded = conn.execute(
        "SELECT reason, source FROM discarded_candidates"
    ).fetchall()
    assert len(discarded) == 1
    assert discarded[0]["reason"] == LISTENER_IGNORED_ML
    assert discarded[0]["source"] == "telegram"

    rows = conn.execute("SELECT COUNT(*) as n FROM outbox").fetchone()
    assert rows["n"] == 0

    conn.close()


@pytest.mark.asyncio
async def test_telegram_ignores_shortlink_resolved_to_ml(tmp_path: Path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    text = "Producto random\nhttps://bit.ly/abc"
    msg = IncomingMessage(
        chat_id=-1001,
        channel="ofertonesmexico",
        message_id=999,
        text=text,
        date=_now(),
        image_path="/fake/x.jpg",
    )
    adapter = FakeTelegramAdapter({-1001: [msg]})

    resolver = FakeResolver(
        {
            "https://bit.ly/abc": ResolvedLink(
                original_url="https://bit.ly/abc",
                final_url="https://www.mercadolibre.com.mx/p/MLM12345",
                http_status=200,
                resolved=True,
                is_mercadolibre=True,
            )
        }
    )

    agent = TelegramListenerAgent(
        config=_config(), adapter=adapter, db_conn=conn, resolver=resolver
    )
    outcomes = await agent.backfill_once()
    assert outcomes[0].candidate.internal_classification == LISTENER_IGNORED_ML
    rows = conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()
    assert rows["n"] == 0

    conn.close()
