"""
price_parser.py — Extrae y calcula descuentos del DOM de Amazon.com.mx
Auto-sanado por DomHealer cuando los selectores fallan.
"""
import re
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - bs4 siempre está en deps
    BeautifulSoup = None  # type: ignore

logger = logging.getLogger("price_parser")

SELECTORS_PATH = Path(__file__).parent / "selectors.json"


# Contenedores del bloque PRINCIPAL de precio (producto seleccionado).
_MAIN_PRICE_CONTAINERS = (
    "#corePriceDisplay_desktop_feature_div",
    "#apex_offerDisplay_desktop",
    "#corePrice_desktop",
    "#corePrice_feature_div",
    "#price",
)

# Contenedores que NUNCA deben aportar el precio anterior (variantes,
# otros vendedores, entrega/mensualidades, comparativas).
_EXCLUDED_OLD_PRICE_CONTAINERS = (
    "#twister",
    "#tp-inline-twister-dim-values-container",
    "#inline-twister-row",
    "#variation_color_name",
    "#variation_size_name",
    "#aod-offer-list",
    "#aod-offer",
    "#mbc",
    "#mir-layout-DELIVERY_BLOCK",
    "#sims-consolidated",
    "#similarities_feature_div",
    "#rhf",
    "#desktop-dp-sims",
)

# Texto que delata mensualidad / MSI dentro del nodo o su contexto.
_MONTHLY_RE = re.compile(
    r"(\bx\s*\d+\s*meses\b|\bmeses\s+sin\s+intereses\b|\bmensual|\b/\s*mes\b|\bMSI\b)",
    re.IGNORECASE,
)

_UNIT_PRICE_RE = re.compile(
    r"(/|\bpor\b)\s*(unidad(?:es)?|unid\.?|pieza(?:s)?|pza\.?|pz\.?|kg|g|gr|ml|l|litro(?:s)?|metro(?:s)?)\b",
    re.IGNORECASE,
)


def _text_immediately_marks_unit_price(price_text: str, context_text: str) -> bool:
    """True when this exact candidate is followed by `/ unidad` or similar."""
    if not price_text or not context_text:
        return False
    compact_context = re.sub(r"\s+", " ", context_text.strip())
    escaped = re.escape(re.sub(r"\s+", " ", price_text.strip()))
    return bool(
        re.search(
            rf"{escaped}\s*(?:/|\bpor\b)\s*(?:unidad(?:es)?|unid\.?|pieza(?:s)?|pza\.?|pz\.?|kg|g|gr|ml|l|litro(?:s)?|metro(?:s)?)\b",
            compact_context,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_unit_price_node(el) -> bool:
    """Detecta si un nodo de precio representa precio unitario, no total."""
    price_text = el.get_text(" ", strip=True)
    contexts: list[str] = [price_text]
    parent = getattr(el, "parent", None)
    if parent is not None:
        contexts.append(parent.get_text(" ", strip=True))
        grandparent = getattr(parent, "parent", None)
        if grandparent is not None:
            contexts.append(grandparent.get_text(" ", strip=True))
    for context in contexts:
        if _text_immediately_marks_unit_price(price_text, context):
            return True
    return False


def _looks_like_previous_price_node(el) -> bool:
    """True si el candidato vive dentro de un precio tachado/de lista."""
    node = el
    for _ in range(4):
        if node is None:
            return False
        attrs = getattr(node, "attrs", {}) or {}
        classes = attrs.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        class_text = " ".join(str(c) for c in classes).lower()
        node_id = str(attrs.get("id") or "").lower()
        if (
            "basisprice" in class_text
            or "a-text-price" in class_text
            or attrs.get("data-a-strike") == "true"
            or "listprice" in node_id
            or "was_price" in node_id
        ):
            return True
        node = getattr(node, "parent", None)
    return False


def load_selectors() -> dict:
    try:
        return json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error cargando selectores: {e}")
        return {}


def parse_price_text(text: str) -> Optional[float]:
    """Convierte texto de precio MX a float. Ej: '$1,299.00' → 1299.0"""
    if not text:
        return None
    # Eliminar símbolo de moneda y espacios
    cleaned = re.sub(r'[^\d,.]', '', text.strip())
    if not cleaned:
        return None
    # Formato MX: 1,299.00 o 1299.00 o 1.299,00
    # Detectar si la coma es separador de miles o decimal
    if ',' in cleaned and '.' in cleaned:
        # Detectar formato: si el punto viene antes de la coma → formato europeo (1.299,00)
        dot_pos = cleaned.index('.')
        comma_pos = cleaned.index(',')
        if dot_pos < comma_pos:
            # Formato europeo: 1.299,00 → quitar puntos de miles, coma a punto decimal
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            # Formato americano: 1,299.00 → quitar comas de miles
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned and '.' not in cleaned:
        # Puede ser 1,299 (miles) o 1,29 (decimal europeo)
        parts = cleaned.split(',')
        if len(parts[-1]) == 2:
            # Probablemente decimal europeo: 1,29
            cleaned = cleaned.replace(',', '.')
        else:
            # Miles: 1,299
            cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_discount_from_text(text: str) -> Optional[int]:
    """Extrae porcentaje de descuento de texto. Ej: '-65%' → 65, '65% de descuento' → 65"""
    if not text:
        return None
    match = re.search(r'(\d{1,3})\s*%', text)
    if match:
        val = int(match.group(1))
        if 1 <= val <= 99:
            return val
    return None


def calculate_discount(price_current: float, price_original: float) -> Optional[int]:
    """Calcula porcentaje de descuento entre precio actual y original."""
    if not price_current or not price_original or price_original <= 0:
        return None
    if price_current >= price_original:
        return None
    discount = ((price_original - price_current) / price_original) * 100
    return int(round(discount))


def extract_verified_old_price(html: str) -> Tuple[Optional[float], Optional[str]]:
    """Extrae el precio anterior SOLO si es confiable y verificable.

    Regla estricta (corrige el bug de descuentos inventados):
    - El precio anterior debe ser un precio tachado / de lista que viva
      DENTRO del bloque principal de precio del producto seleccionado.
    - Se rechazan precios de variantes (twister), otros vendedores (aod),
      bloques de entrega/mensualidad (MSI) y secciones de recomendados.
    - Si no hay un precio anterior confiable → (None, None).

    Devuelve `(old_price, source)` donde `source` indica de qué selector
    salió (para trazabilidad en el payload).
    """
    if not html or BeautifulSoup is None:
        return None, None

    soup = BeautifulSoup(html, "html.parser")

    # 1. Localizar el bloque principal de precio.
    main_block = None
    for sel in _MAIN_PRICE_CONTAINERS:
        main_block = soup.select_one(sel)
        if main_block is not None:
            break
    if main_block is None:
        # Sin bloque principal identificable, no arriesgamos un precio anterior.
        return None, None

    # 2. Defensa: si dentro del bloque principal hay un contenedor excluido
    #    anidado, lo descartamos del análisis quitándolo del árbol local.
    for excluded_sel in _EXCLUDED_OLD_PRICE_CONTAINERS:
        for node in main_block.select(excluded_sel):
            node.decompose()

    # 3. Buscar precio tachado / de lista dentro del bloque principal.
    strike_selectors = (
        (".basisPrice .a-price.a-text-price .a-offscreen", "basis_price_strike"),
        (".basisPrice .a-offscreen", "basis_price"),
        ("[data-a-strike='true'] .a-offscreen", "data_strike"),
        (".a-price.a-text-price .a-offscreen", "text_price_strike"),
        (".a-text-strike", "text_strike"),
        ("#priceblock_was_price .a-offscreen", "was_price"),
        ("#listPrice", "list_price"),
    )

    for sel, source in strike_selectors:
        for el in main_block.select(sel):
            # Rechazar si el contexto sugiere mensualidad / MSI.
            context_text = " ".join(
                t for t in (
                    el.get_text(" ", strip=True),
                    (el.parent.get_text(" ", strip=True) if el.parent else ""),
                )
            )
            if _MONTHLY_RE.search(context_text):
                continue
            price = parse_price_text(el.get_text(strip=True))
            if price and price > 0:
                return price, source

    return None, None


def extract_verified_current_price(html: str) -> Tuple[Optional[float], Optional[str]]:
    """Extrae precio actual evitando precios por unidad.

    Amazon puede renderizar el precio total y el unitario en el mismo bloque,
    por ejemplo `$148.00` y `$0.74 / unidad`. El unitario nunca debe alimentar
    `current_price` porque genera descuentos falsos de 99-100%.
    """
    if not html or BeautifulSoup is None:
        return None, None

    soup = BeautifulSoup(html, "html.parser")
    selectors = (
        (
            "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
            "core_price_display_price_to_pay",
        ),
        ("#apex_offerDisplay_desktop .priceToPay .a-offscreen", "apex_price_to_pay"),
        ("#corePrice_desktop .priceToPay .a-offscreen", "core_price_to_pay"),
        (".priceToPay .a-offscreen", "price_to_pay"),
        ("#priceblock_ourprice", "priceblock_ourprice"),
        ("#priceblock_dealprice", "priceblock_dealprice"),
        ("#price_inside_buybox", "price_inside_buybox"),
        ("#newBuyBoxPrice", "new_buybox_price"),
        (
            "#corePriceDisplay_desktop_feature_div .a-price[data-a-color='price'] .a-offscreen",
            "core_price_display_color_price",
        ),
        (
            "#apex_offerDisplay_desktop .a-price[data-a-color='price'] .a-offscreen",
            "apex_color_price",
        ),
        (
            "#corePrice_desktop .a-price[data-a-color='price'] .a-offscreen",
            "core_color_price",
        ),
        (
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "core_price_display_any_price",
        ),
        ("#apex_offerDisplay_desktop .a-price .a-offscreen", "apex_any_price"),
        ("#corePrice_desktop .a-price .a-offscreen", "core_any_price"),
        (".a-price[data-a-color='price'] .a-offscreen", "color_price"),
        ("#price .a-offscreen", "price_any"),
    )

    for sel, source in selectors:
        for el in soup.select(sel):
            text = el.get_text(strip=True)
            if (
                not text
                or _looks_like_unit_price_node(el)
                or _looks_like_previous_price_node(el)
            ):
                continue
            if _MONTHLY_RE.search(text):
                continue
            price = parse_price_text(text)
            if price and price > 0:
                return price, source
    return None, None


async def extract_product_data_from_page(page, url: str) -> dict:
    """
    Extrae datos completos de un producto desde su página de detalle.
    Usa múltiples estrategias de fallback para máxima robustez.
    """
    selectors = load_selectors()
    pp = selectors.get("product_page", {})

    result = {
        "url": url,
        "title": None,
        "price_current": None,
        "price_original": None,
        "old_price_source": None,
        "old_price_verified": False,
        "current_price_source": None,
        "current_price_raw_text": None,
        "current_price_is_unit_price": False,
        "discount_percent": None,
        "discount_source": None,  # "badge", "calculated", "coupon"
        "asin": None,
        "rating": None,
        "reviews": None,
        "availability": None,
        "category": None,
        "has_deal": False,
        "has_coupon": False,
        "raw_html_snippet": None,
        # Campos añadidos por la integración con `ofertas_hunter`. El
        # adapter usa estos campos para construir Product + OutboxItem.
        # Si quedan en None, el adapter rechaza el item antes de encolar.
        "image_url": None,
        "brand": None,
    }

    try:
        # ASIN desde URL
        asin_match = re.search(r'/dp/([A-Z0-9]{10})', url)
        if asin_match:
            result["asin"] = asin_match.group(1)

        # Título
        for sel in [pp.get("title", ""), "#productTitle", "#title span", "h1 span"]:
            if not sel:
                continue
            try:
                el = await page.query_selector(sel)
                if el:
                    result["title"] = (await el.inner_text()).strip()
                    break
            except Exception:
                continue

        # Precio actual — múltiples estrategias
        price_current = await _extract_current_price(page, pp)
        result["price_current"] = price_current

        # Trazabilidad del precio actual: fuente + texto crudo + flag unit-price.
        # Permite al publisher bloquear falsos positivos (precio por unidad)
        # aunque el extractor se equivoque.
        result["current_price_source"] = None
        result["current_price_raw_text"] = None
        result["current_price_is_unit_price"] = False
        try:
            _page_html_cp = await page.content()
            _cp, _cp_src = extract_verified_current_price(_page_html_cp)
            result["current_price_source"] = _cp_src
            # Texto crudo del bloque principal de precio (recortado).
            _block = None
            for _sel in (
                "#corePriceDisplay_desktop_feature_div",
                "#apex_offerDisplay_desktop",
                "#corePrice_desktop",
                "#corePrice_feature_div",
            ):
                _block = await page.query_selector(_sel)
                if _block is not None:
                    break
            if _block is not None:
                _raw = (await _block.inner_text()).strip().replace("\n", " | ")
                result["current_price_raw_text"] = _raw[:200]
                result["current_price_is_unit_price"] = bool(
                    _UNIT_PRICE_RE.search(_raw)
                )
        except Exception as exc:
            logger.debug(f"current_price trazabilidad falló: {exc}")

        # Precio original — SOLO verificado desde el bloque principal de precio.
        # Esto corrige el bug de descuentos inventados (precios de variantes,
        # otros vendedores, mensualidades, etc. NO se usan como precio anterior).
        price_original = None
        old_price_source = None
        try:
            page_html = await page.content()
            price_original, old_price_source = extract_verified_old_price(page_html)
        except Exception as exc:
            logger.debug(f"extract_verified_old_price falló: {exc}")
        result["price_original"] = price_original
        result["old_price_source"] = old_price_source
        result["old_price_verified"] = bool(
            price_original is not None
            and price_current is not None
            and price_original > price_current
        )

        # Descuento: SOLO se considera válido si hay precio anterior
        # verificado. El badge visible se conserva como dato informativo,
        # pero NO genera un descuento publicable por sí solo (Amazon a veces
        # muestra "-X%" sin precio anterior real del producto seleccionado).
        discount_from_badge = await _extract_discount_badge(page, pp)
        result["discount_badge_raw"] = discount_from_badge

        result["discount_percent"] = None
        result["discount_source"] = None
        result["discount_percent_verified"] = False

        if result["old_price_verified"]:
            calc_discount = calculate_discount(price_current, price_original)
            if calc_discount:
                result["discount_percent"] = calc_discount
                result["discount_source"] = "calculated_verified"
                result["discount_percent_verified"] = True
                # Coherencia con el badge: si el badge difiere mucho del
                # calculado, marcamos la discrepancia (el adapter decide).
                if (
                    discount_from_badge
                    and abs(calc_discount - discount_from_badge) > 2
                ):
                    result["discount_badge_mismatch"] = True

        # Cupón adicional: informativo. NO se suma a un descuento no
        # verificado (no inventamos ofertas).
        coupon_discount = await _extract_coupon(page, pp)
        if coupon_discount:
            result["has_coupon"] = True
            result["coupon_discount"] = coupon_discount

        # Deal badge
        try:
            deal_el = await page.query_selector(pp.get("deal_badge", "#dealBadge"))
            if deal_el:
                result["has_deal"] = True
        except Exception:
            pass

        # Disponibilidad
        try:
            avail_el = await page.query_selector(pp.get("availability", "#availability span"))
            if avail_el:
                result["availability"] = (await avail_el.inner_text()).strip()
        except Exception:
            pass

        # Rating
        try:
            rating_el = await page.query_selector(pp.get("rating", "#acrPopover .a-icon-alt"))
            if rating_el:
                rating_text = await rating_el.get_attribute("title") or await rating_el.inner_text()
                rating_match = re.search(r'(\d+[.,]\d+)', rating_text or "")
                if rating_match:
                    result["rating"] = float(rating_match.group(1).replace(',', '.'))
        except Exception:
            pass

        # Reviews count
        try:
            rev_el = await page.query_selector(pp.get("reviews", "#acrCustomerReviewText"))
            if rev_el:
                rev_text = await rev_el.inner_text()
                rev_match = re.search(r'([\d,]+)', rev_text)
                if rev_match:
                    result["reviews"] = int(rev_match.group(1).replace(',', ''))
        except Exception:
            pass

        # Categoría
        try:
            crumbs = await page.query_selector_all(pp.get("category_breadcrumb", "#wayfinding-breadcrumbs_feature_div a"))
            if crumbs:
                result["category"] = (await crumbs[-1].inner_text()).strip()
        except Exception:
            pass

        # Snippet HTML para diagnóstico (zona de precios)
        try:
            price_zone = await page.query_selector("#apex_offerDisplay_desktop, #corePrice_desktop, #price, #priceblock_ourprice")
            if price_zone:
                result["raw_html_snippet"] = await price_zone.inner_html()
        except Exception:
            pass

        # Imagen principal del producto (extensión para `ofertas_hunter`).
        # Probamos varios selectores Amazon estándar.
        for img_sel in [
            "#landingImage",
            "#imgBlkFront",
            "#main-image",
            "#imageBlock img",
            "img[data-old-hires]",
            "img.a-dynamic-image",
        ]:
            try:
                img_el = await page.query_selector(img_sel)
                if not img_el:
                    continue
                src = (
                    await img_el.get_attribute("data-old-hires")
                    or await img_el.get_attribute("src")
                )
                if src and src.startswith("http") and ".jpg" in src.lower() or ".png" in (src or "").lower() or ".webp" in (src or "").lower() or "media-amazon.com" in (src or ""):
                    result["image_url"] = src
                    break
            except Exception:
                continue

        # Marca (brand) — Amazon expone una tabla o un link con id "bylineInfo".
        for brand_sel in [
            "#bylineInfo",
            "#bylineInfo_feature_div a",
            "tr.po-brand td.a-span9 span",
            "a#bylineInfo_feature_div",
        ]:
            try:
                brand_el = await page.query_selector(brand_sel)
                if not brand_el:
                    continue
                txt = (await brand_el.inner_text()).strip()
                if not txt:
                    continue
                # Limpieza: "Marca: Sony" / "Visita la Tienda Sony" / "De la marca Sony"
                txt = re.sub(
                    r"^(visita\s+la\s+tienda\s+|marca:\s*|de\s+la\s+marca\s+|brand:\s*)",
                    "",
                    txt,
                    flags=re.IGNORECASE,
                ).strip()
                if txt and len(txt) <= 60:
                    result["brand"] = txt
                    break
            except Exception:
                continue

    except Exception as e:
        logger.error(f"Error extrayendo datos de {url}: {e}")

    return result


async def _extract_current_price(page, pp: dict) -> Optional[float]:
    """Extrae precio actual con múltiples selectores de fallback."""
    try:
        html = await page.content()
        price, _source = extract_verified_current_price(html)
        if price is not None:
            return price
    except Exception:
        pass

    selectors_to_try = [
        pp.get("price_current", ""),
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#apex_offerDisplay_desktop .priceToPay .a-offscreen",
        "#corePrice_desktop .priceToPay .a-offscreen",
        ".priceToPay .a-offscreen",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
        "#corePrice_desktop .a-price .a-offscreen",
        "#price_inside_buybox",
        ".a-price[data-a-color='price'] .a-offscreen",
        "#price .a-offscreen",
        "#newBuyBoxPrice",
    ]
    for sel in selectors_to_try:
        if not sel:
            continue
        try:
            # Intentar con query_selector_all para tomar el primero visible
            els = await page.query_selector_all(sel)
            for el in els:
                text = await el.inner_text()
                price = parse_price_text(text)
                if price and price > 0:
                    return price
        except Exception:
            continue
    return None


async def _extract_original_price(page, pp: dict) -> Optional[float]:
    """Extrae precio original (tachado) con múltiples selectores."""
    selectors_to_try = [
        pp.get("price_original", ""),
        "#priceblock_was_price .a-offscreen",
        ".basisPrice .a-offscreen",
        "#apex_offerDisplay_desktop .basisPrice .a-offscreen",
        "#corePrice_desktop .basisPrice .a-offscreen",
        ".a-price.a-text-price .a-offscreen",
        "[data-a-strike='true'] .a-offscreen",
        "#listPrice",
        ".a-text-strike",
    ]
    for sel in selectors_to_try:
        if not sel:
            continue
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                text = await el.inner_text()
                price = parse_price_text(text)
                if price and price > 0:
                    return price
        except Exception:
            continue
    return None


async def _extract_discount_badge(page, pp: dict) -> Optional[int]:
    """Extrae porcentaje de descuento del badge visible."""
    selectors_to_try = [
        pp.get("discount_badge", ""),
        "#savingsPercentage",
        ".savingsPercentage",
        "#dealprice_savings .a-color-price",
        ".reinventPriceSavingsPercentageMargin",
        "[id*='savingsPercentage']",
        ".a-badge-text",
        "[class*='discount']",
        "[class*='saving']",
        "[class*='percent']",
    ]
    for sel in selectors_to_try:
        if not sel:
            continue
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                text = await el.inner_text()
                discount = extract_discount_from_text(text)
                if discount and discount >= 5:
                    return discount
        except Exception:
            continue
    return None


async def _extract_coupon(page, pp: dict) -> Optional[int]:
    """Extrae descuento adicional de cupón si existe."""
    selectors_to_try = [
        pp.get("coupon", ""),
        "#couponBadgeRegularVpc",
        ".couponBadge",
        "#vpcButton",
        "[id*='coupon']",
        "[class*='coupon']",
    ]
    for sel in selectors_to_try:
        if not sel:
            continue
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                discount = extract_discount_from_text(text)
                if discount:
                    return discount
                # Cupón de monto fijo — no lo sumamos al %
        except Exception:
            continue
    return None


async def extract_products_from_search(page, url: str) -> list:
    """
    Extrae lista de productos desde una página de búsqueda/listado de Amazon.
    Retorna lista de dicts con datos básicos y URL del producto.
    """
    selectors = load_selectors()
    sr = selectors.get("search_results", {})
    products = []

    try:
        containers = await page.query_selector_all(
            sr.get("product_container", "[data-component-type='s-search-result']")
        )

        for container in containers:
            try:
                product = await _extract_search_item(container, sr)
                if product:
                    products.append(product)
            except Exception as e:
                logger.debug(f"Error en item de búsqueda: {e}")
                continue

    except Exception as e:
        logger.error(f"Error extrayendo búsqueda de {url}: {e}")

    return products


async def _extract_search_item(container, sr: dict) -> Optional[dict]:
    """Extrae datos de un item en la página de búsqueda."""
    item = {
        "title": None,
        "url": None,
        "price_current": None,
        "price_original": None,
        "discount_percent": None,
        "asin": None,
        "rating": None,
        "reviews": None,
        "has_prime": False,
        "image_url": None,
    }

    # ASIN desde atributo data
    try:
        asin = await container.get_attribute("data-asin")
        if asin:
            item["asin"] = asin
    except Exception:
        pass

    # Título
    for sel in [
        "h2.a-size-base-plus span",
        "h2 a span",
        ".a-size-medium.a-color-base.a-text-normal",
        ".a-size-base-plus.a-color-base.a-text-normal",
        "h2 span",
    ]:
        try:
            el = await container.query_selector(sel)
            if el:
                t = (await el.inner_text()).strip()
                if t:
                    item["title"] = t
                    break
        except Exception:
            continue

    # Link
    for sel in ["h2 a.a-link-normal", "a.a-link-normal.s-line-clamp-4", "h2 a"]:
        try:
            link_el = await container.query_selector(sel)
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = "https://www.amazon.com.mx" + href
                    href = re.sub(r'\?.*', '', href)
                    item["url"] = href
                    break
        except Exception:
            continue

    if not item["url"]:
        return None

    # Precios — estrategia mejorada para Amazon MX 2025
    # Amazon MX puede mostrar los precios en cualquier orden.
    # Estrategia: tomar TODOS los precios del container y determinar cuál es actual vs original.
    try:
        all_price_els = await container.query_selector_all(".a-price .a-offscreen")
        all_prices = []
        for el in all_price_els:
            text = await el.inner_text()
            price = parse_price_text(text)
            if price and price > 0:
                # Verificar si el padre tiene clase a-text-price (precio tachado/original)
                parent_class = await el.evaluate("el => el.parentElement ? el.parentElement.className : ''")
                is_struck = "a-text-price" in parent_class
                all_prices.append((price, is_struck))

        if all_prices:
            struck_prices = [p for p, s in all_prices if s]
            normal_prices = [p for p, s in all_prices if not s]

            if struck_prices and normal_prices:
                # Precio actual = el más bajo de los normales
                # Precio original = el más alto de los tachados
                item["price_current"] = min(normal_prices)
                item["price_original"] = max(struck_prices)
            elif len(all_prices) >= 2:
                # Sin clasificación clara: el más bajo es el actual
                prices_only = [p for p, _ in all_prices]
                item["price_current"] = min(prices_only)
                item["price_original"] = max(prices_only)
            elif len(all_prices) == 1:
                item["price_current"] = all_prices[0][0]
    except Exception:
        pass

    # Badge de descuento — Amazon MX usa clases con hash como _c2Itd_discountText_
    try:
        badge_sels = [
            "[class*='discountText']",   # Selector más específico y confiable
            "[class*='discount']",
            ".a-badge-text",
            ".s-coupon-highlight-color",
            "[class*='savingsPercentage']",
            "[class*='saving']",
        ]
        for sel in badge_sels:
            badge_els = await container.query_selector_all(sel)
            for el in badge_els:
                text = await el.inner_text()
                discount = extract_discount_from_text(text)
                if discount and discount >= 5:
                    item["discount_percent"] = discount
                    break
            if item["discount_percent"]:
                break
    except Exception:
        pass

    # Calcular descuento si no hay badge
    if not item["discount_percent"] and item["price_current"] and item["price_original"]:
        item["discount_percent"] = calculate_discount(item["price_current"], item["price_original"])

    # Rating — Amazon MX muestra "4.3" como texto plano en .a-size-small
    try:
        rating_sels = [
            "span[aria-label*='estrellas']",
            ".a-size-small.a-color-base:not(.a-text-normal)",
            ".a-icon-alt",
        ]
        for sel in rating_sels:
            rating_el = await container.query_selector(sel)
            if rating_el:
                text = (await rating_el.get_attribute("aria-label") or
                        await rating_el.inner_text() or "")
                m = re.search(r'(\d+[.,]\d+)', text)
                if m:
                    item["rating"] = float(m.group(1).replace(',', '.'))
                    break
    except Exception:
        pass

    # Reviews
    try:
        rev_sels = [
            ".a-size-base.s-underline-text",
            "[aria-label*='calificaciones']",
            "[aria-label*='ratings']",
        ]
        for sel in rev_sels:
            rev_el = await container.query_selector(sel)
            if rev_el:
                text = (await rev_el.get_attribute("aria-label") or
                        await rev_el.inner_text() or "")
                m = re.search(r'([\d,]+)', text)
                if m:
                    item["reviews"] = int(m.group(1).replace(',', ''))
                    break
    except Exception:
        pass

    # Prime
    try:
        prime_el = await container.query_selector(".s-prime, [aria-label='Amazon Prime']")
        item["has_prime"] = prime_el is not None
    except Exception:
        pass

    # Imagen
    try:
        img_el = await container.query_selector(".s-image")
        if img_el:
            item["image_url"] = await img_el.get_attribute("src")
    except Exception:
        pass

    return item
