"""Cliente HTTP a Evolution API.

Endpoints usados:

- `POST {base_url}/message/sendText/{instance}` — texto plano.
- `POST {base_url}/message/sendMedia/{instance}` — media (imagen) con caption.

Headers obligatorios:

- `Content-Type: application/json`
- `<EVOLUTION_API_KEY_HEADER>: <EVOLUTION_API_KEY>`

Modos de operación:

- **Dry-run** (default en `.env.example`): no hace request HTTP, sólo registra
  qué hubiera enviado y devuelve un `EvolutionResponse(success=True, dry_run=True)`.
- **Real**: usa httpx async con timeouts y reintentos limitados.

El cliente **no** decide si publicar ni aplica cooldown — eso es del
dispatcher. Aquí sólo se hace la llamada HTTP.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass
class EvolutionResponse:
    """Respuesta normalizada del cliente Evolution.

    `raw` contiene la respuesta JSON cuando aplica (modo real). En dry-run,
    `raw` contiene el payload simulado que se hubiera enviado.
    """

    success: bool
    status_code: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    dry_run: bool = False
    temporary: bool = False


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class EvolutionConfigError(RuntimeError):
    """Falta configuración esencial (URL, instance, api key)."""


# ---------------------------------------------------------------------------
# Helpers de payload
# ---------------------------------------------------------------------------


_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$")
_CONNECTION_CLOSED_RE = re.compile(r"connection\s+closed", re.IGNORECASE)


def _flatten_error_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return " ".join(_flatten_error_text(part) for part in data)
    if isinstance(data, dict):
        return " ".join(_flatten_error_text(value) for value in data.values())
    return str(data)


def _classify_response_error(
    status_code: int, data: dict[str, Any]
) -> tuple[str, bool]:
    if _CONNECTION_CLOSED_RE.search(_flatten_error_text(data)):
        return "connection_closed", True
    return f"http_status_{status_code}", False


def _resolve_media_payload(media: str | bytes | Path) -> tuple[str, str]:
    """Convierte el `media` en `(raw_base64, mimetype)`.

    Acepta:
    - bytes crudos → base64.
    - `Path` o str de path local existente → lee y base64.
    - `data:image/...;base64,...` → extrae partes.
    - URL `http(s)://...` → se devuelve tal cual; Evolution maneja descarga.
    - cadena base64 ya decodificada → se asume `image/jpeg`.
    """
    if isinstance(media, bytes):
        return base64.b64encode(media).decode("ascii"), "image/jpeg"

    if isinstance(media, Path):
        if not media.exists():
            raise FileNotFoundError(f"media path no existe: {media}")
        return (
            base64.b64encode(media.read_bytes()).decode("ascii"),
            _guess_mime(media.name),
        )

    if isinstance(media, str):
        # data URL
        m = _DATA_URL_RE.match(media)
        if m:
            return m.group("data"), m.group("mime")
        # URL pública
        if media.startswith(("http://", "https://")):
            return media, "image/jpeg"
        # Path local en string
        path = Path(media)
        if path.exists():
            return (
                base64.b64encode(path.read_bytes()).decode("ascii"),
                _guess_mime(path.name),
            )
        # base64 puro (asumido)
        return media, "image/jpeg"

    raise TypeError(f"media de tipo no soportado: {type(media)!r}")


def _guess_mime(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


class EvolutionClient:
    """Cliente async para Evolution API."""

    def __init__(
        self,
        base_url: Optional[str],
        api_key: Optional[str],
        instance: Optional[str],
        *,
        api_key_header: str = "apikey",
        dry_run: bool = True,
        timeout_seconds: float = 25.0,
        media_timeout_seconds: float = 40.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.instance = instance or ""
        self.api_key_header = (api_key_header or "apikey").strip() or "apikey"
        self.dry_run = dry_run
        self.timeout_seconds = timeout_seconds
        self.media_timeout_seconds = media_timeout_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.instance)

    def _ensure_configured(self) -> None:
        if self.configured:
            return
        missing = [
            name
            for name, value in (
                ("base_url", self.base_url),
                ("api_key", self.api_key),
                ("instance", self.instance),
            )
            if not value
        ]
        raise EvolutionConfigError(
            f"Evolution client mal configurado, faltan: {', '.join(missing)}"
        )

    async def __aenter__(self) -> "EvolutionClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Headers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            self.api_key_header: self.api_key,
        }

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def send_text(self, number: str, text: str) -> EvolutionResponse:
        """Envía un mensaje de texto a `number` (puede ser JID `...@g.us`)."""
        if not number:
            return EvolutionResponse(success=False, error="number_required")
        if not text:
            return EvolutionResponse(success=False, error="text_required")

        payload = {"number": number, "text": text}

        if self.dry_run:
            logger.info(
                "[DRY-RUN] sendText to %s | %s",
                number,
                text[:120].replace("\n", " ⏎ "),
            )
            return EvolutionResponse(success=True, dry_run=True, raw={"payload": payload})

        return await self._post("/message/sendText", payload, self.timeout_seconds)

    async def send_image(
        self,
        number: str,
        image_url: str,
        caption: str = "",
    ) -> EvolutionResponse:
        """Alias explícito para el contrato de imagen documentado."""
        return await self.send_media(number, image_url, caption=caption)

    async def send_media(
        self,
        number: str,
        media: str | bytes | Path,
        caption: str = "",
        *,
        file_name: str = "oferta.jpg",
    ) -> EvolutionResponse:
        """Envía una imagen con caption.

        `media` puede ser path local, bytes, base64, data URL o URL pública.
        """
        if not number:
            return EvolutionResponse(success=False, error="number_required")
        try:
            media_value, mimetype = _resolve_media_payload(media)
        except (FileNotFoundError, TypeError) as exc:
            return EvolutionResponse(success=False, error=f"media_invalid: {exc}")

        payload: dict[str, Any] = {
            "number": number,
            "mediatype": "image",
            "mimetype": mimetype,
            "media": media_value,
            "fileName": file_name,
            "caption": caption,
        }

        if self.dry_run:
            preview = caption[:120].replace("\n", " ⏎ ") if caption else "(no caption)"
            logger.info(
                "[DRY-RUN] sendMedia to %s | %s | mimetype=%s | media_len=%d",
                number,
                preview,
                mimetype,
                len(media_value),
            )
            # No metemos los bytes de media en el dry-run para no saturar logs
            preview_payload = dict(payload)
            preview_payload["media"] = (
                f"<{len(media_value)} chars>"
                if not media_value.startswith(("http://", "https://"))
                else media_value
            )
            return EvolutionResponse(
                success=True,
                dry_run=True,
                raw={"payload": preview_payload},
            )

        return await self._post("/message/sendMedia", payload, self.media_timeout_seconds)

    async def connection_state(self) -> str:
        """Consulta el estado de la instancia Evolution (`open`, etc.)."""
        if self.dry_run:
            return "dry_run"
        data = await self._get("/instance/connectionState", timeout=self.timeout_seconds)
        instance_data = data.get("instance")
        if isinstance(instance_data, dict):
            state = instance_data.get("state")
            if isinstance(state, str):
                return state
        raise RuntimeError("connection_state_missing")

    # ------------------------------------------------------------------
    # Helpers HTTP
    # ------------------------------------------------------------------

    async def _post(
        self, endpoint: str, payload: dict[str, Any], timeout: float
    ) -> EvolutionResponse:
        self._ensure_configured()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout)

        url = f"{self.base_url}{endpoint}/{self.instance}"
        try:
            resp = await self._client.post(
                url, json=payload, headers=self._headers(), timeout=timeout
            )
        except httpx.HTTPError as exc:
            logger.warning("Evolution %s failed: %s", endpoint, exc)
            return EvolutionResponse(
                success=False,
                error=f"http_error: {exc}",
                temporary=True,
            )

        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            data = {"text": resp.text}

        success = resp.status_code in (200, 201)
        if not success:
            error, temporary = _classify_response_error(resp.status_code, data)
            logger.warning(
                "Evolution %s status=%d body=%s",
                endpoint,
                resp.status_code,
                str(data)[:300],
            )
        else:
            error, temporary = None, False

        return EvolutionResponse(
            success=success,
            status_code=resp.status_code,
            raw=data if isinstance(data, dict) else {"data": data},
            error=error,
            temporary=temporary,
        )

    async def _get(self, endpoint: str, *, timeout: float) -> dict[str, Any]:
        self._ensure_configured()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout)

        url = f"{self.base_url}{endpoint}/{self.instance}"
        resp = await self._client.get(url, headers=self._headers(), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("invalid_json_response")
