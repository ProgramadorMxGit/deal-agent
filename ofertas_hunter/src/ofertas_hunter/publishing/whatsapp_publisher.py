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
from .screenshot_capturer import ScreenshotCapturer


logger = logging.getLogger(__name__)


def _is_valid_affiliate_url(url: Optional[str]) -> bool:
    """True si la URL es un enlace de afiliado Amazon válido.

    Un enlace válido contiene `amzn.to/` (link corto SiteStripe) o `tag=`
    (parámetro de afiliado). Cualquier otra URL (incluida la directa
    `amazon.com.mx/dp/...` sin tag) se considera inválida.
    """
    if not url or not isinstance(url, str):
        return False
    return ("amzn.to/" in url) or ("tag=" in url)


# Texto que delata precio por unidad / pieza / cuota dentro del raw text del
# precio actual. Si el `current_price_raw_text` lo contiene, el precio
# extraído NO es el total y NO debe publicarse (falso positivo 99/100%).
_UNIT_PRICE_TEXT_RE = re.compile(
    r"(/|\bpor\b)\s*(unidad(?:es)?|unid\.?|pieza(?:s)?|pza\.?|pz\.?|count|each|recuento)\b",
    re.IGNORECASE,
)

# Texto que delata precio por unidad de medida (kilo, gramo, litro, ml, onza)
# o cuota/mensualidad. Usado por el gate de Mercado Libre para no tomar el
# "precio por kilo" ($295.45/kg) ni la mensualidad como precio total.
_ML_UNIT_PRICE_TEXT_RE = re.compile(
    r"(/|\bpor\b)\s*"
    r"(kilo(?:gramo)?s?|kg|gramos?|gr?\b|litros?|lt?\b|ml\b|mililitros?|"
    r"onzas?|oz\b|unidad(?:es)?|pieza(?:s)?|pza\.?|porci[oó]n(?:es)?|c/u)",
    re.IGNORECASE,
)
_ML_INSTALLMENT_TEXT_RE = re.compile(
    r"(meses?\s+sin\s+intereses|\bMSI\b|/\s*mes\b|al\s+mes\b|"
    r"\bcuota[s]?\b|mensualidad(?:es)?)",
    re.IGNORECASE,
)


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
        amazon_affiliate_required: bool = True,
        amazon_min_discount_percent: float = 50.0,
        amazon_extreme_discount_threshold: float = 90.0,
        amazon_min_absolute_price: float = 10.0,
        ml_extreme_discount_threshold: float = 70.0,
        screenshot_capturer: Optional["ScreenshotCapturer"] = None,
    ) -> None:
        """
        Args:
            client: Evolution client (puede estar en dry_run).
            target_group_id: JID del grupo (`...@g.us`) o teléfono.
            enabled: si False, el publisher salta todo y devuelve `skipped=True`.
            mercadolibre_affiliate_required: si True, los items ML SIN
                `affiliate_url` no se publican.
            amazon_affiliate_required: si True, los items Amazon SIN
                `affiliate_url` válido (amzn.to/ o tag=) no se publican.
            amazon_min_discount_percent: descuento mínimo verificado para
                publicar una oferta normal de Amazon.
            screenshot_capturer: si se pasa, antes de publicar se captura un
                screenshot del PDP real (imagen + título + precio + buy box) y
                se envía ese en lugar de la imagen pública. Best-effort: si la
                captura falla, se usa `image_url` como fallback.
        """
        self.client = client
        self.target_group_id = target_group_id or ""
        self.enabled = enabled
        self.mercadolibre_affiliate_required = mercadolibre_affiliate_required
        self.amazon_affiliate_required = amazon_affiliate_required
        self.amazon_min_discount_percent = amazon_min_discount_percent
        self.amazon_extreme_discount_threshold = amazon_extreme_discount_threshold
        self.amazon_min_absolute_price = amazon_min_absolute_price
        self.ml_extreme_discount_threshold = ml_extreme_discount_threshold
        self.screenshot_capturer = screenshot_capturer

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

        # Gate Amazon: afiliado válido + precio anterior verificado.
        amazon_gate = self._amazon_gate(item)
        if amazon_gate is not None:
            return amazon_gate

        # Gate Mercado Libre: precio anterior verificado + anti unit-price /
        # mensualidad / variante mismatch / descuento coherente. Mismo espíritu
        # que el gate de Amazon. Bloquea falsos positivos de descuento ML.
        ml_price_gate = self._ml_price_gate(item)
        if ml_price_gate is not None:
            return ml_price_gate

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
        # Si hay capturer, intentamos un screenshot del PDP real (imagen +
        # título + precio + buy box) y lo enviamos en lugar de la imagen
        # pública. Best-effort: si falla, usamos `formatted.image_url`.
        media: Any = formatted.image_url
        file_name = _safe_filename(formatted)
        if self.screenshot_capturer is not None:
            try:
                shot = await self.screenshot_capturer.capture(item.message_payload or {})
            except Exception as exc:  # nunca tumbar la publicación
                logger.warning("screenshot capture raised (id=%s): %s", item.id, exc)
                shot = None
            if shot:
                media = shot
                file_name = _screenshot_filename(formatted)
                logger.info(
                    "publish id=%s usando screenshot PDP (%d bytes)", item.id, len(shot)
                )
            else:
                logger.info(
                    "publish id=%s sin screenshot, fallback a imagen pública", item.id
                )

        evolution_response = await self.client.send_media(
            self.target_group_id,
            media,
            caption=formatted.text,
            file_name=file_name,
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

    def _amazon_gate(self, item: OutboxItem) -> Optional[PublishOutcome]:
        """Guardrail duro para Amazon.

        - Exige `affiliate_url` válido (amzn.to/ o tag=) si está configurado.
        - Para ofertas NORMAL (descuento) exige precio anterior verificado:
          `old_price_verified` truthy, `previous_price > current_price`,
          descuento declarado coherente con el calculado (tolerancia 2 pts),
          y descuento >= mínimo configurado.
        - Los price_error de Amazon no exigen precio anterior (no muestran
          "Antes"), pero sí afiliado válido.

        Devuelve un `PublishOutcome` con `discard_reason` cuando bloquea, o
        `None` cuando el item puede continuar.
        """
        payload = item.message_payload or {}
        if (payload.get("marketplace") or "").lower() != "amazon":
            return None

        # 1. Afiliado válido.
        if self.amazon_affiliate_required:
            if not _is_valid_affiliate_url(payload.get("affiliate_url")):
                return self._amazon_block(item, "amazon_missing_affiliate")

        # 2. Precio anterior verificado (solo ofertas de descuento).
        if item.type == OutboxType.NORMAL.value:
            if not payload.get("old_price_verified"):
                return self._amazon_block(item, "amazon_no_verified_old_price")
            try:
                cur = float(payload.get("current_price"))
                prev = float(payload.get("previous_price"))
            except (TypeError, ValueError):
                return self._amazon_block(item, "amazon_no_verified_old_price")
            if prev <= cur:
                return self._amazon_block(item, "amazon_no_verified_old_price")

            # 2a. GUARDRAIL anti falso-positivo (precio por unidad / extremo).
            #     Defensa en profundidad: aunque el extractor falle, aquí se
            #     bloquea el típico "$0.69 / unidad" publicado como total.
            raw_text = str(payload.get("current_price_raw_text") or "")
            if _UNIT_PRICE_TEXT_RE.search(raw_text):
                return self._amazon_block(item, "amazon_unit_price_as_current_price")

            computed = round((prev - cur) / prev * 100)

            # 2b. Descuento extremo: exige verificación explícita.
            if computed >= self.amazon_extreme_discount_threshold:
                if not payload.get("extreme_discount_verified"):
                    return self._amazon_block(item, "amazon_extreme_discount_unverified")

            # 2c. Precio absoluto sospechoso: muy bajo frente a un old_price alto
            #     (patrón de precio por unidad tomado como total).
            if cur < self.amazon_min_absolute_price and prev > 50:
                if not payload.get("extreme_discount_verified"):
                    return self._amazon_block(item, "amazon_current_price_suspicious")

            # 2d. current_price < 1% del old_price → casi siempre unit price.
            if prev > 0 and cur < (prev * 0.01):
                if not payload.get("extreme_discount_verified"):
                    return self._amazon_block(item, "amazon_current_price_suspicious")

            declared = payload.get("discount_percent")
            if declared is not None:
                try:
                    if abs(float(declared) - computed) > 2:
                        return self._amazon_block(item, "amazon_discount_mismatch")
                except (TypeError, ValueError):
                    return self._amazon_block(item, "amazon_discount_mismatch")
            if computed < self.amazon_min_discount_percent:
                return self._amazon_block(item, "amazon_below_min_discount")

        return None

    def _amazon_block(self, item: OutboxItem, reason: str) -> PublishOutcome:
        logger.warning(
            "publish Amazon bloqueado (id=%s): %s", item.id, reason
        )
        return PublishOutcome(
            success=False,
            dry_run=self.client.dry_run,
            formatted=None,
            evolution_response=None,
            error=reason,
            discard_reason=reason,
        )

    def _ml_price_gate(self, item: OutboxItem) -> Optional[PublishOutcome]:
        """Guardrail duro para Mercado Libre (anti falsos positivos de descuento).

        Para ofertas NORMAL de ML exige:
        - previous_price verificado (ml_previous_price_verified) y > current_price.
        - current_price no proveniente de precio por unidad/kilo/gramo/litro.
        - current_price no proveniente de mensualidad/cuota.
        - sin variant_mismatch.
        - descuento declarado coherente con el calculado (tolerancia 2 pts).
        - descuento extremo (>= umbral) verificado.

        Fail-safe: si faltan los flags de verificación, se trata como NO
        verificado y se bloquea. NO toca Amazon ni price_error.
        """
        payload = item.message_payload or {}
        if (payload.get("marketplace") or "").lower() != "mercadolibre":
            return None
        if item.type != OutboxType.NORMAL.value:
            return None

        # Variante incorrecta (precio de otra presentación/sabor/peso).
        if payload.get("ml_variant_mismatch") is True:
            return self._ml_block(item, "ml_variant_mismatch")
        if "variant_mismatch" in (payload.get("not_publishable_reasons") or []):
            return self._ml_block(item, "ml_variant_mismatch")

        # current_price = precio por unidad/kilo o mensualidad → no es total.
        raw_current = str(payload.get("current_price_raw_text") or "")
        if payload.get("current_price_is_unit_price") is True or _ML_UNIT_PRICE_TEXT_RE.search(raw_current):
            return self._ml_block(item, "ml_unit_price_as_current_price")
        if payload.get("current_price_is_installment") is True or _ML_INSTALLMENT_TEXT_RE.search(raw_current):
            return self._ml_block(item, "ml_installment_as_current_price")

        # Precio anterior verificado obligatorio para publicar descuento.
        if not payload.get("ml_previous_price_verified"):
            return self._ml_block(item, "ml_no_verified_previous_price")

        try:
            cur = float(payload.get("current_price"))
            prev = float(payload.get("previous_price"))
        except (TypeError, ValueError):
            return self._ml_block(item, "ml_no_verified_current_price")
        if cur <= 0:
            return self._ml_block(item, "ml_no_verified_current_price")
        if prev <= cur:
            return self._ml_block(item, "ml_no_verified_previous_price")

        # previous_price de otra variante (flag explícito).
        if payload.get("ml_previous_price_from_other_variant") is True:
            return self._ml_block(item, "ml_previous_price_from_other_variant")

        computed = round((prev - cur) / prev * 100)

        # Descuento extremo: exige verificación explícita.
        if computed >= self.ml_extreme_discount_threshold:
            if not payload.get("ml_extreme_discount_verified"):
                return self._ml_block(item, "ml_extreme_discount_unverified")

        # Descuento declarado coherente con el calculado.
        declared = payload.get("discount_percent")
        if declared is not None:
            try:
                if abs(float(declared) - computed) > 2:
                    return self._ml_block(item, "ml_discount_mismatch")
            except (TypeError, ValueError):
                return self._ml_block(item, "ml_discount_mismatch")

        return None

    def _ml_block(self, item: OutboxItem, reason: str) -> PublishOutcome:
        logger.warning("publish ML bloqueado (id=%s): %s", item.id, reason)
        return PublishOutcome(
            success=False,
            dry_run=self.client.dry_run,
            formatted=None,
            evolution_response=None,
            error=reason,
            discard_reason=reason,
        )

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
        # Para Amazon NUNCA degradamos a oferta normal derivando el precio
        # anterior desde el descuento: una oferta normal Amazon exige
        # `old_price_verified`. Si no lo tiene, no se degrada (y el
        # `_amazon_gate` ya lo habrá bloqueado antes de llegar aquí).
        if (payload.get("marketplace") or "").lower() == "amazon" and not payload.get(
            "old_price_verified"
        ):
            return item
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
            marketplace = (payload.get("marketplace") or "").lower()
            if discount_percent is None:
                raise FormatterError(
                    "normal offer requires discount_percent"
                )
            # Para Amazon NUNCA derivamos el precio anterior desde el
            # descuento: exigimos un previous_price real y verificado (el
            # gate de Amazon ya lo validó). Inventar "Antes" a partir del
            # % es justamente el bug que estamos corrigiendo.
            if marketplace == "amazon":
                if not payload.get("old_price_verified"):
                    raise FormatterError(
                        "amazon offer requires verified previous_price"
                    )
                if previous_price is None:
                    raise FormatterError(
                        "amazon offer requires previous_price"
                    )
            else:
                # ML/otros: NUNCA derivar previous_price desde el descuento.
                # Inventar "Antes" a partir del % es el bug de falsos positivos
                # (descuentos como 78% sin precio anterior tachado real). El
                # gate ML ya exige previous_price verificado; aquí solo se
                # formatea lo verificado. Si no hay previous_price real, el
                # formatter lanza FormatterError y el item no se publica.
                pass
            if previous_price is None:
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


def _screenshot_filename(msg: FormattedMessage) -> str:
    if msg.type == "price_error":
        return "error_de_precio_captura.jpg"
    return "oferta_captura.jpg"


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
