"""Tests del sistema de recovery de sesión ML.

Cubre:

- Validación del JSON de cookies (formato, dominio ML, tamaños).
- Monitor: detecta cookie_expiry, manda WhatsApp, respeta cooldown.
- Reloader: rota backups, escribe JSON, llama callback de reload.
- Mensajes al admin son texto plano (NO formato canónico de oferta).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.session.ml_session_recovery import (
    ALERT_KIND,
    ALERT_SKIPPED_KIND,
    MLCookieReloader,
    MLCookieValidator,
    MLRecoveryConfig,
    MLSessionMonitor,
    RELOAD_OK_KIND,
    RELOAD_ERROR_KIND,
    build_admin_alert_message,
    build_admin_success_message,
    build_admin_validation_error_message,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def ml_config(tmp_path):
    return MLRecoveryConfig(
        enabled=True,
        admin_numbers=("528338498692",),
        alert_cooldown_seconds=60,
        inbound_enabled=False,  # tests separados activan el server
        inbound_secret="testsecret",
        cookie_backup_count=3,
        cookies_path=str(tmp_path / "ml_cookies.json"),
    )


def _emit_event(db, kind: str, severity: str = "warning", *, payload: dict | None = None):
    db.execute(
        "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            kind,
            severity,
            json.dumps(payload or {}, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        ),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Validador
# ---------------------------------------------------------------------------


def test_validator_accepts_well_formed_ml_cookies():
    raw = json.dumps([
        {"name": "_d2id", "value": "abc", "domain": ".mercadolibre.com.mx"},
        {"name": "_csrf", "value": "x", "domain": ".mercadolibre.com.mx"},
    ])
    r = MLCookieValidator().validate(raw)
    assert r.ok
    assert r.sanitized_count == 2


def test_validator_rejects_empty_body():
    r = MLCookieValidator().validate("")
    assert not r.ok and r.reason == "empty_body"


def test_validator_rejects_invalid_json():
    r = MLCookieValidator().validate("not json")
    assert not r.ok and r.reason.startswith("invalid_json")


def test_validator_rejects_object_root():
    r = MLCookieValidator().validate('{"foo": "bar"}')
    assert not r.ok and r.reason == "json_root_must_be_list"


def test_validator_rejects_empty_list():
    r = MLCookieValidator().validate("[]")
    assert not r.ok and r.reason == "empty_cookies_list"


def test_validator_rejects_missing_required_fields():
    raw = json.dumps([{"name": "x", "value": "y"}])  # falta domain
    r = MLCookieValidator().validate(raw)
    assert not r.ok and "missing_domain" in r.reason


def test_validator_rejects_no_ml_domain():
    raw = json.dumps([
        {"name": "x", "value": "y", "domain": ".google.com"},
        {"name": "z", "value": "w", "domain": ".facebook.com"},
    ])
    r = MLCookieValidator().validate(raw)
    assert not r.ok and r.reason == "no_mercadolibre_domain_found"


def test_validator_accepts_subdomains_ml():
    raw = json.dumps([
        {"name": "x", "value": "y", "domain": "subdomain.mercadolibre.com.mx"},
    ])
    r = MLCookieValidator().validate(raw)
    assert r.ok


def test_validator_rejects_too_many_cookies():
    cookies = [
        {"name": f"c{i}", "value": "v", "domain": ".mercadolibre.com.mx"}
        for i in range(301)
    ]
    r = MLCookieValidator().validate(json.dumps(cookies))
    assert not r.ok and r.reason.startswith("too_many_cookies")


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ml_session_monitor_alerts_admin_on_expiry(db, ml_config):
    """Test obligatorio: cuando hay un cookie_expiry, manda WhatsApp."""
    sent_to: list[tuple[str, str]] = []

    async def fake_send(number: str, text: str) -> bool:
        sent_to.append((number, text))
        return True

    monitor = MLSessionMonitor(db=db, config=ml_config, evolution_send=fake_send)

    # No hay expiry → no hace nada
    assert await monitor.tick() is None

    _emit_event(db, "cookie_expiry")
    result = await monitor.tick()
    assert result == ALERT_KIND
    assert len(sent_to) == 1
    assert sent_to[0][0] == "528338498692"
    assert "Mercado Libre" in sent_to[0][1]


@pytest.mark.asyncio
async def test_ml_session_monitor_respects_cooldown(db, ml_config):
    """Test obligatorio: si ya alertamos hace <cooldown, no spamear."""
    sent_to: list[tuple[str, str]] = []

    async def fake_send(number: str, text: str) -> bool:
        sent_to.append((number, text))
        return True

    monitor = MLSessionMonitor(db=db, config=ml_config, evolution_send=fake_send)

    _emit_event(db, "cookie_expiry")
    assert await monitor.tick() == ALERT_KIND  # alert
    assert len(sent_to) == 1

    # Otro expiry inmediato → cooldown debe bloquear
    _emit_event(db, "cookie_expiry")
    result = await monitor.tick()
    assert result == ALERT_SKIPPED_KIND
    assert len(sent_to) == 1  # NO se mandó otro WhatsApp


@pytest.mark.asyncio
async def test_ml_session_monitor_skips_when_no_admins_configured(db):
    """Si no hay admins, no se hace nada (no crashea)."""
    config = MLRecoveryConfig(enabled=True, admin_numbers=(), alert_cooldown_seconds=60)

    async def fake_send(number, text):
        raise AssertionError("No debería invocarse send")

    monitor = MLSessionMonitor(db=db, config=config, evolution_send=fake_send)
    _emit_event(db, "cookie_expiry")
    assert await monitor.tick() is None


@pytest.mark.asyncio
async def test_ml_session_monitor_does_not_re_alert_after_already_alerted(db, ml_config):
    """Si la última alerta es POSTERIOR al último expiry, no re-alertamos."""
    sent_to: list = []

    async def fake_send(number, text):
        sent_to.append(1)
        return True

    monitor = MLSessionMonitor(db=db, config=ml_config, evolution_send=fake_send)

    _emit_event(db, "cookie_expiry")
    await monitor.tick()
    assert len(sent_to) == 1

    # Sin nuevos expiries, llamar tick otra vez no debería volver a alertar.
    result = await monitor.tick()
    assert result is None
    assert len(sent_to) == 1


@pytest.mark.asyncio
async def test_ml_session_monitor_does_not_alert_when_send_fails(db, ml_config):
    """Si Evolution falla, NO marcamos como alertado: próximo tick reintenta."""

    async def failing_send(number, text):
        return False

    monitor = MLSessionMonitor(db=db, config=ml_config, evolution_send=failing_send)
    _emit_event(db, "cookie_expiry")

    result = await monitor.tick()
    assert result is None  # ni alert ni skip

    # No debe haber `ml_session_admin_alerted` en la DB
    rows = db.execute(
        "SELECT 1 FROM runtime_events WHERE kind = ?", (ALERT_KIND,)
    ).fetchall()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_no_alert_when_disabled(db):
    """Test obligatorio: si `enabled=False`, no envía nada."""
    config = MLRecoveryConfig(enabled=False, admin_numbers=("528338498692",))

    async def fake_send(number, text):
        raise AssertionError("No debería enviarse")

    monitor = MLSessionMonitor(db=db, config=config, evolution_send=fake_send)
    _emit_event(db, "cookie_expiry")
    assert await monitor.tick() is None


@pytest.mark.asyncio
async def test_ml_session_monitor_alerts_via_telegram_sender_when_channel_is_telegram(
    db, tmp_path
):
    config = MLRecoveryConfig(
        enabled=True,
        alert_channel="telegram",
        telegram_bot_token="bot-token",
        telegram_chat_id="5054325626",
        inbound_enabled=False,
        cookies_path=str(tmp_path / "ml_cookies.json"),
    )
    telegram_messages: list[str] = []
    whatsapp_calls: list[tuple[str, str]] = []

    async def telegram_send(_target: str, text: str) -> bool:
        telegram_messages.append(text)
        return True

    async def whatsapp_send(number: str, text: str) -> bool:
        whatsapp_calls.append((number, text))
        return True

    monitor = MLSessionMonitor(db=db, config=config, evolution_send=telegram_send)
    _emit_event(
        db,
        "cookie_expiry",
        payload={"reason": "cookies_missing_or_empty", "manager_state": "invalid"},
    )

    result = await monitor.tick()
    assert result == ALERT_KIND
    assert len(telegram_messages) == 1
    assert "cookies_missing_or_empty" in telegram_messages[0]
    assert "invalid" in telegram_messages[0]
    assert whatsapp_calls == []


@pytest.mark.asyncio
async def test_ml_session_monitor_telegram_missing_config_does_not_crash(
    db, tmp_path, caplog
):
    config = MLRecoveryConfig(
        enabled=True,
        alert_channel="telegram",
        telegram_bot_token="",
        telegram_chat_id="",
        inbound_enabled=False,
        cookies_path=str(tmp_path / "ml_cookies.json"),
    )

    async def missing_config_sender(_target: str, _text: str) -> bool:
        return False

    monitor = MLSessionMonitor(db=db, config=config, evolution_send=missing_config_sender)
    _emit_event(db, "cookie_expiry", payload={"reason": "missing_cookies"})

    with caplog.at_level("ERROR"):
        result = await monitor.tick()

    assert result is None


# ---------------------------------------------------------------------------
# Mensajes
# ---------------------------------------------------------------------------


def test_admin_alert_message_is_plain_text_not_canonical_format():
    """Test obligatorio: el aviso al admin NO debe parecer una oferta
    canónica. No debe tener `*titulo*`, `~precio~`, ni `*X% de descuento*`.
    """
    msg = build_admin_alert_message()
    assert "Mercado Libre" in msg
    # NO debe usar el formato canónico de ofertas
    assert "*Ver oferta:*" not in msg
    # No debe parecer la cabecera `*nombre del producto*`: revisamos que
    # no haya patrones tipo `*X% de descuento*`.
    import re
    assert not re.search(r"\*\d+% de descuento\*", msg)
    assert "~$" not in msg


def test_admin_success_message_includes_count():
    msg = build_admin_success_message(42)
    assert "42" in msg


def test_admin_validation_error_message_includes_reason():
    msg = build_admin_validation_error_message("invalid_json")
    assert "invalid_json" in msg


# ---------------------------------------------------------------------------
# Reloader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reloader_writes_cookies_with_backup(db, ml_config, tmp_path):
    """Test obligatorio: rota archivo viejo, escribe nuevo."""
    cookies_path = Path(ml_config.cookies_path)
    # Archivo previo
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    cookies_path.write_text(
        json.dumps([{"name": "old", "value": "1", "domain": ".mercadolibre.com.mx"}]),
        encoding="utf-8",
    )

    new_cookies = [
        {"name": "fresh", "value": "abc", "domain": ".mercadolibre.com.mx"},
    ]

    reloader = MLCookieReloader(db=db, config=ml_config, reload_callback=None)
    ok = await reloader.apply(new_cookies)
    assert ok

    # Archivo nuevo escrito
    saved = json.loads(cookies_path.read_text(encoding="utf-8"))
    assert saved[0]["name"] == "fresh"

    # Backup creado
    backup_dir = cookies_path.parent / "cookies_backups"
    backups = list(backup_dir.iterdir())
    assert len(backups) == 1
    assert "old" in backups[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_reloader_prunes_old_backups(db, ml_config):
    """Conserva sólo los últimos `cookie_backup_count` backups."""
    cookies_path = Path(ml_config.cookies_path)
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    cookies_path.write_text(
        json.dumps([{"name": "v0", "value": "x", "domain": ".mercadolibre.com.mx"}]),
        encoding="utf-8",
    )

    reloader = MLCookieReloader(db=db, config=ml_config, reload_callback=None)

    # 5 reloads. Con timestamp en microsegundos no colisionan.
    for i in range(5):
        await reloader.apply([
            {"name": f"v{i+1}", "value": "x", "domain": ".mercadolibre.com.mx"},
        ])

    backup_dir = cookies_path.parent / "cookies_backups"
    backups = list(backup_dir.iterdir())
    assert len(backups) == 3, f"esperaba 3 backups, hay {len(backups)}"


@pytest.mark.asyncio
async def test_reloader_calls_reload_callback_and_emits_event(db, ml_config):
    """Test obligatorio: hot-reload callback se llama y emite event."""
    received: list[list[dict]] = []

    async def reload_cb(cookies: list[dict]) -> bool:
        received.append(cookies)
        return True

    cookies_path = Path(ml_config.cookies_path)
    cookies_path.parent.mkdir(parents=True, exist_ok=True)

    reloader = MLCookieReloader(db=db, config=ml_config, reload_callback=reload_cb)
    new_cookies = [
        {"name": "fresh", "value": "abc", "domain": ".mercadolibre.com.mx"},
    ]
    ok = await reloader.apply(new_cookies)
    assert ok
    assert len(received) == 1 and received[0] == new_cookies

    rows = db.execute(
        "SELECT severity FROM runtime_events WHERE kind = ?", (RELOAD_OK_KIND,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["severity"] == "info"


@pytest.mark.asyncio
async def test_reloader_emits_error_when_callback_fails(db, ml_config):
    """Si el callback falla / retorna False, emite RELOAD_ERROR_KIND."""

    async def failing_cb(cookies):
        return False

    cookies_path = Path(ml_config.cookies_path)
    cookies_path.parent.mkdir(parents=True, exist_ok=True)

    reloader = MLCookieReloader(db=db, config=ml_config, reload_callback=failing_cb)
    ok = await reloader.apply([
        {"name": "x", "value": "y", "domain": ".mercadolibre.com.mx"},
    ])
    assert not ok
    rows = db.execute(
        "SELECT 1 FROM runtime_events WHERE kind = ?", (RELOAD_ERROR_KIND,)
    ).fetchall()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_reloader_works_without_existing_cookies_file(db, ml_config):
    """Si no existe archivo previo, debe escribir sin tratar de hacer backup."""

    async def reload_cb(cookies):
        return True

    cookies_path = Path(ml_config.cookies_path)
    if cookies_path.exists():
        cookies_path.unlink()

    reloader = MLCookieReloader(db=db, config=ml_config, reload_callback=reload_cb)
    ok = await reloader.apply([
        {"name": "fresh", "value": "v", "domain": ".mercadolibre.com.mx"},
    ])
    assert ok
    assert cookies_path.exists()
    backup_dir = cookies_path.parent / "cookies_backups"
    # No hay backup porque el archivo no existía antes.
    assert not backup_dir.exists() or len(list(backup_dir.iterdir())) == 0


# ---------------------------------------------------------------------------
# is_admin / reply_target — tolerancia a JID/LID/prefijo MX
# ---------------------------------------------------------------------------


def test_is_admin_matches_e164_exact():
    cfg = MLRecoveryConfig(admin_numbers=("528338498692",))
    assert cfg.is_admin("528338498692")


def test_is_admin_matches_with_mx_mobile_prefix():
    """Baileys añade `1` después del país a móviles MX."""
    cfg = MLRecoveryConfig(admin_numbers=("528338498692",))
    assert cfg.is_admin("5218338498692")
    assert cfg.is_admin("5218338498692@s.whatsapp.net")


def test_is_admin_matches_lid_exactly():
    cfg = MLRecoveryConfig(admin_numbers=("25877295435783@lid",))
    assert cfg.is_admin("25877295435783@lid")
    # Pero no debe matchear cualquier número con esos dígitos
    assert not cfg.is_admin("25877295435783")  # sin @lid


def test_is_admin_supports_multiple_admins_mixed_formats():
    cfg = MLRecoveryConfig(admin_numbers=("528338498692", "25877295435783@lid"))
    assert cfg.is_admin("5218338498692@s.whatsapp.net")
    assert cfg.is_admin("25877295435783@lid")
    assert not cfg.is_admin("999@lid")
    assert not cfg.is_admin("528112345678")


def test_is_admin_rejects_empty_or_unknown():
    cfg = MLRecoveryConfig(admin_numbers=("528338498692",))
    assert not cfg.is_admin("")
    assert not cfg.is_admin(None)  # type: ignore[arg-type]
    assert not cfg.is_admin("999000111222")


def test_reply_target_returns_number_when_input_is_number():
    cfg = MLRecoveryConfig(admin_numbers=("528338498692",))
    assert cfg.reply_target("5218338498692@s.whatsapp.net") == "5218338498692"
    assert cfg.reply_target("528338498692") == "528338498692"


def test_reply_target_falls_back_when_input_is_lid():
    cfg = MLRecoveryConfig(admin_numbers=("25877295435783@lid", "528338498692"))
    # Para responder a un `@lid` usamos el primer admin numérico.
    assert cfg.reply_target("25877295435783@lid") == "528338498692"


def test_reply_target_returns_none_when_no_numeric_admin():
    cfg = MLRecoveryConfig(admin_numbers=("25877295435783@lid",))
    assert cfg.reply_target("25877295435783@lid") is None
