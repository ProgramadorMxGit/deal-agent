"""Tests del CookieStore (carga, sanitización, save)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ofertas_hunter.session.cookie_store import (
    CookieHealth,
    CookieStore,
    resolve_cookie_path,
    sanitize_cookie,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "cookies"


def test_ml_loads_legacy_cookie_format():
    store = CookieStore(path=FIXTURES / "legacy_extension_format.json")
    cookies, health = store.load()
    # 4 entries en el archivo, 2 válidas (las 2 cookies bien formadas).
    assert health.total_in_file == 4
    assert health.loaded == 2
    assert health.is_empty is False
    assert health.healthy is True

    # Las cookies cargadas tienen 'expires' (no 'expirationDate'):
    for cookie in cookies:
        assert "expirationDate" not in cookie
        assert "hostOnly" not in cookie
        assert "storeId" not in cookie
        assert "session" not in cookie


def test_ml_normalizes_cookies_for_playwright():
    store = CookieStore(path=FIXTURES / "legacy_extension_format.json")
    cookies, _ = store.load()
    # 'sameSite' del legacy: 'no_restriction' → 'None', 'lax' → 'Lax'.
    samesites = sorted([c.get("sameSite") for c in cookies if "sameSite" in c])
    assert samesites == ["Lax", "None"]
    # 'expires' es float
    for cookie in cookies:
        if "expires" in cookie:
            assert isinstance(cookie["expires"], float)


def test_ml_detects_missing_cookies(tmp_path: Path):
    store = CookieStore(path=tmp_path / "no_existe.json")
    cookies, health = store.load()
    assert cookies == []
    assert health.is_missing is True
    assert health.is_empty is True
    assert health.healthy is False


def test_cookie_store_loads_utf8_bom_json(tmp_path: Path):
    target = tmp_path / "bom.json"
    payload = '[{"name":"foo","value":"bar","domain":".amazon.com.mx","path":"/"}]'
    target.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

    store = CookieStore(path=target)
    cookies, health = store.load()

    assert health.is_missing is False
    assert health.loaded == 1
    assert cookies[0]["name"] == "foo"


def test_ml_detects_expired_or_empty_cookies():
    store = CookieStore(path=FIXTURES / "empty.json")
    cookies, health = store.load()
    assert cookies == []
    assert health.is_missing is False
    assert health.is_empty is True
    assert health.healthy is False


def test_ml_saves_cookies_on_exit(tmp_path: Path):
    target = tmp_path / "out.json"
    store = CookieStore(path=target)

    cookies = [
        {
            "name": "foo",
            "value": "bar",
            "domain": ".mercadolibre.com.mx",
            "path": "/",
            "expires": 9999999999.0,
            "secure": True,
            "sameSite": "Lax",
        }
    ]
    saved_path = store.save(cookies)
    assert saved_path == target
    assert target.exists()
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk[0]["name"] == "foo"
    assert on_disk[0]["sameSite"] == "Lax"


def test_resolve_cookie_path_uses_primary_when_exists(tmp_path: Path):
    f = tmp_path / "primary.json"
    f.write_text("[]", encoding="utf-8")
    resolved = resolve_cookie_path(primary=str(f))
    assert resolved == f


def test_resolve_cookie_path_falls_back_to_env(monkeypatch, tmp_path: Path):
    f = tmp_path / "env.json"
    f.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH", str(f))
    resolved = resolve_cookie_path(
        primary=None,
        env_vars=("BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH",),
        default="not-exists.json",
    )
    assert resolved == f


def test_resolve_cookie_path_uses_default_when_nothing_exists(tmp_path: Path):
    resolved = resolve_cookie_path(
        primary=str(tmp_path / "missing.json"),
        env_vars=(),
        default=str(tmp_path / "default.json"),
    )
    # Devuelve el primary aunque no exista (para que el load() lo reporte).
    assert resolved == tmp_path / "missing.json"


def test_sanitize_cookie_handles_invalid_input():
    assert sanitize_cookie({"name": "foo"}) is None  # falta value
    assert sanitize_cookie({}) is None
    assert sanitize_cookie("not a dict") is None  # type: ignore[arg-type]


def test_expiring_soon_counter():
    store = CookieStore(path=FIXTURES / "playwright_format.json", expiry_warn_seconds=10**12)
    _, health = store.load()
    # Con warn_seconds gigante, todas las cookies cuentan como "expiring_soon".
    assert health.expiring_soon >= 1
