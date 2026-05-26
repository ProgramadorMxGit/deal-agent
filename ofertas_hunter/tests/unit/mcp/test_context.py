"""Tests para `ofertas_hunter.mcp.context.ServerContext`.

No instanciamos browser real (Playwright). Los tests cubren:
- factory `build` construye scheduler con la misma config que Settings
- locks por marketplace son singletons
- getters de evolution_client y publisher devuelven la misma instancia
- record_normal_publication actualiza el timestamp
- aclose es idempotente cuando no hay agentes inicializados
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from ofertas_hunter.config import Settings
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.mcp.context import ServerContext
from ofertas_hunter.runtime.scheduler import ScheduleMode


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "x.db"
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def settings() -> Settings:
    # Forzamos modo seguro (publishing_dry_run=True por default)
    return Settings()


def test_build_uses_scheduler_config_from_settings(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    assert ctx.scheduler is not None
    assert ctx.scheduler.config.timezone_name == settings.schedule_timezone


def test_lock_for_returns_same_lock_per_marketplace(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    a = ctx.lock_for("amazon")
    b = ctx.lock_for("amazon")
    c = ctx.lock_for("mercadolibre")
    assert a is b
    assert a is not c


def test_get_evolution_client_is_singleton(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    a = ctx.get_evolution_client()
    b = ctx.get_evolution_client()
    assert a is b


def test_get_publisher_is_singleton_and_uses_safe_mode_default(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    pub = ctx.get_publisher()
    assert pub is ctx.get_publisher()
    assert pub.enabled == settings.publishing_enabled


def test_get_outbox_repo_is_singleton(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    a = ctx.get_outbox_repo()
    assert a is ctx.get_outbox_repo()


def test_get_dispatcher_is_singleton(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    a = ctx.get_dispatcher()
    assert a is ctx.get_dispatcher()


def test_record_normal_publication_sets_timestamp(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    assert ctx.last_normal_publication_at is None
    ctx.record_normal_publication()
    assert ctx.last_normal_publication_at is not None


def test_dispatcher_uses_same_scheduler(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    dispatcher = ctx.get_dispatcher()
    assert dispatcher.scheduler is ctx.scheduler


@pytest.mark.asyncio
async def test_aclose_is_idempotent_without_browser(db, settings) -> None:
    ctx = ServerContext.build(db=db, settings=settings)
    # No tocamos browser ni hunters: aclose debe correr sin errores
    await ctx.aclose()
    await ctx.aclose()
