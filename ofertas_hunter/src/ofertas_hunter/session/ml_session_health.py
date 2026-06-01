"""Health check de la sesión de Mercado Libre.

Comprueba periódicamente si la sesión ML sigue viva (cookies presentes +
una validación activa contra una URL real, p.ej. `/ofertas`). Clasifica el
estado en {ok, login_required, expired, blocked, unknown}, aplica backoff
cuando se requiere login (para no quemar ciclos ML que igual irían a login)
y emite el evento `ml_session_health`.

NO publica, NO toca outbox, NO toca gates ni precios, NO escribe cookies.
Solo lee cookies/locks y navega en un Chromium temporal para diagnosticar.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .cookie_store import CookieStore, resolve_cookie_path
from .ml_session_manager import (
    REASON_CAPTCHA,
    REASON_CHALLENGE,
    REASON_LOGIN_REDIRECT,
    REASON_NAVIGATION_FAILED,
    REASON_QR_LOGIN,
    REASON_TWOFA,
    ValidationOutcome,
    _default_playwright_validator,
)

logger = logging.getLogger(__name__)

EVENT_HEALTH = "ml_session_health"

# Estados expuestos.
STATUS_OK = "ok"
STATUS_LOGIN_REQUIRED = "login_required"
STATUS_EXPIRED = "expired"
STATUS_BLOCKED = "blocked"
STATUS_UNKNOWN = "unknown"

# URL por defecto a validar: una página de ofertas (lo que más sufre el
# login_redirect en producción).
DEFAULT_CHECK_URL = "https://www.mercadolibre.com.mx/ofertas"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class MLHealthConfig:
    enabled: bool = True
    every_minutes: int = 30
    login_required_backoff_minutes: int = 60
    check_url: str = DEFAULT_CHECK_URL

    @classmethod
    def from_settings(cls, s) -> "MLHealthConfig":
        g = lambda n, d: getattr(s, n, d)
        return cls(
            enabled=bool(g("ml_session_health_enabled", True)),
            every_minutes=int(g("ml_session_health_every_minutes", 30)),
            login_required_backoff_minutes=int(g("ml_session_login_required_backoff_minutes", 60)),
            check_url=str(g("ml_session_health_check_url", DEFAULT_CHECK_URL)),
        )


@dataclass
class MLHealthResult:
    status: str
    checked_url: str
    redirected_to: Optional[str] = None
    has_login_form: bool = False
    has_captcha: bool = False
    cookies_count: int = 0
    profile_locked: bool = False
    last_success_at: Optional[str] = None
    backoff_until: Optional[str] = None
    timestamp: str = ""
    reason: Optional[str] = None

    def as_payload(self) -> dict:
        return dict(self.__dict__)


# Mapea la razón del validador a un status de salud.
def _classify(outcome: ValidationOutcome) -> tuple[str, bool, bool]:
    """Devuelve (status, has_login_form, has_captcha)."""
    if outcome.ok:
        return STATUS_OK, False, False
    reason = outcome.reason or ""
    signals = " ".join(outcome.detected_signals or ())
    has_login = ("login" in reason) or ("qr" in reason) or ("dom_login" in signals)
    has_captcha = (reason == REASON_CAPTCHA) or ("captcha" in signals)
    if reason in (REASON_CAPTCHA, REASON_CHALLENGE, REASON_TWOFA):
        return STATUS_BLOCKED, has_login, has_captcha
    if reason in (REASON_LOGIN_REDIRECT, REASON_QR_LOGIN):
        return STATUS_LOGIN_REQUIRED, True, has_captcha
    if reason == REASON_NAVIGATION_FAILED:
        return STATUS_UNKNOWN, has_login, has_captcha
    return STATUS_UNKNOWN, has_login, has_captcha


class MLSessionHealthChecker:
    """Ejecuta el health check ML con throttle + backoff + emisión de evento."""

    def __init__(
        self,
        *,
        db: sqlite3.Connection,
        config: MLHealthConfig,
        cookie_path: Optional[str | Path] = None,
        ml_profile_dir: Optional[Path] = None,
        validator: Optional[Callable[[list, str], Awaitable[ValidationOutcome]]] = None,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self.db = db
        self.config = config
        self._cookie_path = cookie_path
        self._ml_profile_dir = ml_profile_dir
        self._validator = validator or _default_playwright_validator
        self._clock = clock
        self._backoff_until: Optional[datetime] = None
        self._last_success_at: Optional[datetime] = None
        self._last_check_at: Optional[datetime] = None

    # ------------------------------------------------------------------

    def in_backoff(self) -> bool:
        return self._backoff_until is not None and self._clock() < self._backoff_until

    def due(self) -> bool:
        """True si toca correr el check (respeta `every_minutes`)."""
        if not self.config.enabled:
            return False
        if self._last_check_at is None:
            return True
        delta = (self._clock() - self._last_check_at).total_seconds()
        return delta >= self.config.every_minutes * 60

    def _profile_locked(self) -> bool:
        """True si el perfil ML tiene un lock activo (otro proceso lo usa)."""
        if self._ml_profile_dir is None:
            return False
        try:
            for lock in ("SingletonLock", ".profile.lock"):
                if (Path(self._ml_profile_dir) / lock).exists():
                    return True
        except Exception:
            return False
        return False

    def _load_cookies(self) -> tuple[list, int]:
        try:
            path = resolve_cookie_path(primary=self._cookie_path)
            store = CookieStore.for_path(path)
            cookies, health = store.load()
            return cookies, health.loaded
        except Exception:
            logger.exception("ml_session_health: load cookies falló")
            return [], 0

    async def check(self, *, force: bool = False) -> Optional[MLHealthResult]:
        """Ejecuta el health check. Devuelve None si no toca correr todavía."""
        if not force and not self.due():
            return None
        now = self._clock()
        self._last_check_at = now

        cookies, cookies_count = self._load_cookies()
        profile_locked = self._profile_locked()

        # Sin cookies → expired (no hay sesión que validar).
        if cookies_count == 0:
            result = MLHealthResult(
                status=STATUS_EXPIRED,
                checked_url=self.config.check_url,
                cookies_count=0,
                profile_locked=profile_locked,
                last_success_at=_iso(self._last_success_at) if self._last_success_at else None,
                backoff_until=_iso(self._backoff_until) if self._backoff_until else None,
                timestamp=_iso(now),
                reason="no_cookies",
            )
            self._apply_backoff_if_needed(result, now)
            self._emit(result)
            return result

        try:
            outcome = await self._validator(cookies, self.config.check_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ml_session_health: validator error: %s", exc)
            outcome = ValidationOutcome(ok=False, reason=f"validator_error:{exc}")

        status, has_login, has_captcha = _classify(outcome)
        if status == STATUS_OK:
            self._last_success_at = now

        result = MLHealthResult(
            status=status,
            checked_url=self.config.check_url,
            redirected_to=outcome.final_url,
            has_login_form=has_login,
            has_captcha=has_captcha,
            cookies_count=cookies_count,
            profile_locked=profile_locked,
            last_success_at=_iso(self._last_success_at) if self._last_success_at else None,
            timestamp=_iso(now),
            reason=outcome.reason,
        )
        self._apply_backoff_if_needed(result, now)
        result.backoff_until = _iso(self._backoff_until) if self._backoff_until else None
        self._emit(result)
        return result

    def _apply_backoff_if_needed(self, result: MLHealthResult, now: datetime) -> None:
        """Si login_required/blocked, fija backoff para no quemar ciclos ML."""
        if result.status in (STATUS_LOGIN_REQUIRED, STATUS_BLOCKED, STATUS_EXPIRED):
            self._backoff_until = now + timedelta(
                minutes=self.config.login_required_backoff_minutes
            )
            result.backoff_until = _iso(self._backoff_until)
        elif result.status == STATUS_OK:
            self._backoff_until = None

    def _emit(self, result: MLHealthResult) -> None:
        if self.db is None:
            return
        severity = "info" if result.status == STATUS_OK else "warning"
        try:
            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (EVENT_HEALTH, severity, json.dumps(result.as_payload(), ensure_ascii=False),
                 _iso(self._clock())),
            )
            self.db.commit()
        except Exception:
            logger.exception("ml_session_health: emit evento falló")


__all__ = [
    "MLHealthConfig",
    "MLHealthResult",
    "MLSessionHealthChecker",
    "EVENT_HEALTH",
    "STATUS_OK",
    "STATUS_LOGIN_REQUIRED",
    "STATUS_EXPIRED",
    "STATUS_BLOCKED",
    "STATUS_UNKNOWN",
]
