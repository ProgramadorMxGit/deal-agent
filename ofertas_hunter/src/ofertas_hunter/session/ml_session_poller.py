"""Poller de Evolution API para detectar `/cookies_ml` del admin.

Diseñado para entornos donde Evolution corre en un host remoto y no puede
hacer POST al webhook local del bot. Hace pull con intervalos cortos
(default 15s) y procesa cualquier mensaje del admin que contenga el
comando ``/cookies_ml``.

Diseño:

- Lee `chat/findMessages` con `where.key.fromMe=false` (mensajes
  entrantes), filtra por número admin y por aparición de
  ``/cookies_ml`` (case-insensitive).
- Mantiene cursor en memoria (``last_seen_message_id``) +
  persistente (`runtime_meta.ml_poller_cursor`) para no reprocesar
  mensajes entre reinicios.
- Soporta tanto texto inline como ``documentMessage`` con base64
  (mismo path que el webhook).
- Reusa ``MLCookieValidator``, ``MLCookieReloader`` y
  ``MercadoLibreSessionManager`` — la lógica de validación/promote/rotate
  es única.
- Es resiliente: si Evolution está caído, no crashea el bot, sólo emite
  ``ml_poller_error`` y reintenta en el siguiente tick.

NO usa frameworks pesados — sólo httpx async (que ya está en el bot).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from .ml_session_inbound import (
    COMMAND_TOKEN,
    extract_attachment_json,
    looks_like_cookies_json,
    parse_command_and_payload,
)
from .ml_session_recovery import (
    MLCookieValidator,
    MLRecoveryConfig,
    RECEIVED_KIND,
    VALIDATED_KIND,
    build_admin_success_message,
    build_admin_validation_error_message,
    build_admin_validation_failed_session_message,
)


logger = logging.getLogger(__name__)


POLLER_KIND_TICK = "ml_poller_tick"
POLLER_KIND_ERROR = "ml_poller_error"
POLLER_KIND_PROCESSED = "ml_poller_processed_command"


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _digits_only(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


@dataclass
class MLPollerConfig:
    enabled: bool = True
    base_url: str = ""
    api_key: str = ""
    api_key_header: str = "apikey"
    instance: str = ""
    poll_interval_seconds: int = 15
    page_size: int = 20
    """Número de mensajes recientes a pedir por tick."""

    @classmethod
    def from_settings(cls, settings: Any) -> "MLPollerConfig":
        return cls(
            enabled=bool(getattr(settings, "ml_session_poller_enabled", True)),
            base_url=str(getattr(settings, "evolution_base_url", "") or ""),
            api_key=str(getattr(settings, "evolution_api_key", "") or ""),
            api_key_header=str(
                getattr(settings, "evolution_api_key_header", "apikey") or "apikey"
            ),
            instance=str(getattr(settings, "evolution_instance", "") or ""),
            poll_interval_seconds=int(
                getattr(settings, "ml_session_poller_interval_seconds", 15)
            ),
            page_size=int(getattr(settings, "ml_session_poller_page_size", 20)),
        )


class MLEvolutionPoller:
    """Poller HTTP que consulta Evolution API por mensajes nuevos.

    Para cada mensaje que coincide con ``/cookies_ml`` del admin
    autorizado, llama a ``on_cookies(cookies)`` (mismo callback que el
    webhook) y manda confirmación.

    El poller es seguro de arrancar incluso si Evolution está caído:
    captura excepciones por tick.
    """

    def __init__(
        self,
        *,
        recovery_config: MLRecoveryConfig,
        poller_config: MLPollerConfig,
        validator: MLCookieValidator,
        on_cookies: Callable[[list[dict]], Awaitable[bool]],
        evolution_send: Callable[[str, str], Awaitable[bool]],
        db: Any,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.recovery_config = recovery_config
        self.config = poller_config
        self.validator = validator
        self._on_cookies = on_cookies
        self._evolution_send = evolution_send
        self.db = db
        self._client = http_client
        self._owns_client = http_client is None
        self._cursor_message_id: Optional[str] = None
        self._cursor_timestamp: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Idempotencia: ids ya procesados para evitar doble-trigger si la
        # API duplica al borde de la página.
        self._processed_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self.config.enabled:
            logger.info("ml_session_poller: deshabilitado por config")
            return
        if not (self.config.base_url and self.config.api_key and self.config.instance):
            logger.warning(
                "ml_session_poller: config incompleta (base_url=%s, instance=%s); "
                "no arranco",
                bool(self.config.base_url),
                bool(self.config.instance),
            )
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=8.0),
                headers={self.config.api_key_header: self.config.api_key},
            )
            self._owns_client = True

        # Cargar cursor previo
        self._load_cursor()

        # Arranque en hot: marcar cualquier mensaje existente como ya
        # visto para no reprocesar el historial al primer tick.
        if self._cursor_timestamp is None:
            await self._initialize_cursor()

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(), name="ml-session-poller"
        )
        logger.info(
            "ml_session_poller: corriendo cada %ds contra %s/%s",
            self.config.poll_interval_seconds,
            self.config.base_url,
            self.config.instance,
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("ml_session_poller: tick raised")
                self._emit(POLLER_KIND_ERROR, "warning", {"error": str(exc)})
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _initialize_cursor(self) -> None:
        """En el primer arranque sin cursor previo, procesa cualquier
        ``/cookies_ml`` reciente del admin (últimas 6 horas) y luego
        cursa al timestamp más alto visto.

        Esto evita perder mensajes que el admin envió antes de que el
        bot estuviera escuchando, sin reprocesar todo el histórico.
        """
        try:
            messages = await self._fetch_recent(limit=self.config.page_size)
        except Exception as exc:
            logger.warning("ml_session_poller: init cursor falló: %s", exc)
            return

        # Ventana razonable: 6 horas hacia atrás. Cookies más viejas que
        # eso casi seguro ya están obsoletas.
        from time import time as _time
        cutoff_ts = int(_time()) - 6 * 3600

        max_ts = 0
        max_id: Optional[str] = None
        # Procesar en orden cronológico ascendente para mantener
        # idempotencia.
        recent = sorted(messages, key=lambda m: int(m.get("messageTimestamp") or 0))
        for m in recent:
            ts = int(m.get("messageTimestamp") or 0)
            mid = m.get("key", {}).get("id") or m.get("id")
            if ts > max_ts:
                max_ts = ts
                max_id = mid
            # Sólo intentar procesar si está dentro de la ventana y
            # parece relevante (admin + texto/document).
            if ts >= cutoff_ts:
                try:
                    await self._process_message(m)
                except Exception:
                    logger.exception(
                        "ml_session_poller: error procesando msg en init"
                    )
                if mid:
                    self._processed_ids.add(mid)

        if max_ts:
            self._cursor_timestamp = max_ts
            self._cursor_message_id = max_id
            self._save_cursor()

    async def _tick_once(self) -> None:
        messages = await self._fetch_recent(limit=self.config.page_size)
        # Filtrar mensajes nuevos (timestamp > cursor)
        new_msgs: list[dict] = []
        for m in messages:
            ts = int(m.get("messageTimestamp") or 0)
            mid = m.get("key", {}).get("id") or m.get("id") or ""
            if mid and mid in self._processed_ids:
                continue
            if self._cursor_timestamp is not None and ts <= self._cursor_timestamp:
                continue
            new_msgs.append(m)

        # Procesar en orden cronológico ascendente
        new_msgs.sort(key=lambda m: int(m.get("messageTimestamp") or 0))

        max_ts_seen = self._cursor_timestamp or 0
        max_id_seen = self._cursor_message_id

        for msg in new_msgs:
            ts = int(msg.get("messageTimestamp") or 0)
            mid = msg.get("key", {}).get("id") or msg.get("id") or ""
            try:
                await self._process_message(msg)
            except Exception:
                logger.exception("ml_session_poller: error procesando msg %s", mid)
            if mid:
                self._processed_ids.add(mid)
            if ts > max_ts_seen:
                max_ts_seen = ts
                max_id_seen = mid

        if max_ts_seen != (self._cursor_timestamp or 0):
            self._cursor_timestamp = max_ts_seen
            self._cursor_message_id = max_id_seen
            self._save_cursor()

        # Emit tick periódico (info) sólo si hubo trabajo, para no
        # contaminar runtime_events.
        if new_msgs:
            self._emit(
                POLLER_KIND_TICK,
                "info",
                {
                    "checked": len(messages),
                    "new": len(new_msgs),
                    "cursor_ts": self._cursor_timestamp,
                },
            )

    # ------------------------------------------------------------------
    # Evolution API
    # ------------------------------------------------------------------

    async def _fetch_recent(self, *, limit: int) -> list[dict]:
        """Llama POST /chat/findMessages/<instance> con filtro fromMe=false.

        Devuelve la lista `records` ordenada como vino. Maneja la forma
        Evolution v2 (`{"messages": {"records": [...]}}`).
        """
        assert self._client is not None
        url = f"{self.config.base_url.rstrip('/')}/chat/findMessages/{self.config.instance}"
        body = {
            "where": {"key": {"fromMe": False}},
            "limit": limit,
            "order": "desc",
        }
        try:
            resp = await self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"http_error: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"non_200: {resp.status_code} body={resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError:
            return []
        # Form A: {"messages": {"records": [...]}}
        if isinstance(data, dict) and isinstance(data.get("messages"), dict):
            records = data["messages"].get("records") or []
        # Form B: lista directa
        elif isinstance(data, list):
            records = data
        else:
            records = data.get("records", []) if isinstance(data, dict) else []
        return [r for r in records if isinstance(r, dict)]

    # ------------------------------------------------------------------
    # Procesamiento de un mensaje
    # ------------------------------------------------------------------

    async def _process_message(self, msg: dict) -> None:
        """Procesa un mensaje individual del polling.

        Replica la lógica del webhook: filtra admin, parsea
        ``/cookies_ml``, valida, llama on_cookies, manda confirmación.
        """
        key = msg.get("key") or {}
        remote_jid = key.get("remoteJid") or ""
        # Pasamos el JID completo al filtro admin (acepta @lid, números E164,
        # o números con prefijo `1` de WhatsApp).
        if not self.recovery_config.is_admin(remote_jid):
            return

        # Destino para responder. Si el remitente es un `@lid`, no se le
        # puede mandar mensaje; usamos el primer admin numérico
        # configurado.
        reply_to = self.recovery_config.reply_target(remote_jid) or _digits_only(
            remote_jid.split("@", 1)[0]
        )
        # Para logs / runtime_events seguimos usando una representación
        # razonable (no vacía).
        from_num = reply_to or remote_jid

        # Extraer texto del mensaje (similar a extract_admin_payload pero
        # sobre el formato directo de findMessages).
        msg_obj = msg.get("message") or {}
        text: Optional[str] = None
        if isinstance(msg_obj, dict):
            text = (
                msg_obj.get("conversation")
                or msg_obj.get("extendedTextMessage", {}).get("text")
                or msg_obj.get("imageMessage", {}).get("caption")
                or msg_obj.get("documentMessage", {}).get("caption")
            )

        # Buscar attachment .json con base64.
        attachment_json = extract_attachment_json({"data": msg})
        # Algunas variantes meten el documentMessage directamente.
        if not attachment_json:
            attachment_json = extract_attachment_json(msg)

        if not text and not attachment_json:
            return

        command, remaining_text = parse_command_and_payload(text)
        text_looks_like_cookies = looks_like_cookies_json(text)
        if command != COMMAND_TOKEN and not attachment_json and not text_looks_like_cookies:
            return

        # Determinar fuente del JSON
        cookie_text: Optional[str] = None
        source = "unknown"
        if attachment_json:
            cookie_text = attachment_json.strip()
            source = "poll_attachment"
        elif command == COMMAND_TOKEN and remaining_text and remaining_text.strip():
            cookie_text = remaining_text.strip()
            source = "poll_inline"
        elif text_looks_like_cookies and text:
            cookie_text = text.strip()
            source = "poll_auto_detected"

        if not cookie_text:
            await self._safe_send(reply_to,
                "Recibí /cookies_ml pero sin cookies. "
                "Pega el JSON debajo del comando o envía un archivo .json.",
            )
            return

        if not (cookie_text.startswith("[") or cookie_text.startswith("{")):
            await self._safe_send(reply_to,
                build_admin_validation_error_message("el cuerpo no parece JSON"),
            )
            return

        self._emit(RECEIVED_KIND, "info", {"from": from_num, "source": source})

        result = self.validator.validate(cookie_text)
        self._emit(
            VALIDATED_KIND,
            "info" if result.ok else "warning",
            {
                "from": from_num,
                "ok": result.ok,
                "reason": result.reason,
                "count": result.sanitized_count,
                "source": source,
            },
        )

        if not result.ok:
            await self._safe_send(reply_to,
                build_admin_validation_error_message(result.reason or "?"),
            )
            return

        # Reload (stage → validate activa → promote → rotate)
        try:
            reload_ok = bool(await self._on_cookies(result.cookies))
        except Exception as exc:
            logger.exception("ml_session_poller: on_cookies raised")
            self._emit(
                "ml_poller_reload_error",
                "error",
                {"error": str(exc)},
            )
            await self._safe_send(reply_to,
                build_admin_validation_failed_session_message(
                    "error interno al aplicar cookies"
                ),
            )
            return

        if reload_ok:
            await self._safe_send(reply_to, build_admin_success_message(len(result.cookies))
            )
        else:
            await self._safe_send(reply_to,
                build_admin_validation_failed_session_message(
                    "validación activa rechazó la sesión"
                ),
            )

        self._emit(
            POLLER_KIND_PROCESSED,
            "info" if reload_ok else "warning",
            {
                "from": from_num,
                "source": source,
                "reload_ok": reload_ok,
                "count": result.sanitized_count,
            },
        )

    async def _safe_send(self, number: str, text: str) -> None:
        try:
            await self._evolution_send(number, text)
        except Exception:
            logger.exception("ml_session_poller: send falló para %s", number)

    # ------------------------------------------------------------------
    # Cursor persistente (runtime_events kind=ml_poller_cursor)
    # ------------------------------------------------------------------

    def _load_cursor(self) -> None:
        if self.db is None:
            return
        try:
            row = self.db.execute(
                "SELECT payload_json FROM runtime_events "
                "WHERE kind = 'ml_poller_cursor' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except Exception:
            return
        if not row:
            return
        try:
            raw = row[0] if not hasattr(row, "keys") else row["payload_json"]
            data = json.loads(raw)
            self._cursor_timestamp = data.get("ts")
            self._cursor_message_id = data.get("mid")
        except Exception:
            pass

    def _save_cursor(self) -> None:
        if self.db is None:
            return
        try:
            payload = json.dumps(
                {"ts": self._cursor_timestamp, "mid": self._cursor_message_id},
                ensure_ascii=False,
            )
            # Borramos cursor anterior para no acumular filas; mantenemos
            # el último.
            self.db.execute(
                "DELETE FROM runtime_events WHERE kind = 'ml_poller_cursor'"
            )
            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("ml_poller_cursor", "debug", payload, _now_iso()),
            )
            self.db.commit()
        except Exception:
            logger.exception("ml_session_poller: save_cursor falló")

    def _emit(self, kind: str, severity: str, payload: dict) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (kind, severity, json.dumps(payload, ensure_ascii=False), _now_iso()),
            )
            self.db.commit()
        except Exception:
            logger.exception("ml_session_poller: emit falló")
