"""Session manager para Mercado Libre (hot-reload sin reiniciar).

Orquesta el ciclo de vida de la sesión ML del bot:

    estado actual ←──────── runtime
        │
        │  /cookies_ml por WhatsApp
        ▼
    stage_cookies(...)     # secrets/incoming_cookies/mercadolibre_latest.json (chmod 600)
        │
        ▼
    validate_cookies(...)  # abre Chromium temporal → navega a ML → verifica
        │
        ├─ fail → conserva sesión vieja, status = invalid|challenge_required
        │
        └─ ok   → promote_cookies(...)        # secrets/mercadolibre_cookies.json
                  rotate_context(...)         # cierra context viejo, crea nuevo
                  mark_valid()

Diseño:

- **Estado en memoria** (`MLSessionStatus`) + emit `runtime_events` en cada
  transición. El estado se consulta antes de cada ciclo del hunter ML.
- **Validación real** con Chromium temporal. NO usa el browser persistente
  del bot (no contamina su profile y no bloquea sus operaciones). Por
  defecto usa Playwright async; en tests se inyecta un factory mock.
- **Hot rotation**: la rotación real del context la delega al
  `ServerContext` via callback. Esto evita acoplamiento con
  `PlaywrightBrowserWorker` y permite test con stubs.
- **Seguridad**: nunca loguea valores de cookies. `cookies_metadata()`
  produce sólo `count`, `domains`, `exp_min`, `exp_max`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Estados + DTOs
# ---------------------------------------------------------------------------


class MLSessionStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    WAITING_FOR_ADMIN_COOKIES = "waiting_for_admin_cookies"
    VALIDATING_RECEIVED_COOKIES = "validating_received_cookies"
    CHALLENGE_REQUIRED = "challenge_required"


# Eventos emitidos al cambiar estado.
STATE_CHANGE_KIND = "ml_session_state_changed"
VALIDATION_KIND = "ml_session_active_validation"
ROTATION_KIND = "ml_session_context_rotated"
PROMOTION_KIND = "ml_cookies_promoted"
STAGING_KIND = "ml_cookies_staged"


@dataclass(frozen=True)
class CookieSafetyMeta:
    """Metadata segura para logging (sin valores)."""

    count: int
    domains: tuple[str, ...]
    exp_min: Optional[float]
    exp_max: Optional[float]


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    reason: Optional[str] = None
    final_url: Optional[str] = None
    detected_signals: tuple[str, ...] = ()


# Razones de invalidez detectadas por el validador activo.
REASON_LOGIN_REDIRECT = "login_redirect"
REASON_QR_LOGIN = "qr_login"
REASON_CAPTCHA = "captcha"
REASON_CHALLENGE = "challenge"
REASON_TWOFA = "twofa_required"
REASON_BROWSER_UNAVAILABLE = "browser_unavailable"
REASON_NAVIGATION_FAILED = "navigation_failed"
REASON_UNEXPECTED = "unexpected"


# Tokens para detectar redirect/login en URL.
_LOGIN_URL_TOKENS = (
    "/gz/account-verification",
    "/jms/mlm/lgz/login",
    "/account-verification",
    "/login",
    "/auth/security/captcha",
    "challenge.mercadolibre",
)


# ---------------------------------------------------------------------------
# Helpers de tiempo / I/O
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: list[dict] | dict) -> None:
    """Escritura atómica con permisos 600 (best-effort en Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def cookies_metadata(cookies: Iterable[dict]) -> CookieSafetyMeta:
    """Genera metadata segura (sin valores). Útil para logs/eventos."""
    cookies_list = list(cookies)
    domains: set[str] = set()
    exp_values: list[float] = []
    for c in cookies_list:
        d = c.get("domain")
        if isinstance(d, str) and d:
            domains.add(d)
        for key in ("expires", "expirationDate", "expiry"):
            v = c.get(key)
            if isinstance(v, (int, float)) and v > 0:
                exp_values.append(float(v))
                break
    return CookieSafetyMeta(
        count=len(cookies_list),
        domains=tuple(sorted(domains)),
        exp_min=min(exp_values) if exp_values else None,
        exp_max=max(exp_values) if exp_values else None,
    )


# ---------------------------------------------------------------------------
# Browser factory para validación activa
# ---------------------------------------------------------------------------


# Tipo de factory: callable async que recibe `cookies` y URL, devuelve
# ValidationOutcome. Esto permite mockearlo en tests sin Playwright real.
ValidationBrowserFactory = Callable[[list[dict], str], Awaitable[ValidationOutcome]]


async def _default_playwright_validator(
    cookies: list[dict], url: str
) -> ValidationOutcome:
    """Implementación real con Playwright Chromium efímero.

    NO usa el `BrowserWorker` persistente del bot. Crea un `playwright`
    nuevo, un browser, un context, inyecta cookies, navega a `url`, y
    determina si la sesión sigue viva.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover - defensa
        logger.error("validate_cookies: playwright no disponible: %s", exc)
        return ValidationOutcome(ok=False, reason=REASON_BROWSER_UNAVAILABLE)

    p = None
    browser = None
    context = None
    try:
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="es-MX",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        # Normalizar cookies al formato que acepta Playwright.
        normalized = _normalize_cookies_for_playwright(cookies)
        if not normalized:
            return ValidationOutcome(ok=False, reason="no_normalizable_cookies")

        await context.add_cookies(normalized)

        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            logger.warning("validate_cookies: goto raised: %s", exc)
            return ValidationOutcome(ok=False, reason=REASON_NAVIGATION_FAILED)

        final_url = page.url or (response.url if response else url)

        # Heurística URL.
        lowered = (final_url or "").lower()
        signals: list[str] = []
        for tok in _LOGIN_URL_TOKENS:
            if tok in lowered:
                signals.append(f"url_contains:{tok}")
                return ValidationOutcome(
                    ok=False,
                    reason=REASON_LOGIN_REDIRECT,
                    final_url=final_url,
                    detected_signals=tuple(signals),
                )

        # Heurística DOM.
        try:
            html = await page.content()
        except Exception:
            html = ""
        html_lower = html.lower() if html else ""

        if any(
            kw in html_lower
            for kw in (
                'id="login_id"',
                "name=\"user_id\"",
                "input[name='password']",
                'data-testid="qr-code"',
                "qr-code-container",
                "scan_qr_to_login",
            )
        ):
            signals.append("dom_login_form_or_qr")
            reason = REASON_QR_LOGIN if "qr" in html_lower else REASON_LOGIN_REDIRECT
            return ValidationOutcome(
                ok=False,
                reason=reason,
                final_url=final_url,
                detected_signals=tuple(signals),
            )

        if any(
            kw in html_lower
            for kw in (
                "captcha",
                "verifica que eres humano",
                "robot check",
            )
        ):
            signals.append("dom_captcha")
            return ValidationOutcome(
                ok=False,
                reason=REASON_CAPTCHA,
                final_url=final_url,
                detected_signals=tuple(signals),
            )

        if any(
            kw in html_lower
            for kw in (
                "verifica tu identidad",
                "verificar tu identidad",
                "two-factor",
                "two_factor",
                "código de verificación",
            )
        ):
            signals.append("dom_2fa_or_challenge")
            return ValidationOutcome(
                ok=False,
                reason=REASON_TWOFA,
                final_url=final_url,
                detected_signals=tuple(signals),
            )

        # Señales positivas: presencia de menú de cuenta o saludo.
        positive = False
        for kw in (
            'data-testid="header-user"',
            'class="nav-header-username"',
            "/myaccount",
            "hola,",
            ">cerrar sesión<",
            "mi cuenta",
        ):
            if kw in html_lower:
                signals.append(f"dom_authenticated:{kw[:24]}")
                positive = True
                break

        if positive:
            return ValidationOutcome(
                ok=True,
                final_url=final_url,
                detected_signals=tuple(signals),
            )

        # Sin señales claras: si la URL es ML normal y no hay login,
        # asumimos válido por defecto. ML a veces no muestra el menú en
        # la home anónima ni en la home autenticada.
        if "mercadolibre.com" in lowered and not any(
            t in lowered for t in _LOGIN_URL_TOKENS
        ):
            signals.append("dom_inconclusive_but_no_login_signal")
            return ValidationOutcome(
                ok=True,
                final_url=final_url,
                detected_signals=tuple(signals),
            )

        return ValidationOutcome(
            ok=False,
            reason=REASON_UNEXPECTED,
            final_url=final_url,
            detected_signals=tuple(signals),
        )

    except Exception as exc:
        logger.exception("validate_cookies: error inesperado")
        return ValidationOutcome(ok=False, reason=f"{REASON_UNEXPECTED}: {exc}")
    finally:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                await p.stop()
        except Exception:
            pass


def _normalize_cookies_for_playwright(cookies: list[dict]) -> list[dict]:
    """Normaliza al formato que acepta `context.add_cookies`."""
    out: list[dict] = []
    samesite_map = {
        "no_restriction": "None",
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
        "unspecified": "None",
    }
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        if not (isinstance(name, str) and isinstance(value, str) and isinstance(domain, str)):
            continue
        n: dict = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path", "/") or "/",
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }
        # expires
        exp = c.get("expires")
        if exp is None:
            exp = c.get("expirationDate")
        if exp is None:
            exp = c.get("expiry")
        if c.get("session") is True:
            exp = None
        if isinstance(exp, (int, float)) and exp > 0:
            n["expires"] = float(exp)
        # sameSite
        ss = c.get("sameSite")
        if ss is None or ss == "":
            n["sameSite"] = "None"
        elif ss in ("Strict", "Lax", "None"):
            n["sameSite"] = ss
        elif isinstance(ss, str) and ss.lower() in samesite_map:
            n["sameSite"] = samesite_map[ss.lower()]
        else:
            n["sameSite"] = "None"
        out.append(n)
    return out


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


# Type del callback de rotation: típicamente `ctx.reload_ml_cookies(cookies)`.
RotationCallback = Callable[[list[dict]], Awaitable[bool]]


@dataclass
class MLSessionPaths:
    """Rutas relativas al workspace para staging/promotion."""

    staging_path: Path
    active_cookies_path: Path
    profile_cookies_path: Optional[Path] = None  # secrets/browser_profiles/mercadolibre/cookies.json

    @classmethod
    def default(cls, *, secrets_dir: Path) -> "MLSessionPaths":
        return cls(
            staging_path=secrets_dir / "incoming_cookies" / "mercadolibre_latest.json",
            active_cookies_path=secrets_dir / "mercadolibre_cookies.json",
            profile_cookies_path=secrets_dir / "browser_profiles" / "mercadolibre" / "cookies.json",
        )


class MercadoLibreSessionManager:
    """Núcleo del hot-reload de sesión ML.

    Métodos públicos clave (alineados con la spec):

    - `validate_cookies(cookies)` — valida en Chromium temporal. NO promueve.
    - `reload_from_cookies(cookies)` — orquesta stage → validate → promote → rotate.
    - `rotate_context(new_cookies)` — pide al ctx rotar el browser context ML.
    - `mark_invalid(reason)` / `mark_valid()` / `mark_waiting()` /
      `mark_validating()` / `mark_challenge_required()` — transiciones de estado.

    No depende del `MLInboundServer` (lo orquesta el runtime).
    """

    DEFAULT_VALIDATION_URL = "https://www.mercadolibre.com.mx/"

    def __init__(
        self,
        *,
        db: Optional[sqlite3.Connection],
        paths: MLSessionPaths,
        rotation_callback: Optional[RotationCallback] = None,
        validation_factory: Optional[ValidationBrowserFactory] = None,
        validation_url: Optional[str] = None,
        initial_status: MLSessionStatus = MLSessionStatus.VALID,
    ) -> None:
        self.db = db
        self.paths = paths
        self._rotation_cb = rotation_callback
        self._validate = validation_factory or _default_playwright_validator
        self._validation_url = validation_url or self.DEFAULT_VALIDATION_URL
        self._status = initial_status
        self._reason: Optional[str] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public state API
    # ------------------------------------------------------------------

    @property
    def status(self) -> MLSessionStatus:
        return self._status

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    def is_valid(self) -> bool:
        return self._status == MLSessionStatus.VALID

    def snapshot(self) -> dict:
        return {
            "status": self._status.value,
            "reason": self._reason,
            "validation_url": self._validation_url,
            "staging_path": str(self.paths.staging_path),
            "active_path": str(self.paths.active_cookies_path),
        }

    def mark_valid(self) -> None:
        self._transition(MLSessionStatus.VALID, reason=None)

    def mark_invalid(self, reason: str) -> None:
        self._transition(MLSessionStatus.INVALID, reason=reason)

    def mark_waiting(self) -> None:
        self._transition(
            MLSessionStatus.WAITING_FOR_ADMIN_COOKIES, reason=self._reason
        )

    def mark_validating(self) -> None:
        self._transition(MLSessionStatus.VALIDATING_RECEIVED_COOKIES, reason=None)

    def mark_challenge_required(self, reason: str = "challenge_required") -> None:
        self._transition(MLSessionStatus.CHALLENGE_REQUIRED, reason=reason)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def validate_cookies(self, cookies: list[dict]) -> ValidationOutcome:
        """Valida cookies abriendo Chromium temporal. NO promueve nada."""
        meta = cookies_metadata(cookies)
        self._emit_event(
            VALIDATION_KIND,
            "info",
            {
                "phase": "start",
                "url": self._validation_url,
                "cookie_meta": _meta_to_payload(meta),
            },
        )
        try:
            # Timeout de 60s para que una validación colgada no bloquee
            # el sistema indefinidamente.
            outcome = await asyncio.wait_for(
                self._validate(cookies, self._validation_url),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("validate_cookies: timeout (60s) — marcando como fallo")
            outcome = ValidationOutcome(
                ok=False, reason=f"{REASON_NAVIGATION_FAILED}:timeout"
            )
        except Exception as exc:
            logger.exception("validate_cookies factory raised")
            outcome = ValidationOutcome(
                ok=False, reason=f"{REASON_UNEXPECTED}: {exc}"
            )
        self._emit_event(
            VALIDATION_KIND,
            "info" if outcome.ok else "warning",
            {
                "phase": "end",
                "ok": outcome.ok,
                "reason": outcome.reason,
                "final_url": outcome.final_url,
                "signals": list(outcome.detected_signals),
                "cookie_meta": _meta_to_payload(meta),
            },
        )
        return outcome

    # ------------------------------------------------------------------
    # Staging + Promotion
    # ------------------------------------------------------------------

    def stage_cookies(self, cookies: list[dict]) -> Path:
        """Escribe cookies en `staging_path` con chmod 600. NO promueve."""
        _atomic_write_json(self.paths.staging_path, cookies)
        meta = cookies_metadata(cookies)
        self._emit_event(
            STAGING_KIND,
            "info",
            {
                "path": str(self.paths.staging_path),
                "cookie_meta": _meta_to_payload(meta),
            },
        )
        return self.paths.staging_path

    def promote_cookies(self, cookies: list[dict]) -> tuple[Path, Optional[Path]]:
        """Copia cookies del staging a la sesión activa.

        Escribe a `active_cookies_path` y, si está configurado, también a
        `profile_cookies_path`. Permisos 600 best-effort.
        """
        _atomic_write_json(self.paths.active_cookies_path, cookies)
        profile_written: Optional[Path] = None
        if self.paths.profile_cookies_path is not None:
            try:
                _atomic_write_json(self.paths.profile_cookies_path, cookies)
                profile_written = self.paths.profile_cookies_path
            except Exception:
                logger.exception(
                    "promote_cookies: no se pudo escribir profile cookies en %s",
                    self.paths.profile_cookies_path,
                )

        meta = cookies_metadata(cookies)
        self._emit_event(
            PROMOTION_KIND,
            "info",
            {
                "active_path": str(self.paths.active_cookies_path),
                "profile_path": str(profile_written) if profile_written else None,
                "cookie_meta": _meta_to_payload(meta),
            },
        )
        return self.paths.active_cookies_path, profile_written

    # ------------------------------------------------------------------
    # Hot rotation
    # ------------------------------------------------------------------

    async def rotate_context(self, new_cookies: list[dict]) -> bool:
        """Pide al ctx rotar el browser context ML con cookies nuevas.

        El callback típico es `ctx.reload_ml_cookies(cookies)` que cierra el
        context viejo + crea uno nuevo + recarga cookies. Si no hay
        callback, retorna True (sin operación, las cookies se aplicarán al
        próximo arranque del browser).
        """
        if self._rotation_cb is None:
            self._emit_event(
                ROTATION_KIND,
                "warning",
                {
                    "ok": True,
                    "note": "no_rotation_callback_configured",
                    "cookie_count": len(new_cookies),
                },
            )
            return True

        ok = False
        error: Optional[str] = None
        try:
            ok = bool(await self._rotation_cb(new_cookies))
        except Exception as exc:
            logger.exception("rotate_context: callback raised")
            error = str(exc)
            ok = False

        self._emit_event(
            ROTATION_KIND,
            "info" if ok else "error",
            {
                "ok": ok,
                "error": error,
                "cookie_count": len(new_cookies),
            },
        )
        return ok

    # ------------------------------------------------------------------
    # Pipeline completo
    # ------------------------------------------------------------------

    async def reload_from_cookies(
        self, cookies: list[dict]
    ) -> tuple[bool, Optional[ValidationOutcome]]:
        """Pipeline canónico: stage → validate → promote → rotate.

        Devuelve `(ok, validation_outcome)`. Si validation falla, NO se
        promueve ni se rota; conservamos la sesión actual.
        """
        async with self._lock:
            try:
                self.stage_cookies(cookies)
            except Exception as exc:
                logger.exception("reload_from_cookies: stage falló")
                self.mark_invalid(f"stage_failed: {exc}")
                return False, None

            self.mark_validating()

            try:
                outcome = await asyncio.wait_for(
                    self.validate_cookies(cookies),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                logger.warning("reload_from_cookies: validate_cookies timeout (90s)")
                outcome = ValidationOutcome(
                    ok=False, reason=f"{REASON_NAVIGATION_FAILED}:pipeline_timeout"
                )
            except Exception as exc:
                logger.exception("reload_from_cookies: validate_cookies raised")
                outcome = ValidationOutcome(
                    ok=False, reason=f"{REASON_UNEXPECTED}: {exc}"
                )

            if not outcome.ok:
                # Mantener sesión vieja. Estado depende de la razón.
                if outcome.reason in (REASON_TWOFA, REASON_CHALLENGE):
                    self.mark_challenge_required(outcome.reason)
                else:
                    self.mark_invalid(outcome.reason or "validation_failed")
                return False, outcome

            try:
                self.promote_cookies(cookies)
            except Exception as exc:
                logger.exception("reload_from_cookies: promote falló")
                self.mark_invalid(f"promote_failed: {exc}")
                return False, outcome

            rotated = await self.rotate_context(cookies)
            if not rotated:
                self.mark_invalid("rotate_failed")
                return False, outcome

            self.mark_valid()
            return True, outcome

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _transition(self, new_status: MLSessionStatus, *, reason: Optional[str]) -> None:
        previous = self._status
        self._status = new_status
        self._reason = reason
        self._emit_event(
            STATE_CHANGE_KIND,
            "info" if new_status == MLSessionStatus.VALID else "warning",
            {
                "previous": previous.value,
                "current": new_status.value,
                "reason": reason,
            },
        )

    def _emit_event(self, kind: str, severity: str, payload: dict) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (kind, severity, json.dumps(payload, ensure_ascii=False), _iso_now()),
            )
            self.db.commit()
        except Exception:
            logger.exception("MercadoLibreSessionManager: emit_event falló")


def _meta_to_payload(meta: CookieSafetyMeta) -> dict:
    return {
        "count": meta.count,
        "domains": list(meta.domains),
        "exp_min": meta.exp_min,
        "exp_max": meta.exp_max,
    }


__all__ = [
    "CookieSafetyMeta",
    "MLSessionPaths",
    "MLSessionStatus",
    "MercadoLibreSessionManager",
    "PROMOTION_KIND",
    "ROTATION_KIND",
    "STAGING_KIND",
    "STATE_CHANGE_KIND",
    "VALIDATION_KIND",
    "ValidationBrowserFactory",
    "ValidationOutcome",
    "cookies_metadata",
    "REASON_BROWSER_UNAVAILABLE",
    "REASON_CAPTCHA",
    "REASON_CHALLENGE",
    "REASON_LOGIN_REDIRECT",
    "REASON_NAVIGATION_FAILED",
    "REASON_QR_LOGIN",
    "REASON_TWOFA",
    "REASON_UNEXPECTED",
]
