"""ML session recovery: detector + alerta admin + hot-reload.

Componentes:

- ``MLSessionMonitor``: detecta `cookie_expiry` en runtime + manda alerta
  al admin con instrucciones para enviar el nuevo JSON. Aplica
  cooldown anti-spam.

- ``MLCookieValidator``: valida estructura mínima del JSON que llega del
  admin. No hace contacto con ML, sólo verifica formato.

- ``MLCookieReloader``: hot-reload sobre el browser ML actual:
  guarda backups, escribe el JSON nuevo, llama a `ctx.reload_ml_cookies()`,
  resetea flag `paused`, emite `ml_cookies_reloaded`.

Estos tres componentes son orquestados por un servidor HTTP minimal
(`ml_session_inbound.py`) que recibe el webhook de Evolution API.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------


ALERT_KIND = "ml_session_admin_alerted"
ALERT_SKIPPED_KIND = "ml_session_alert_skipped_cooldown"
RECEIVED_KIND = "ml_cookies_received"
VALIDATED_KIND = "ml_cookies_validated"
RELOAD_OK_KIND = "ml_cookies_reloaded"
RELOAD_ERROR_KIND = "ml_cookies_reload_failed"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Settings DTO (subset para que tests no necesiten Settings completo)
# ---------------------------------------------------------------------------


@dataclass
class MLRecoveryConfig:
    enabled: bool = True
    alert_channel: str = "whatsapp"
    admin_numbers: tuple[str, ...] = ()
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_timeout_seconds: float = 15.0
    alert_cooldown_seconds: int = 1800
    inbound_enabled: bool = True
    inbound_host: str = "127.0.0.1"
    inbound_port: int = 9099
    inbound_secret: Optional[str] = None
    cookie_backup_count: int = 5
    cookies_path: str = "secrets/mercadolibre_cookies.json"

    def is_admin(self, number: str) -> bool:
        """Comprueba si ``number`` corresponde a algún admin configurado.

        Acepta dos formatos en ``admin_numbers``:

        - **Números E.164** (con o sin prefijo de móvil ``1`` añadido por
          WhatsApp/Baileys). Comparación tolerante por sufijo de 10
          dígitos para que ``5218338498692`` matchee con admin
          ``528338498692``.
        - **JID/LID** (identificadores de Baileys: ``25877295435783@lid``,
          ``5218338498692@s.whatsapp.net``). Si el admin configurado
          incluye ``@``, exigimos coincidencia exacta del local-part Y
          del sufijo (e.g. admin ``X@lid`` no matchea con números
          planos del estilo ``X`` por seguridad: un LID es opaco).

        Para JIDs ``@s.whatsapp.net`` también admitimos números planos
        equivalentes (porque WhatsApp normalmente usa el número como
        local-part).
        """
        if not number:
            return False
        number = str(number).strip()
        cand_local, _, cand_suffix_jid = number.partition("@")
        cand_digits = "".join(ch for ch in cand_local if ch.isdigit())
        cand_short = cand_digits[-10:] if len(cand_digits) >= 10 else cand_digits

        for admin in self.admin_numbers:
            admin = str(admin).strip()
            if not admin:
                continue
            admin_local, _, admin_suffix_jid = admin.partition("@")
            admin_digits = "".join(ch for ch in admin_local if ch.isdigit())
            admin_short = admin_digits[-10:] if len(admin_digits) >= 10 else admin_digits

            if admin_suffix_jid:
                # Admin configurado como JID/LID. Exigimos que el input
                # también tenga el mismo sufijo @ (lid != s.whatsapp.net)
                # y que el local-part sea igual.
                if admin_suffix_jid == cand_suffix_jid and admin_local == cand_local:
                    return True
                # Caso especial: admin con @s.whatsapp.net y entrada
                # numérica plana o con prefijo `1`. Permitimos match por
                # dígitos.
                if (
                    admin_suffix_jid == "s.whatsapp.net"
                    and not cand_suffix_jid
                    and admin_short
                    and cand_short
                    and admin_short == cand_short
                ):
                    return True
                # No queremos que un LID matchee a algo que no sea LID.
                continue

            # Admin sin `@`: número E.164. Aceptamos:
            # - input numérico plano con mismo sufijo de 10 dígitos
            # - input JID @s.whatsapp.net con misma "parte numérica"
            if cand_suffix_jid and cand_suffix_jid != "s.whatsapp.net":
                # Ej. cand viene como `X@lid`: NO matchea contra un admin
                # que es número plano.
                continue
            if not cand_digits:
                continue
            if cand_digits == admin_digits:
                return True
            if cand_short and admin_short and cand_short == admin_short:
                return True
        return False

    def reply_target(self, from_id: Optional[str] = None) -> Optional[str]:
        """Identifica el destinatario para responder al admin.

        Evolution no puede enviar mensajes a JIDs ``@lid`` (son
        identificadores anonimizados). Si el remitente vino con
        ``@lid``, devolvemos el primer admin configurado que sea un
        número telefónico real.

        Si ``from_id`` es ya un número o un JID ``@s.whatsapp.net``, lo
        normalizamos a sólo dígitos y lo devolvemos. Si no, fallback al
        primer admin numérico configurado. ``None`` si nada aplica.
        """
        if from_id:
            local, _, suffix = from_id.partition("@")
            digits = "".join(ch for ch in local if ch.isdigit())
            # Solo devolvemos el input si NO es un LID anonimizado.
            if digits and (not suffix or suffix == "s.whatsapp.net"):
                return digits
        # Fallback: primer admin numérico (sin `@lid`).
        for admin in self.admin_numbers:
            admin_local, _, admin_suffix = admin.partition("@")
            if admin_suffix and admin_suffix != "s.whatsapp.net":
                continue
            digits = "".join(ch for ch in admin_local if ch.isdigit())
            if digits:
                return digits
        return None

    @property
    def uses_telegram_alerts(self) -> bool:
        return self.alert_channel.strip().lower() == "telegram"

    @classmethod
    def from_settings(cls, settings: Any) -> "MLRecoveryConfig":
        admins_raw = (getattr(settings, "ml_session_admin_numbers", "") or "").strip()
        admins = tuple(
            n.strip().lstrip("+")
            for n in admins_raw.split(",")
            if n.strip()
        )
        return cls(
            enabled=bool(getattr(settings, "ml_session_alert_enabled", True)),
            alert_channel=str(
                getattr(settings, "ml_session_alert_channel", "whatsapp")
                or "whatsapp"
            ),
            admin_numbers=admins,
            telegram_bot_token=getattr(settings, "ml_session_telegram_bot_token", None),
            telegram_chat_id=getattr(settings, "ml_session_telegram_chat_id", None),
            telegram_timeout_seconds=float(
                getattr(settings, "ml_session_telegram_timeout_seconds", 15.0)
            ),
            alert_cooldown_seconds=int(
                getattr(settings, "ml_session_alert_cooldown_seconds", 1800)
            ),
            inbound_enabled=bool(getattr(settings, "ml_session_inbound_enabled", True)),
            inbound_host=getattr(settings, "ml_session_inbound_host", "127.0.0.1"),
            inbound_port=int(getattr(settings, "ml_session_inbound_port", 9099)),
            inbound_secret=getattr(settings, "ml_session_inbound_secret", None),
            cookie_backup_count=int(
                getattr(settings, "ml_session_cookie_backup_count", 5)
            ),
            cookies_path=getattr(settings, "mercadolibre_cookies_path", "secrets/mercadolibre_cookies.json"),
        )


# ---------------------------------------------------------------------------
# Mensajes
# ---------------------------------------------------------------------------


def build_admin_alert_message(
    *,
    reason: str = "cookie_expiry",
    state: str = "invalid",
    timestamp: str = "",
) -> str:
    """Texto plano para el admin.

    Intencionalmente NO usa el formato canónico de ofertas.
    """
    return (
        "⚠️ Mercado Libre requiere nueva sesión\n\n"
        "Se detectó problema con cookies/sesión de Mercado Libre.\n"
        f"Motivo: {reason}\n"
        f"Estado: {state}\n"
        f"Fecha: {timestamp}\n\n"
        "Acción requerida:\n"
        "Actualiza las cookies de sesión de Mercado Libre para que el bot pueda continuar."
    )


def build_admin_success_message(cookie_count: int) -> str:
    return (
        "Cookies de Mercado Libre aceptadas. "
        "Sesión reactivada sin reiniciar el servicio."
        f"\n\nCookies cargadas: {cookie_count}."
    )


def build_admin_validation_error_message(reason: str) -> str:
    return (
        "Cookies recibidas, pero no son válidas para Mercado Libre.\n\n"
        f"Razón: {reason}\n\n"
        "Intenta exportar de nuevo desde 'Cookie-Editor' (formato JSON) "
        "y envíalas con /cookies_ml."
    )


def build_admin_validation_failed_session_message(reason: str) -> str:
    """Mensaje cuando el formato es válido pero la sesión sigue inválida."""
    return (
        "Cookies recibidas, pero Mercado Libre sigue pidiendo "
        "login/challenge. No reemplacé la sesión activa."
        f"\n\nDetalle: {reason}"
    )


def build_admin_alert_message(
    *,
    reason: str = "cookie_expiry",
    state: str = "invalid",
    timestamp: str = "",
) -> str:
    return (
        "Mercado Libre requiere nueva sesion\n\n"
        "Se detecto problema con cookies/sesion de Mercado Libre.\n"
        f"Motivo: {reason}\n"
        f"Estado: {state}\n"
        f"Fecha: {timestamp}\n\n"
        "Accion requerida:\n"
        "Envia las cookies de sesion de Mercado Libre en el siguiente mensaje, "
        "ya sea pegando el JSON o adjuntando un archivo .json/.txt."
    )


def build_admin_validation_error_message(reason: str) -> str:
    return (
        "Cookies recibidas, pero no son válidas para Mercado Libre.\n\n"
        f"Razon: {reason}\n\n"
        "Intenta exportar de nuevo desde 'Cookie-Editor' (formato JSON) "
        "y envialas otra vez en este chat."
    )


# ---------------------------------------------------------------------------
# Validador del JSON entrante
# ---------------------------------------------------------------------------


@dataclass
class CookieValidationResult:
    ok: bool
    cookies: list[dict] = field(default_factory=list)
    reason: Optional[str] = None
    sanitized_count: int = 0


class MLCookieValidator:
    """Valida estructura mínima del JSON que envía el admin.

    Acepta varios formatos comunes:

    - Lista directa: ``[{name, value, domain, ...}, ...]``
    - Objeto con campo ``cookies``: ``{"cookies": [...]}``
    - Objeto con campo ``data``: ``{"data": [...]}`` (algunos exports)

    Reglas:

    - Cada item debe tener ``name`` (str), ``value`` (str), ``domain`` (str).
    - Al menos UN cookie debe ser de dominio Mercado Libre / MercadoPago.
    - Hasta ``MAX_COOKIES`` (más sería sospechoso).
    - Cookies expiradas (``expires/expirationDate < now``) se descartan; si
      no queda ninguna cookie de ML válida tras filtrar, falla.
    """

    REQUIRED_FIELDS = ("name", "value", "domain")
    MAX_COOKIES = 300
    # Soporta `mercadolibre.com[.mx|.ar|.br|.uy|.cl|.co|.pe]` con o sin punto inicial,
    # y también el dominio de pagos (`mercadopago.com[.mx|...]`).
    ML_DOMAIN_PATTERN = re.compile(
        r"\.?(mercadolibre|mercadopago)\.(com|com\.mx|com\.ar|com\.br|com\.uy|com\.cl|com\.co|com\.pe)\b",
        re.IGNORECASE,
    )

    def validate(self, raw_text: str) -> CookieValidationResult:
        if not raw_text or not raw_text.strip():
            return CookieValidationResult(ok=False, reason="empty_body")

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return CookieValidationResult(
                ok=False, reason=f"invalid_json: {exc.msg} (line {exc.lineno})"
            )

        # Aceptar formatos {"cookies": [...]} o {"data": [...]}.
        if isinstance(data, dict):
            if isinstance(data.get("cookies"), list):
                data = data["cookies"]
            elif isinstance(data.get("data"), list):
                data = data["data"]
            else:
                return CookieValidationResult(
                    ok=False, reason="json_root_must_be_list"
                )

        if not isinstance(data, list):
            return CookieValidationResult(
                ok=False, reason="json_root_must_be_list"
            )

        if len(data) == 0:
            return CookieValidationResult(ok=False, reason="empty_cookies_list")

        if len(data) > self.MAX_COOKIES:
            return CookieValidationResult(
                ok=False,
                reason=f"too_many_cookies (max {self.MAX_COOKIES}, got {len(data)})",
            )

        now_ts = datetime.now(timezone.utc).timestamp()
        sanitized: list[dict] = []
        ml_domain_seen = False
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                return CookieValidationResult(
                    ok=False, reason=f"item_{idx}_not_object"
                )
            for field_name in self.REQUIRED_FIELDS:
                if field_name not in item:
                    return CookieValidationResult(
                        ok=False, reason=f"item_{idx}_missing_{field_name}"
                    )
                if not isinstance(item[field_name], str):
                    return CookieValidationResult(
                        ok=False,
                        reason=f"item_{idx}_{field_name}_not_str",
                    )

            # Filtrar cookies expiradas. Si la cookie es de sesión
            # (`session=True`), no tiene exp y la conservamos.
            exp = item.get("expires")
            if exp is None:
                exp = item.get("expirationDate")
            if exp is None:
                exp = item.get("expiry")
            if (
                not item.get("session")
                and isinstance(exp, (int, float))
                and exp > 0
                and exp < now_ts
            ):
                # Saltar cookie expirada.
                continue

            domain = item["domain"]
            if self.ML_DOMAIN_PATTERN.search(domain):
                ml_domain_seen = True
            sanitized.append(item)

        if not ml_domain_seen:
            return CookieValidationResult(
                ok=False, reason="no_mercadolibre_domain_found"
            )

        if len(sanitized) == 0:
            return CookieValidationResult(
                ok=False, reason="all_cookies_expired"
            )

        return CookieValidationResult(
            ok=True, cookies=sanitized, sanitized_count=len(sanitized)
        )


# ---------------------------------------------------------------------------
# Monitor: detecta cookie_expiry en runtime_events y manda alerta admin
# ---------------------------------------------------------------------------


class MLSessionMonitor:
    """Polling ligero sobre `runtime_events` para detectar `cookie_expiry`.

    Diseño no-bloqueante: cada `tick(now)` revisa si:

    - Hubo un nuevo `cookie_expiry` después del último alertado.
    - El cooldown desde el último `ml_session_admin_alerted` ya se
      cumplió.

    Si ambas condiciones, envía la alerta al admin via `evolution_send`
    (callback async para no acoplarse al EvolutionClient real). Si falla
    el envío, NO marca como alertado (próximo tick reintenta).

    El monitor NO ejecuta el envío directamente; recibe un callable
    `evolution_send(number: str, text: str) -> Awaitable[bool]` que el
    caller inyecta. Esto permite tests con stubs.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        config: MLRecoveryConfig,
        evolution_send: Callable[[str, str], Awaitable[bool]],
    ) -> None:
        self.db = db
        self.config = config
        self._send = evolution_send

    async def tick(self) -> Optional[str]:
        """Devuelve `kind` del evento emitido (`ALERT_KIND` /
        `ALERT_SKIPPED_KIND`) o `None` si no hizo nada.
        """
        if not self.config.enabled:
            return None
        if not self.config.uses_telegram_alerts and not self.config.admin_numbers:
            return None

        # Usamos `id` autoincremental para comparar orden, no `created_at`
        # (resolución milisegundo no es suficiente para ráfagas).
        last_expiry_id = self._last_event_id("cookie_expiry")
        if last_expiry_id is None:
            return None
        last_expiry = self._event_by_id(last_expiry_id)
        reason = "cookie_expiry"
        state = "invalid"
        event_ts = _iso(_now_utc())
        if last_expiry is not None:
            payload = last_expiry.get("payload") or {}
            reason = str(payload.get("reason") or payload.get("final_url") or "cookie_expiry")
            state = str(payload.get("manager_state") or payload.get("state") or "invalid")
            if last_expiry.get("created_at"):
                event_ts = str(last_expiry["created_at"])

        last_alerted_id = self._last_event_id(ALERT_KIND)

        # Si la alerta más reciente vino DESPUÉS del último expiry,
        # ya estamos al día.
        if last_alerted_id is not None and last_alerted_id > last_expiry_id:
            return None

        # Cooldown: si alertamos hace <cooldown segundos, no spamear,
        # aunque haya un expiry nuevo.
        if last_alerted_id is not None:
            last_alerted_time = self._last_event_time(ALERT_KIND)
            if last_alerted_time is not None:
                elapsed = (_now_utc() - last_alerted_time).total_seconds()
                if elapsed < self.config.alert_cooldown_seconds:
                    self._emit(
                        ALERT_SKIPPED_KIND,
                        "info",
                        {
                            "reason": "cooldown_active",
                            "cooldown_seconds": self.config.alert_cooldown_seconds,
                            "elapsed_seconds": int(elapsed),
                        },
                    )
                    return ALERT_SKIPPED_KIND

        # Mandamos a todos los admins en orden. Si AL MENOS UNO recibe,
        # marcamos como alertado.
        text = build_admin_alert_message(reason=reason, state=state, timestamp=event_ts)
        sent_ok = False
        delivery: list[dict] = []
        recipients: tuple[str, ...]
        if self.config.uses_telegram_alerts:
            recipients = (self.config.telegram_chat_id or "",)
        else:
            recipients = self.config.admin_numbers
        for number in recipients:
            try:
                ok = await self._send(number, text)
            except Exception as exc:
                logger.exception("MLSessionMonitor: send failed for %s", number)
                delivery.append({"number": number, "ok": False, "error": str(exc)})
                continue
            delivery.append({"number": number, "ok": bool(ok)})
            if ok:
                sent_ok = True

        if not sent_ok:
            # No emitimos `alerted` para que el siguiente tick reintente.
            return None

        self._emit(
            ALERT_KIND,
            "info",
            {
                "delivery": delivery,
                "alerted_at": _iso(_now_utc()),
            },
        )
        return ALERT_KIND

    # ------------------------------------------------------------------

    def reset_cooldown(self) -> None:
        """Marca un evento `ml_cookies_reloaded` como ancla nueva: el
        próximo `cookie_expiry` que llegue después se considerará
        un nuevo incidente y se alertará sin cooldown.
        """
        # No-op a propósito: el cooldown se basa en el timestamp de
        # `ALERT_KIND`. Para un nuevo expiry posterior, el monitor
        # naturalmente alertará si supera el cooldown. Mantenemos esta
        # función para integración explícita y como punto de extensión.
        return

    # ------------------------------------------------------------------

    def _last_event_id(self, kind: str) -> Optional[int]:
        """Devuelve el `id` (rowid autoincremental) del último evento del
        tipo dado. Usamos id en vez de created_at porque sub-segundo es
        más fiable y SQLite garantiza id estrictamente creciente.
        """
        row = self.db.execute(
            "SELECT id FROM runtime_events WHERE kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row[0] if not hasattr(row, "keys") else row["id"])
        except Exception:
            return None

    def _last_event_time(self, kind: str) -> Optional[datetime]:
        row = self.db.execute(
            "SELECT created_at FROM runtime_events WHERE kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if row is None:
            return None
        try:
            ts = row[0] if not hasattr(row, "keys") else row["created_at"]
        except Exception:
            ts = row[0]
        if ts is None:
            return None
        try:
            # Soportar formatos con/ sin sufijo Z
            ts_clean = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(ts_clean)
        except ValueError:
            return None

    def _event_by_id(self, event_id: int) -> Optional[dict[str, Any]]:
        row = self.db.execute(
            "SELECT id, payload_json, created_at FROM runtime_events WHERE id = ? LIMIT 1",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload_raw = row[1] if not hasattr(row, "keys") else row["payload_json"]
            created_at = row[2] if not hasattr(row, "keys") else row["created_at"]
            payload = json.loads(payload_raw or "{}")
            if not isinstance(payload, dict):
                payload = {}
            return {"payload": payload, "created_at": created_at}
        except Exception:
            return None

    def _emit(self, kind: str, severity: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, severity, json.dumps(payload, ensure_ascii=False), _iso(_now_utc())),
        )
        self.db.commit()


# ---------------------------------------------------------------------------
# Reloader: rota archivo + escribe nuevo + llama a ctx.reload_ml_cookies
# ---------------------------------------------------------------------------


class MLCookieReloader:
    """Persiste el JSON nuevo, rota backups y dispara hot-reload en el bot.

    El "hot-reload" en sí depende del runtime: necesita un `ServerContext`
    con un método `reload_ml_cookies(cookies)`. Para tests usamos un stub.

    Si se inyecta `session_manager` (instancia de
    ``MercadoLibreSessionManager``), `apply()` delega el flujo seguro
    completo: staging → validate (Chromium temporal) → promote → rotate.
    El callback `reload_callback` se ignora cuando hay manager (la
    rotación la hace el manager via su propio rotation_callback).
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        config: MLRecoveryConfig,
        reload_callback: Optional[Callable[[list[dict]], Awaitable[bool]]] = None,
        *,
        session_manager: Optional[Any] = None,
    ) -> None:
        self.db = db
        self.config = config
        self._reload = reload_callback
        self._manager = session_manager

    async def apply(self, cookies: list[dict]) -> bool:
        """Rota archivo + escribe + dispara reload.

        Si hay `session_manager`, usa el pipeline seguro
        (stage → validate → promote → rotate) y respeta el resultado de
        validación. Si no, mantiene el comportamiento histórico de
        backup + write + callback.
        """
        if self._manager is not None:
            ok, outcome = await self._manager.reload_from_cookies(cookies)
            self._emit(
                RELOAD_OK_KIND if ok else RELOAD_ERROR_KIND,
                "info" if ok else "warning",
                {
                    "cookies_count": len(cookies),
                    "managed": True,
                    "validation_ok": getattr(outcome, "ok", None),
                    "validation_reason": getattr(outcome, "reason", None),
                },
            )
            return ok

        # ── Camino legacy (sin manager) ───────────────────────────────
        cookies_path = Path(self.config.cookies_path)
        cookies_path.parent.mkdir(parents=True, exist_ok=True)

        # 1) Backup del actual (si existe).
        backup_path: Optional[Path] = None
        if cookies_path.exists():
            backup_dir = cookies_path.parent / "cookies_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = _now_utc().strftime("%Y%m%dT%H%M%S_%fZ")
            backup_name = f"{cookies_path.stem}_{ts}.json"
            backup_path = backup_dir / backup_name
            try:
                backup_path.write_text(
                    cookies_path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning(
                    "MLCookieReloader: backup falló (continuamos): %s", exc
                )
                backup_path = None
            self._prune_backups(backup_dir, prefix=cookies_path.stem)

        # 2) Escribir nuevo JSON.
        try:
            cookies_path.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.exception("MLCookieReloader: write falló")
            self._emit(
                RELOAD_ERROR_KIND,
                "error",
                {"step": "write_json", "error": str(exc)},
            )
            return False

        # 3) Reload en runtime (si hay callback).
        reload_ok = True
        reload_error: Optional[str] = None
        if self._reload is not None:
            try:
                reload_ok = bool(await self._reload(cookies))
            except Exception as exc:
                logger.exception("MLCookieReloader: reload_callback raised")
                reload_ok = False
                reload_error = str(exc)

        kind = RELOAD_OK_KIND if reload_ok else RELOAD_ERROR_KIND
        severity = "info" if reload_ok else "error"
        self._emit(
            kind,
            severity,
            {
                "cookies_count": len(cookies),
                "backup_path": str(backup_path) if backup_path else None,
                "cookies_path": str(cookies_path),
                "reload_callback_used": self._reload is not None,
                "reload_error": reload_error,
            },
        )
        return reload_ok

    # ------------------------------------------------------------------

    def _prune_backups(self, backup_dir: Path, *, prefix: str) -> None:
        """Conserva sólo los últimos `cookie_backup_count` backups."""
        keep = max(1, int(self.config.cookie_backup_count))
        try:
            files = sorted(
                (p for p in backup_dir.iterdir() if p.name.startswith(prefix + "_")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except FileNotFoundError:
            return
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    def _emit(self, kind: str, severity: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, severity, json.dumps(payload, ensure_ascii=False), _iso(_now_utc())),
        )
        self.db.commit()
