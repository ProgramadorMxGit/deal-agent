"""Publisher de alto nivel: outbox → formatter → Evolution API.

Responsabilidades:

- Tomar un `OutboxItem` con su payload normalizado.
- Validar imagen + precio + url (gates duros).
- Para Mercado Libre: si `affiliate_required=True` y falta `affiliate_url`,
  rechazar con razón `missing_affiliate_url`. Si hay `affiliate_url`, usarlo
  como URL de publicación (preferido sobre `canonical_url`).
- Llamar al `formatter` correspondiente (normal vs error de precio).
- Llamar al `EvolutionClient` (sendMedia con caption).
- Devolver un `PublishOutcome` que el dispatcher persiste en
  `published_messages`.

Es agnóstico al cooldown y a la cola — eso vive en `outbox_dispatcher.py`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..models import OutboxItem, OutboxType
from .evolution_client import EvolutionClient, EvolutionResponse
from .formatter import (
    FormatterError,
    FormattedMessage,
    format_normal_offer,
    format_price_error,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass
class PublishOutcome:
    success: bool
    dry_run: bool
    formatted: Optional[FormattedMessage]
    evolution_response: Optional[EvolutionResponse]
    skipped: bool = False
    skip_reason: Optional[str] = None
    error: Optional[str] = None
    # Cuando el publisher decide que el item NO debe reintentarse (e.g. es un
    # falso PE con confianza medium), expone un `discard_reason`. El
    # dispatcher honra este campo y marca el outbox como `discarded` en
    # lugar de `failed`.
    discard_reason: Optional[str] = None
    # Si el publisher transformó internamente el item (e.g. PE medium
    # degradado a oferta normal), expone el nuevo tipo y payload para que
    # el dispatcher pueda actualizarlos en el outbox antes de marcarlo.
    degraded_outbox_type: Optional[str] = None
    degraded_payload: Optional[dict] = None


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


class WhatsAppPublisher:
    """Publisher hacia un grupo de WhatsApp via Evolution API."""

    def __init__(
        self,
        client: EvolutionClient,
        target_group_id: Optional[str],
        *,
        enabled: bool = True,
        mercadolibre_affiliate_required: bool = True,
    ) -> None:
        """
        Args:
            client: Evolution client (puede estar en dry_run).
            target_group_id: JID del grupo (`...@g.us`) o teléfono.
            enabled: si False, el publisher salta todo y devuelve `skipped=True`.
            mercadolibre_affiliate_required: si True, los items ML SIN
                `affiliate_url` no se publican.
        """
        self.client = client
        self.target_group_id = target_group_id or ""
        self.enabled = enabled
        self.mercadolibre_affiliate_required = mercadolibre_affiliate_required

    async def publish(self, item: OutboxItem) -> PublishOutcome:
        """Publica el item correspondiente al outbox.

        Si el publisher está deshabilitado (`enabled=False`), retorna `skipped`.
        Si los datos no son válidos (sin imagen, sin precio, sin url, ML sin
        affiliate cuando es obligatorio) retorna `success=False` con `error`
        explicativo.
        """
        if not self.enabled:
            logger.info("publish skipped: publishing_disabled")
            return PublishOutcome(
                success=False,
                dry_run=self.client.dry_run,
                formatted=None,
                evolution_response=None,
                skipped=True,
                skip_reason="publishing_disabled",
            )

        if not self.target_group_id:
            return PublishOutcome(
                success=False,
                dry_run=self.client.dry_run,
                formatted=None,
                evolution_response=None,
                error="target_group_id_unset",
            )

        # Gate ML: si es Mercado Libre y exige afiliado, validar.
        ml_gate_error = self._mercadolibre_affiliate_gate(item)
        if ml_gate_error is not None:
            logger.warning(
                "publish ML rejected (id=%s): %s", item.id, ml_gate_error
            )
            return PublishOutcome(
                success=False,
                dry_run=self.client.dry_run,
                formatted=None,
                evolution_response=None,
                error=ml_gate_error,
            )

        # REGLA 6: medium-confidence price_error guardrail. Antes de
        # formatear, si el item es PE/possible_pe pero la confianza efectiva
        # es `medium` (o inferior), NO se publica como ERROR DE PRECIO.
        # - Si tiene descuento real >= 50%, se degrada a oferta normal.
        # - Si no, se marca para descarte.
        guardrail_outcome = self._medium_pe_guardrail(item)
        if guardrail_outcome is not None:
            return guardrail_outcome
        # Tras el guardrail, `item` puede haber sido reescrito (degradado).
        original_type = item.type
        item = self._maybe_degrade_item(item)
        was_degraded = item.type != original_type

        try:
            formatted = self._format_message(item)
        except FormatterError as exc:
            logger.warning("publish formatter rejected item id=%s: %s", item.id, exc)
            return PublishOutcome(
                success=False,
                dry_run=self.client.dry_run,
                formatted=None,
                evolution_response=None,
                error=f"formatter_error: {exc}",
            )

        # La política de publicación es siempre con imagen (sendMedia).
        evolution_response = await self.client.send_media(
            self.target_group_id,
            formatted.image_url,
            caption=formatted.text,
            file_name=_safe_filename(formatted),
        )

        return PublishOutcome(
            success=evolution_response.success,
            dry_run=evolution_response.dry_run,
            formatted=formatted,
            evolution_response=evolution_response,
            error=evolution_response.error,
            degraded_outbox_type=item.type if was_degraded else None,
            degraded_payload=dict(item.message_payload) if was_degraded else None,
        )

    # ------------------------------------------------------------------
    # Gates específicos
    # ------------------------------------------------------------------

    def _mercadolibre_affiliate_gate(self, item: OutboxItem) -> Optional[str]:
        payload = item.message_payload or {}
        marketplace = (payload.get("marketplace") or "").lower()
        if marketplace != "mercadolibre":
            return None
        if not self.mercadolibre_affiliate_required:
            return None
        affiliate_url = payload.get("affiliate_url")
        if affiliate_url:
            return None
        return "missing_affiliate_url"

    # ------------------------------------------------------------------
    # Guardrail medium-confidence price_error (REGLA 6)
    # ------------------------------------------------------------------

    def _medium_pe_guardrail(self, item: OutboxItem) -> Optional[PublishOutcome]:
        """Si el item es PE/possible_pe con confianza < high, decide qué
        hacer antes de publicar.

        Devuelve:
            - `PublishOutcome` con `discard_reason` cuando el item NO debe
              publicarse como PE ni convertirse en oferta normal.
            - `None` cuando el item puede continuar (porque es PE válido o
              porque puede degradarse a normal — la degradación se aplica
              en `_maybe_degrade_item`).
        """
        if item.type not in (
            OutboxType.PRICE_ERROR.value,
            OutboxType.POSSIBLE_PE.value,
        ):
            return None
        payload = item.message_payload or {}
        confidence = (payload.get("confidence_label") or "").lower()
        # Cualquier label distinta de "high"/"very high" se considera
        # insuficiente para publicar como ERROR DE PRECIO.
        if confidence in ("high", "very high"):
            return None

        discount_percent = payload.get("discount_percent")
        try:
            discount_value = float(discount_percent) if discount_percent is not None else None
        except (TypeError, ValueError):
            discount_value = None

        # Caso A: tiene descuento real >= 50% → permitimos degradación a
        # oferta normal en `_maybe_degrade_item`.
        if discount_value is not None and discount_value >= 50.0:
            return None

        reason = "discarded_false_price_error_medium_confidence"
        logger.warning(
            "publish guardrail (REGLA 6): item id=%s type=%s confidence=%s "
            "no tiene descuento >=50%% — descarte: %s",
            item.id,
            item.type,
            confidence or "(unset)",
            reason,
        )
        return PublishOutcome(
            success=False,
            dry_run=self.client.dry_run,
            formatted=None,
            evolution_response=None,
            error=reason,
            discard_reason=reason,
        )

    def _maybe_degrade_item(self, item: OutboxItem) -> OutboxItem:
        """Degrada un item PE con confianza medium + descuento >=50% a oferta
        normal. Devuelve un nuevo `OutboxItem` (no muta el original).
        """
        if item.type not in (
            OutboxType.PRICE_ERROR.value,
            OutboxType.POSSIBLE_PE.value,
        ):
            return item
        payload = item.message_payload or {}
        confidence = (payload.get("confidence_label") or "").lower()
        if confidence in ("high", "very high"):
            return item
        discount_percent = payload.get("discount_percent")
        try:
            discount_value = float(discount_percent) if discount_percent is not None else None
        except (TypeError, ValueError):
            discount_value = None
        if discount_value is None or discount_value < 50.0:
            return item
        # Para degradar a oferta normal necesitamos previous_price. Si no
        # está, lo derivamos a partir del descuento si es posible.
        new_payload = dict(payload)
        try:
            current = float(new_payload.get("current_price"))
        except (TypeError, ValueError):
            return item
        previous = new_payload.get("previous_price")
        if previous is None and current > 0:
            previous = round(current / (1 - discount_value / 100.0), 2)
        new_payload["previous_price"] = previous
        new_payload["discount_percent"] = discount_value
        new_payload["degraded_from"] = item.type
        new_payload["degraded_reason"] = (
            "medium_confidence_pe_with_real_discount_>=50"
        )
        # Reemplazamos el item con type=normal en runtime. La persistencia
        # se actualiza por el dispatcher cuando ve `degraded_outbox_type`.
        from dataclasses import replace as _replace

        logger.info(
            "publish guardrail (REGLA 6): degradado outbox id=%s a 'normal' "
            "(confidence=medium, discount=%.0f%%)",
            item.id,
            discount_value,
        )
        return _replace(
            item,
            type=OutboxType.NORMAL.value,
            message_payload=new_payload,
        )

    # ------------------------------------------------------------------
    # Formatter selection
    # ------------------------------------------------------------------

    def _format_message(self, item: OutboxItem) -> FormattedMessage:
        payload: dict[str, Any] = item.message_payload or {}

        title: str = payload.get("title") or ""
        image_url: str = payload.get("image_url") or ""
        current_price = payload.get("current_price")

        # `url` para publicación: prioriza affiliate_url cuando existe.
        url = (
            payload.get("affiliate_url")
            or payload.get("url")
            or payload.get("canonical_url")
            or ""
        )

        # Gates duros antes de formatear
        if not image_url:
            raise FormatterError("payload missing image_url")
        if current_price is None:
            raise FormatterError("payload missing current_price (precio no validado)")
        if not url:
            raise FormatterError("payload missing url")

        # Override de caption proveniente del MCP quality gate. Cuando está
        # presente, respetamos el texto re-escrito por el cliente y NO tocamos
        # ni la URL ni la imagen ni los precios. Esto evita que el LLM altere
        # campos materiales accidentalmente.
        #
        # Validación: el override debe respetar el formato canónico (asteriscos
        # `*...*` para título / `% de descuento` / `AHORA: $...`, y tildes
        # `~$..~` en el "Antes" cuando hay previous_price). Si la IA devuelve
        # algo sin esos marcadores, hacemos fallback al formato canónico
        # estricto en lugar de publicar markdown roto.
        caption_override = payload.get("caption_override")
        if caption_override and _caption_respects_canonical_format(
            text=str(caption_override),
            item_type=item.type,
            payload=payload,
        ):
            return FormattedMessage(
                text=str(caption_override),
                image_url=image_url,
                type=(
                    "price_error"
                    if item.type
                    in (OutboxType.PRICE_ERROR.value, OutboxType.POSSIBLE_PE.value)
                    else "normal"
                ),
            )
        if caption_override:
            logger.warning(
                "caption_override de item id=%s no respeta el formato canónico "
                "(faltan asteriscos/tildes); usando formato estricto",
                item.id,
            )

        if item.type == OutboxType.NORMAL.value:
            previous_price = payload.get("previous_price")
            discount_percent = payload.get("discount_percent")
            if previous_price is None or discount_percent is None:
                raise FormatterError(
                    "normal offer requires previous_price + discount_percent"
                )
            return format_normal_offer(
                title=title,
                current_price=float(current_price),
                previous_price=float(previous_price),
                discount_percent=float(discount_percent),
                url=url,
                image_url=image_url,
            )

        if item.type in (OutboxType.PRICE_ERROR.value, OutboxType.POSSIBLE_PE.value):
            confidence_label = payload.get("confidence_label", "high")
            marketplace = payload.get("marketplace", "other")
            return format_price_error(
                title=title,
                current_price=float(current_price),
                confidence_label=confidence_label,
                marketplace=marketplace,
                url=url,
                image_url=image_url,
            )

        raise FormatterError(f"outbox type no soportado: {item.type}")


def _safe_filename(msg: FormattedMessage) -> str:
    if msg.type == "price_error":
        return "error_de_precio.jpg"
    return "oferta.jpg"


# ---------------------------------------------------------------------------
# Validación de formato canónico para `caption_override`
# ---------------------------------------------------------------------------


_TITLE_BOLD_RE = re.compile(r"\*[^*\n]{4,}\*")  # cualquier *texto* (>=4 chars)
_DISCOUNT_BOLD_RE = re.compile(
    r"\*\s*\d{1,3}\s*%\s*de\s+descuento\s*\*", re.IGNORECASE
)
_AHORA_BOLD_RE = re.compile(
    r"\*\s*AHORA\s*:\s*\$[^*\n]+\*", re.IGNORECASE
)
_ANTES_STRIKE_RE = re.compile(r"~\s*\$[^~\n]+~")
_VER_OFERTA_BOLD_RE = re.compile(
    r"\*\s*Ver\s+oferta\s*:?\s*\*", re.IGNORECASE
)


def _caption_respects_canonical_format(
    *,
    text: str,
    item_type: str,
    payload: dict,
) -> bool:
    """Devuelve True si el `caption_override` mantiene el formato canónico.

    Reglas:
      - Título envuelto en `*...*` al menos una vez.
      - "X% de descuento" envuelto en `*...*`.
      - "AHORA: $..." envuelto en `*...*`.
      - "Ver oferta" envuelto en `*...*`.
      - Si el payload tiene `previous_price`, debe haber al menos un
        `~$...~` (formato strikethrough WhatsApp para precio anterior).
      - El texto debe contener la URL/affiliate_url declarada en el payload.

    Si alguna falla, el publisher hace fallback al formato canónico estricto
    para garantizar consistencia en el grupo de WhatsApp.
    """
    if not text:
        return False
    if not _TITLE_BOLD_RE.search(text):
        return False
    if not _DISCOUNT_BOLD_RE.search(text):
        return False
    if not _AHORA_BOLD_RE.search(text):
        return False
    if not _VER_OFERTA_BOLD_RE.search(text):
        return False
    # Si el payload tiene previous_price, el override debe mostrarlo con
    # tildes ~$...~ para que WhatsApp lo renderice tachado.
    previous_price = payload.get("previous_price")
    if previous_price is not None and not _ANTES_STRIKE_RE.search(text):
        return False
    # La URL del payload debe estar en el texto (la IA NO debe quitar el
    # link). Comparamos con affiliate_url > url > canonical_url, lo que
    # exista. Si ninguna está, no validamos esa parte.
    expected_url = (
        payload.get("affiliate_url")
        or payload.get("url")
        or payload.get("canonical_url")
        or ""
    )
    if expected_url and expected_url not in text:
        return False
    return True
