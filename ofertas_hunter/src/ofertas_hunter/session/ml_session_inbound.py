"""Servidor HTTP minimal para recibir cookies ML del admin via Evolution API.

Diseño:

- Stdlib `http.server.ThreadingHTTPServer` corriendo en un thread separado.
- Endpoint único `POST /wa/inbound`.
- Auth: header `X-Webhook-Secret` debe coincidir con el configurado.
- Filtro: el `from` (número remitente) debe estar en `admin_numbers`.
- **Comando `/cookies_ml`**: el bot solo intenta validar/aplicar cookies si
  el mensaje contiene la marca `/cookies_ml`. Si llega cualquier otro texto
  (saludo, etc.), se ignora silenciosamente sin desperdiciar recursos.
- **Attachments**: si el webhook trae un `documentMessage` con base64 de un
  archivo `.json`, se decodifica y se trata como cookies.
- Si el body trae cookies válidas, llama a `MLCookieReloader.apply(...)`
  via `asyncio.run_coroutine_threadsafe(...)` contra el loop principal.
- Manda WhatsApp de confirmación / error usando el mismo `evolution_send`.

NO usa frameworks pesados (aiohttp, fastapi). Sólo stdlib.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Awaitable, Callable, Optional

from .ml_session_recovery import (
    MLCookieReloader,
    MLCookieValidator,
    MLRecoveryConfig,
    RECEIVED_KIND,
    VALIDATED_KIND,
    build_admin_success_message,
    build_admin_validation_error_message,
    build_admin_validation_failed_session_message,
)


logger = logging.getLogger(__name__)


# Comando explícito que el admin debe usar para enviar cookies.
COMMAND_TOKEN = "/cookies_ml"


# ---------------------------------------------------------------------------
# Extracción de mensaje + número remitente del payload Evolution
# ---------------------------------------------------------------------------


def _digits_only(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


def extract_admin_payload(body: dict) -> tuple[Optional[str], Optional[str]]:
    """Saca `(from_identifier, text)` del payload heterogéneo de Evolution.

    El primer elemento puede ser un número E.164, un JID
    (``5218338498692@s.whatsapp.net``) o un LID
    (``25877295435783@lid``). Lo dejamos tal cual viene; la decisión de
    si es admin la hace ``MLRecoveryConfig.is_admin``.
    """
    # Desempaquetar `data` si viene envuelto
    if isinstance(body, dict) and "data" in body and isinstance(body["data"], dict):
        candidate_inner = body["data"]
    else:
        candidate_inner = body

    # 1) Plain
    if isinstance(candidate_inner.get("from"), str) and (
        isinstance(candidate_inner.get("text"), str)
        or isinstance(candidate_inner.get("message"), str)
    ):
        from_id = candidate_inner.get("from", "").strip().lstrip("+")
        text = candidate_inner.get("text") or candidate_inner.get("message") or ""
        return (from_id or None, text)

    # 2) Evolution v1/v2: key.remoteJid + message.conversation
    key = candidate_inner.get("key") or {}
    remote_jid = key.get("remoteJid") or candidate_inner.get("remoteJid", "")
    # Conservamos la cadena entera (con `@lid` o `@s.whatsapp.net`) para
    # que `is_admin` pueda matchear identificadores anonimizados.
    from_id = (remote_jid or "").strip()

    msg_obj = candidate_inner.get("message") or {}
    text: Optional[str] = None
    if isinstance(msg_obj, dict):
        # rutas comunes
        text = (
            msg_obj.get("conversation")
            or msg_obj.get("extendedTextMessage", {}).get("text")
            or msg_obj.get("imageMessage", {}).get("caption")
            or msg_obj.get("documentMessage", {}).get("caption")
        )
    elif isinstance(msg_obj, str):
        text = msg_obj

    # Algunos webhooks ponen el texto bajo "messageContent" o "body"
    if not text:
        text = candidate_inner.get("messageContent") or candidate_inner.get("body")

    if from_id or text:
        return (from_id or None, text)
    return (None, None)


def extract_attachment_json(body: dict) -> Optional[str]:
    """Extrae el contenido textual de un attachment ``.json``.

    Soporta:

    - ``{"data": {"message": {"documentMessage": {"mimetype": "application/json",
        "fileName": "...", "data": "<base64>"}}}}``
    - ``{"attachment": {"contentType": "application/json", "data": "<base64>"}}``
    - ``{"document": {"data": "<base64>", "mimetype": "..."}}``

    Returns: string con el JSON crudo, o ``None`` si no hay attachment.
    """
    if not isinstance(body, dict):
        return None

    # Desempaquetar
    inner = body.get("data") if isinstance(body.get("data"), dict) else body

    # Ruta 1: documentMessage con base64
    msg = inner.get("message") if isinstance(inner.get("message"), dict) else None
    if msg:
        doc = msg.get("documentMessage") if isinstance(msg.get("documentMessage"), dict) else None
        if doc:
            mime = (doc.get("mimetype") or "").lower()
            filename = (doc.get("fileName") or doc.get("filename") or "").lower()
            if "json" in mime or filename.endswith(".json"):
                # Evolution puede entregar la data en `data`, `body`, o `base64`.
                b64 = doc.get("data") or doc.get("body") or doc.get("base64")
                decoded = _try_decode_base64(b64)
                if decoded:
                    return decoded

    # Ruta 2: attachment plano
    for key in ("attachment", "document", "file"):
        att = inner.get(key)
        if isinstance(att, dict):
            mime = (att.get("contentType") or att.get("mimetype") or "").lower()
            name = (att.get("name") or att.get("fileName") or "").lower()
            if "json" in mime or name.endswith(".json"):
                b64 = att.get("data") or att.get("body") or att.get("base64")
                decoded = _try_decode_base64(b64)
                if decoded:
                    return decoded
                # A veces ya viene como texto plano
                content = att.get("content") or att.get("text")
                if isinstance(content, str) and content.strip():
                    return content

    return None


def _try_decode_base64(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Quitar prefijo data:application/json;base64,...
        if value.startswith("data:"):
            _, _, rest = value.partition(",")
            value = rest
        decoded = base64.b64decode(value, validate=False).decode("utf-8", errors="replace")
        return decoded
    except Exception:
        return None


def parse_command_and_payload(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Extrae ``(command, remaining_text)`` cuando hay un slash command.

    Reconoce ``/cookies_ml`` como primer token (ignorando espacios). El
    resto del mensaje se devuelve tal cual (puede ser JSON, vacío o
    cualquier nota del admin).

    Si no hay comando reconocido, devuelve ``(None, text)``.
    """
    if not text:
        return (None, text)
    stripped = text.strip()
    if not stripped:
        return (None, stripped)
    # Match contra `/cookies_ml` al inicio (case-insensitive).
    m = re.match(r"^(/cookies_ml)\b\s*", stripped, flags=re.IGNORECASE)
    if not m:
        return (None, stripped)
    remaining = stripped[m.end():]
    return (COMMAND_TOKEN, remaining)


# Heurística para detectar un JSON de cookies sin necesitar el slash
# command. Sólo se usa cuando el mensaje viene de un admin autorizado.
_COOKIES_JSON_HINT = re.compile(
    r'"(domain|name)"\s*:\s*"[^"]*(mercadolibre|mercadopago)',
    re.IGNORECASE,
)


def looks_like_cookies_json(text: Optional[str]) -> bool:
    """Detecta si el texto se ve como un JSON de cookies ML.

    Es una heurística barata para auto-aceptar respuestas del admin que
    pegan el JSON directo (sin escribir ``/cookies_ml``). Reglas:

    - Empieza con ``[`` o ``{`` (después de strip).
    - Contiene una key ``domain`` o ``name`` cuyo valor menciona
      ``mercadolibre`` o ``mercadopago``.

    El validador real (``MLCookieValidator``) es quien rechaza si el
    contenido no está bien formado.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0] not in "[{":
        return False
    return bool(_COOKIES_JSON_HINT.search(stripped))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@dataclass
class _Deps:
    """Dependencias inyectadas al handler."""

    config: MLRecoveryConfig
    validator: MLCookieValidator
    on_cookies: Callable[[list[dict]], Awaitable[bool]]
    """Callback async: recibe cookies sanitizadas, retorna True si reload OK."""

    evolution_send: Callable[[str, str], Awaitable[bool]]
    """Callback async para enviar WhatsApp al admin (confirmación / error)."""

    asyncio_loop: asyncio.AbstractEventLoop
    """Loop main donde se ejecutan las callbacks via run_coroutine_threadsafe."""

    db: Any
    """Conexión SQLite para emitir runtime_events sincronos desde el thread."""


def _emit_event_threadsafe(deps: _Deps, kind: str, severity: str, payload: dict) -> None:
    """Inserta un runtime_event de forma síncrona desde el thread del HTTP
    server. SQLite acepta multi-thread si abrimos con `check_same_thread=False`,
    pero por seguridad serializamos vía el lock interno del cursor.
    """
    try:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        deps.db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, severity, json.dumps(payload, ensure_ascii=False), now),
        )
        deps.db.commit()
    except Exception:
        logger.exception("ml_session_inbound: emit_event_threadsafe falló")


def make_handler(deps: _Deps):
    """Construye el handler con cierre sobre `deps`."""

    class WAInboundHandler(BaseHTTPRequestHandler):
        # Silenciar el logging por defecto del handler
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("ml_session_inbound: " + format, *args)

        def _respond(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            # Healthcheck
            if self.path == "/wa/health":
                return self._respond(200, {"status": "ok"})
            return self._respond(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/wa/inbound":
                return self._respond(404, {"error": "not_found"})

            # Validación de secret
            secret_required = deps.config.inbound_secret
            secret_received = self.headers.get("X-Webhook-Secret", "")
            if secret_required and secret_received != secret_required:
                _emit_event_threadsafe(
                    deps,
                    "ml_session_inbound_unauthorized",
                    "warning",
                    {
                        "remote": self.client_address[0],
                        "path": self.path,
                        "header_present": bool(secret_received),
                    },
                )
                return self._respond(401, {"error": "unauthorized"})

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 5 * 1024 * 1024:  # cap 5MB
                return self._respond(413, {"error": "payload_too_large_or_empty"})

            try:
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
            except Exception as exc:
                logger.exception("ml_session_inbound: read body falló")
                return self._respond(400, {"error": f"body_read: {exc}"})

            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                return self._respond(400, {"error": f"invalid_envelope_json: {exc.msg}"})

            from_num, text = extract_admin_payload(body)
            attachment_json = extract_attachment_json(body)
            if not from_num:
                return self._respond(400, {"error": "missing_from"})
            if not text and not attachment_json:
                return self._respond(400, {"error": "missing_text"})

            # Filtro admin numbers
            if not deps.config.is_admin(from_num):
                _emit_event_threadsafe(
                    deps,
                    "ml_session_inbound_non_admin_ignored",
                    "info",
                    {"from": from_num},
                )
                return self._respond(403, {"error": "not_admin"})

            # Parsear comando del texto. El admin puede:
            # 1) Empezar con /cookies_ml (preferido).
            # 2) O simplemente pegar el JSON de cookies como respuesta al
            #    mensaje del bot (auto-detectamos por dominio ML/MP).
            command, remaining_text = parse_command_and_payload(text)

            text_looks_like_cookies = looks_like_cookies_json(text)

            # Destino para responderle al admin. Si el remitente vino
            # como `@lid` (id anonimizado), Evolution no puede enviarle;
            # usamos el primer admin numérico configurado.
            reply_to = deps.config.reply_target(from_num) or from_num

            if command != COMMAND_TOKEN and not attachment_json and not text_looks_like_cookies:
                _emit_event_threadsafe(
                    deps,
                    "ml_session_inbound_no_command_ignored",
                    "info",
                    {
                        "from": from_num,
                        "preview": (text or "")[:80],
                        "had_attachment": False,
                    },
                )
                return self._respond(200, {"status": "ignored_no_command"})

            # Determinar fuente del JSON: attachment > comando+inline > auto-detect.
            cookie_text: Optional[str] = None
            source = "unknown"
            if attachment_json:
                cookie_text = attachment_json.strip()
                source = "attachment"
            elif command == COMMAND_TOKEN and remaining_text and remaining_text.strip():
                cookie_text = remaining_text.strip()
                source = "inline"
            elif text_looks_like_cookies and text:
                cookie_text = text.strip()
                source = "auto_detected"

            if not cookie_text:
                # Comando recibido pero sin cuerpo. Avisar al admin que
                # debe pegar el JSON o adjuntar el .json.
                _emit_event_threadsafe(
                    deps,
                    "ml_session_inbound_command_without_body",
                    "info",
                    {"from": from_num},
                )
                fut = asyncio.run_coroutine_threadsafe(
                    deps.evolution_send(reply_to,
                        "Recibí /cookies_ml pero sin cookies. "
                        "Pega el JSON debajo del comando o envía un "
                        "archivo .json adjunto.",
                    ),
                    deps.asyncio_loop,
                )
                try:
                    fut.result(timeout=10)
                except Exception:
                    logger.warning("ml_session_inbound: missing-body msg falló")
                return self._respond(400, {"error": "command_without_body"})

            # Sanity check: tiene que parecer JSON.
            if not (cookie_text.startswith("[") or cookie_text.startswith("{")):
                _emit_event_threadsafe(
                    deps,
                    "ml_session_inbound_invalid_payload",
                    "warning",
                    {"from": from_num, "source": source},
                )
                fut = asyncio.run_coroutine_threadsafe(
                    deps.evolution_send(reply_to,
                        build_admin_validation_error_message(
                            "el cuerpo no parece JSON"
                        ),
                    ),
                    deps.asyncio_loop,
                )
                try:
                    fut.result(timeout=10)
                except Exception:
                    pass
                return self._respond(400, {"error": "not_json_payload"})

            _emit_event_threadsafe(
                deps,
                RECEIVED_KIND,
                "info",
                {"from": from_num, "source": source},
            )

            result = deps.validator.validate(cookie_text)
            _emit_event_threadsafe(
                deps,
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
                err_msg = build_admin_validation_error_message(result.reason or "?")
                fut = asyncio.run_coroutine_threadsafe(
                    deps.evolution_send(reply_to, err_msg), deps.asyncio_loop
                )
                try:
                    fut.result(timeout=10)
                except Exception:
                    logger.warning("ml_session_inbound: error msg send falló")
                return self._respond(400, {"error": result.reason})

            # Reload via callback (corre en el loop main).
            fut = asyncio.run_coroutine_threadsafe(
                deps.on_cookies(result.cookies), deps.asyncio_loop
            )
            try:
                reload_ok = bool(fut.result(timeout=60))
            except Exception as exc:
                logger.exception("ml_session_inbound: on_cookies raised")
                _emit_event_threadsafe(
                    deps,
                    "ml_session_inbound_reload_error",
                    "error",
                    {"error": str(exc)},
                )
                return self._respond(500, {"error": "reload_failed"})

            # Mensaje de confirmación / fallo de sesión al admin.
            if reload_ok:
                ok_msg = build_admin_success_message(len(result.cookies))
            else:
                ok_msg = build_admin_validation_failed_session_message(
                    "validación activa rechazó la sesión"
                )

            fut2 = asyncio.run_coroutine_threadsafe(
                deps.evolution_send(reply_to, ok_msg), deps.asyncio_loop
            )
            try:
                fut2.result(timeout=10)
            except Exception:
                logger.warning("ml_session_inbound: confirm msg send falló")

            return self._respond(
                200,
                {
                    "status": "ok" if reload_ok else "session_invalid_after_reload",
                    "cookies_count": result.sanitized_count,
                    "source": source,
                },
            )

    return WAInboundHandler


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


class MLInboundServer:
    """Thread-based HTTP server. Inicia con `start()`, detiene con `stop()`."""

    def __init__(
        self,
        config: MLRecoveryConfig,
        validator: MLCookieValidator,
        on_cookies: Callable[[list[dict]], Awaitable[bool]],
        evolution_send: Callable[[str, str], Awaitable[bool]],
        db: Any,
    ) -> None:
        self.config = config
        self._deps_factory = lambda loop: _Deps(
            config=config,
            validator=validator,
            on_cookies=on_cookies,
            evolution_send=evolution_send,
            asyncio_loop=loop,
            db=db,
        )
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    async def start(self) -> None:
        if self.is_running:
            return
        if not self.config.inbound_enabled:
            logger.info("ml_session_inbound: deshabilitado por config")
            return

        self._loop = asyncio.get_running_loop()
        deps = self._deps_factory(self._loop)
        handler_cls = make_handler(deps)
        try:
            self._server = ThreadingHTTPServer(
                (self.config.inbound_host, self.config.inbound_port),
                handler_cls,
            )
        except OSError as exc:
            logger.error(
                "ml_session_inbound: no se pudo bindear %s:%d (%s)",
                self.config.inbound_host,
                self.config.inbound_port,
                exc,
            )
            self._server = None
            return

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ml-session-inbound",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "ml_session_inbound: escuchando en http://%s:%d/wa/inbound",
            self.config.inbound_host,
            self.config.inbound_port,
        )

    async def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                logger.exception("ml_session_inbound: stop falló")
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._loop = None
