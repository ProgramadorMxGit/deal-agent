"""Formateadores de mensajes para WhatsApp.

Dos templates oficiales:

1. Oferta normal (formato JBL Tune 510BT solicitado por el usuario):

   ```
   *<title>*

   🔥 *<discount_percent>% de descuento*
   ❌ Antes: ~$<previous_price>~
   ✅ *AHORA: $<current_price>*

   👉 *Ver oferta:*
   <url>
   ```

2. Error de precio (formato definido en `RULES.md §12.2`):

   ```
   🚨 ERROR DE PRECIO 🚨

   *<title>*

   🔥 Precio detectado: *$<current_price>*
   ⚡ Confianza: <confidence_label>
   🏬 Tienda: <marketplace>

   👉 *Ver oferta:*
   <url>
   ```

El formateador no decide si publicar o no — eso es del dispatcher. Sólo
formatea si los datos mínimos están presentes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


_MARKETPLACE_LABELS: dict[str, str] = {
    "amazon": "Amazon México",
    "mercadolibre": "Mercado Libre",
    "walmart": "Walmart",
    "sams_club": "Sam's Club",
    "coppel": "Coppel",
    "office_depot": "Office Depot",
    "liverpool": "Liverpool",
    "sears": "Sears",
    "dell": "Dell",
    "sony_store": "Sony Store",
    "costco": "Costco",
    "bestbuy": "Best Buy",
    "other": "Tienda",
}


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class FormatterError(ValueError):
    """Datos insuficientes para formatear el mensaje."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_price(value: float) -> str:
    """Formatea un precio como `$1,299` o `$1,299.50`.

    Usa formato MXN con coma como separador de miles. Quita decimales sólo si
    la parte fraccional es 0.
    """
    if value is None:
        raise FormatterError("price is required")
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return f"${int(rounded):,}"
    return f"${rounded:,.2f}"


def _validate_url(url: Optional[str]) -> str:
    if not url or not isinstance(url, str):
        raise FormatterError("url is required")
    if not url.startswith(("http://", "https://")):
        raise FormatterError(f"url must be http(s): {url}")
    return url.strip()


def _validate_image(image_url: Optional[str]) -> str:
    if not image_url or not isinstance(image_url, str):
        raise FormatterError("image_url is required (no image -> not publishable)")
    return image_url.strip()


def _marketplace_label(marketplace: Optional[str]) -> str:
    if not marketplace:
        return "Tienda"
    return _MARKETPLACE_LABELS.get(marketplace.lower(), marketplace.title())


# ---------------------------------------------------------------------------
# Resultado del formateador (texto + media)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormattedMessage:
    text: str
    image_url: str
    type: str  # "normal" | "price_error"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def format_normal_offer(
    title: str,
    current_price: float,
    previous_price: float,
    discount_percent: float,
    url: str,
    image_url: str,
) -> FormattedMessage:
    """Formato para oferta normal (descuento >= 50%).

    Ejemplo de salida (corresponde al ejemplo JBL Tune 510BT del usuario):

        *JBL Tune 510BT - Auriculares in-Ear inalámbricos con Sonido Purebass, Color Azul*

        🔥 *57% de descuento*
        ❌ Antes: ~$899~
        ✅ *AHORA: $388*

        👉 *Ver oferta:*
        https://amzn.to/4e3yTjG
    """
    if not title or not title.strip():
        raise FormatterError("title is required")
    if discount_percent is None:
        raise FormatterError("discount_percent is required")

    title_clean = title.strip()
    image = _validate_image(image_url)
    link = _validate_url(url)
    discount_int = int(round(float(discount_percent)))
    current = _format_price(current_price)
    previous = _format_price(previous_price)

    text = (
        f"*{title_clean}*\n"
        f"\n"
        f"🔥 *{discount_int}% de descuento*\n"
        f"❌ Antes: ~{previous}~\n"
        f"✅ *AHORA: {current}*\n"
        f"\n"
        f"👉 *Ver oferta:*\n"
        f"{link}"
    )
    return FormattedMessage(text=text, image_url=image, type="normal")


def format_price_error(
    title: str,
    current_price: float,
    confidence_label: str,
    marketplace: str,
    url: str,
    image_url: str,
) -> FormattedMessage:
    """Formato para error de precio confirmado o posible (alta confianza)."""
    if not title or not title.strip():
        raise FormatterError("title is required")
    # REGLA 7: el header "🚨 ERROR DE PRECIO 🚨" sólo se permite con
    # confianza "high" o "very high". Si llega un item con `medium`, el
    # publisher debe degradarlo a oferta normal (si tiene descuento) o
    # descartarlo. Nunca se publica un PE con confianza media.
    if confidence_label not in {"very high", "high"}:
        raise FormatterError(
            f"confidence_label inválido: {confidence_label} "
            "(esperado: very high | high)"
        )

    title_clean = title.strip()
    image = _validate_image(image_url)
    link = _validate_url(url)
    current = _format_price(current_price)
    label_market = _marketplace_label(marketplace)

    text = (
        f"🚨 ERROR DE PRECIO 🚨\n"
        f"\n"
        f"*{title_clean}*\n"
        f"\n"
        f"🔥 Precio detectado: *{current}*\n"
        f"⚡ Confianza: {confidence_label}\n"
        f"🏬 Tienda: {label_market}\n"
        f"\n"
        f"👉 *Ver oferta:*\n"
        f"{link}"
    )
    return FormattedMessage(text=text, image_url=image, type="price_error")
