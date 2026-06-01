"""Tests del ``MercadoLibreSessionManager`` (núcleo del hot-reload).

Cubre:

- Estados explícitos (VALID, INVALID, WAITING_FOR_ADMIN_COOKIES, ...).
- Pipeline canónico: stage → validate → promote → rotate.
- Validation factory mockeado (sin Playwright real).
- ``stage_cookies`` escribe en ``incoming_cookies/`` con permisos restrictivos.
- Si validation falla, NO se promueve y NO se llama rotation_callback.
- ``cookies_metadata`` no incluye valores de cookies.
- Tests obligatorios A-F del spec del usuario.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.session.ml_session_manager import (
    MLSessionPaths,
    MLSessionStatus,
    MercadoLibreSessionManager,
    PROMOTION_KIND,
    ROTATION_KIND,
    STAGING_KIND,
    STATE_CHANGE_KIND,
    VALIDATION_KIND,
    ValidationOutcome,
    cookies_metadata,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def paths(tmp_path):
    return MLSessionPaths(
        staging_path=tmp_path / "incoming_cookies" / "mercadolibre_latest.json",
        active_cookies_path=tmp_path / "ml_cookies.json",
        profile_cookies_path=tmp_path
        / "browser_profiles"
        / "mercadolibre"
        / "cookies.json",
    )


def _ml_cookies(n: int = 2) -> list[dict]:
    return [
        {"name": f"c{i}", "value": f"v{i}", "domain": ".mercadolibre.com.mx"}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# cookies_metadata: seguro, no incluye valores
# ---------------------------------------------------------------------------


def test_cookies_metadata_does_not_include_values():
    cookies = [
        {"name": "x", "value": "SECRET_VALUE_1", "domain": "a.com"},
        {"name": "y", "value": "SECRET_VALUE_2", "domain": "b.com"},
    ]
    meta = cookies_metadata(cookies)
    rendered = json.dumps(
        {
            "count": meta.count,
            "domains": list(meta.domains),
            "exp_min": meta.exp_min,
            "exp_max": meta.exp_max,
        }
    )
    assert "SECRET_VALUE_1" not in rendered
    assert "SECRET_VALUE_2" not in rendered
    assert meta.count == 2
    assert "a.com" in meta.domains
    assert "b.com" in meta.domains


def test_cookies_metadata_collects_expirations():
    cookies = [
        {"name": "a", "value": "v", "domain": "d", "expires": 1000.0},
        {"name": "b", "value": "v", "domain": "d", "expirationDate": 2000.0},
        {"name": "c", "value": "v", "domain": "d"},
    ]
    meta = cookies_metadata(cookies)
    assert meta.exp_min == 1000.0
    assert meta.exp_max == 2000.0


# ---------------------------------------------------------------------------
# Estado / transiciones
# ---------------------------------------------------------------------------


def test_initial_status_is_valid_by_default(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    assert m.status == MLSessionStatus.VALID
    assert m.is_valid()


def test_mark_invalid_emits_state_change(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    m.mark_invalid("login_redirect")
    assert m.status == MLSessionStatus.INVALID
    assert m.reason == "login_redirect"
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind = ?",
        (STATE_CHANGE_KIND,),
    ).fetchall()
    assert len(rows) >= 1
    payload = json.loads(rows[-1]["payload_json"])
    assert payload["current"] == "invalid"
    assert payload["reason"] == "login_redirect"


def test_mark_valid_resets_reason(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    m.mark_invalid("oops")
    m.mark_valid()
    assert m.status == MLSessionStatus.VALID
    assert m.reason is None


def test_mark_challenge_required(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    m.mark_challenge_required("twofa_required")
    assert m.status == MLSessionStatus.CHALLENGE_REQUIRED


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def test_stage_cookies_writes_to_staging_path(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    cookies = _ml_cookies(2)
    out = m.stage_cookies(cookies)
    assert out == paths.staging_path
    assert paths.staging_path.exists()
    data = json.loads(paths.staging_path.read_text(encoding="utf-8"))
    assert data == cookies

    # Active path NO se toca durante staging
    assert not paths.active_cookies_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 600 no aplica en Windows")
def test_stage_cookies_writes_with_chmod_600(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    m.stage_cookies(_ml_cookies(1))
    mode = paths.staging_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_stage_cookies_emits_safe_event(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    m.stage_cookies([
        {"name": "n", "value": "SECRET", "domain": ".mercadolibre.com.mx"},
    ])
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind = ?",
        (STAGING_KIND,),
    ).fetchall()
    assert len(rows) == 1
    payload = rows[0]["payload_json"]
    # Seguridad: el evento NO debe contener el valor de la cookie.
    assert "SECRET" not in payload


# ---------------------------------------------------------------------------
# Validación activa (factory mock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_cookies_uses_factory(db, paths):
    captured: list[tuple[list, str]] = []

    async def factory(cookies, url):
        captured.append((cookies, url))
        return ValidationOutcome(ok=True, final_url=url)

    m = MercadoLibreSessionManager(
        db=db, paths=paths, validation_factory=factory
    )
    cookies = _ml_cookies(1)
    outcome = await m.validate_cookies(cookies)
    assert outcome.ok
    assert len(captured) == 1
    assert captured[0][0] == cookies


@pytest.mark.asyncio
async def test_validate_cookies_logs_safe_metadata(db, paths):
    async def factory(cookies, url):
        return ValidationOutcome(ok=False, reason="login_redirect")

    m = MercadoLibreSessionManager(
        db=db, paths=paths, validation_factory=factory
    )
    await m.validate_cookies([
        {"name": "n", "value": "SECRET", "domain": ".mercadolibre.com.mx"},
    ])
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind = ?",
        (VALIDATION_KIND,),
    ).fetchall()
    for row in rows:
        assert "SECRET" not in row["payload_json"]


# ---------------------------------------------------------------------------
# Pipeline reload_from_cookies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_pipeline_happy_path(db, paths):
    """Test obligatorio D: cookies válidas → promote + rotate."""
    rotated: list[list[dict]] = []

    async def rot_cb(cookies):
        rotated.append(cookies)
        return True

    async def factory(cookies, url):
        return ValidationOutcome(ok=True, final_url=url)

    m = MercadoLibreSessionManager(
        db=db,
        paths=paths,
        rotation_callback=rot_cb,
        validation_factory=factory,
    )

    cookies = _ml_cookies(2)
    ok, outcome = await m.reload_from_cookies(cookies)
    assert ok
    assert outcome.ok
    # active path written
    assert paths.active_cookies_path.exists()
    saved = json.loads(paths.active_cookies_path.read_text(encoding="utf-8"))
    assert saved == cookies
    # profile path written
    assert paths.profile_cookies_path is not None
    assert paths.profile_cookies_path.exists()
    # staging written
    assert paths.staging_path.exists()
    # rotation callback fue llamado
    assert len(rotated) == 1
    # estado final = VALID
    assert m.status == MLSessionStatus.VALID


@pytest.mark.asyncio
async def test_reload_pipeline_validation_fails_does_not_promote(tmp_path, db, paths):
    """Test obligatorio C: si validate falla, NO se promueve, NO se rota."""
    paths.active_cookies_path.parent.mkdir(parents=True, exist_ok=True)
    paths.active_cookies_path.write_text(
        json.dumps([{"name": "OLD", "value": "x", "domain": ".mercadolibre.com.mx"}]),
        encoding="utf-8",
    )

    rotated: list = []

    async def rot_cb(cookies):
        rotated.append(cookies)
        return True

    async def factory(cookies, url):
        return ValidationOutcome(ok=False, reason="login_redirect")

    m = MercadoLibreSessionManager(
        db=db,
        paths=paths,
        rotation_callback=rot_cb,
        validation_factory=factory,
    )

    new_cookies = _ml_cookies(1)
    ok, outcome = await m.reload_from_cookies(new_cookies)
    assert not ok
    assert outcome is not None and outcome.ok is False

    # active NO fue sobreescrito
    on_disk = json.loads(paths.active_cookies_path.read_text(encoding="utf-8"))
    assert on_disk[0]["name"] == "OLD"

    # rotation NO se ejecutó
    assert rotated == []

    # estado quedó en INVALID
    assert m.status == MLSessionStatus.INVALID

    # staging SÍ fue escrito
    assert paths.staging_path.exists()


@pytest.mark.asyncio
async def test_reload_pipeline_marks_challenge_when_2fa(tmp_path, db, paths):
    async def factory(cookies, url):
        return ValidationOutcome(ok=False, reason="twofa_required")

    m = MercadoLibreSessionManager(
        db=db, paths=paths, validation_factory=factory
    )

    ok, _ = await m.reload_from_cookies(_ml_cookies(1))
    assert not ok
    assert m.status == MLSessionStatus.CHALLENGE_REQUIRED


@pytest.mark.asyncio
async def test_reload_pipeline_rotate_failure_marks_invalid(db, paths):
    async def factory(cookies, url):
        return ValidationOutcome(ok=True, final_url=url)

    async def rot_cb(cookies):
        return False  # rotation falla

    m = MercadoLibreSessionManager(
        db=db,
        paths=paths,
        rotation_callback=rot_cb,
        validation_factory=factory,
    )

    ok, _ = await m.reload_from_cookies(_ml_cookies(1))
    assert not ok
    assert m.status == MLSessionStatus.INVALID
    assert m.reason == "rotate_failed"


@pytest.mark.asyncio
async def test_reload_pipeline_emits_promotion_and_rotation_events(db, paths):
    async def factory(cookies, url):
        return ValidationOutcome(ok=True)

    async def rot_cb(cookies):
        return True

    m = MercadoLibreSessionManager(
        db=db,
        paths=paths,
        rotation_callback=rot_cb,
        validation_factory=factory,
    )
    await m.reload_from_cookies(_ml_cookies(1))

    kinds = [
        r["kind"]
        for r in db.execute(
            "SELECT kind FROM runtime_events ORDER BY id"
        ).fetchall()
    ]
    assert STAGING_KIND in kinds
    assert PROMOTION_KIND in kinds
    assert ROTATION_KIND in kinds


# ---------------------------------------------------------------------------
# rotate_context sin callback no crashea
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_context_without_callback_returns_true(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    ok = await m.rotate_context([{"name": "x", "value": "y", "domain": "d"}])
    assert ok is True


# ---------------------------------------------------------------------------
# is_valid() helper
# ---------------------------------------------------------------------------


def test_is_valid_returns_correct_value_for_each_state(db, paths):
    m = MercadoLibreSessionManager(db=db, paths=paths)
    assert m.is_valid()
    m.mark_invalid("x")
    assert not m.is_valid()
    m.mark_validating()
    assert not m.is_valid()
    m.mark_waiting()
    assert not m.is_valid()
    m.mark_challenge_required()
    assert not m.is_valid()
    m.mark_valid()
    assert m.is_valid()
