"""
price_parser.py — Extrae y calcula descuentos del DOM de Amazon.com.mx
Auto-sanado por DomHealer cuando los selectores fallan.
"""
import re
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("price_parser")

SELECTORS_PATH = Path(__file__).parent.parent / "config" / "selectors.json"


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

        # Precio original
        price_original = await _extract_original_price(page, pp)
        result["price_original"] = price_original

        # Descuento desde badge
        discount_from_badge = await _extract_discount_badge(page, pp)
        if discount_from_badge:
            result["discount_percent"] = discount_from_badge
            result["discount_source"] = "badge"

        # Descuento calculado (más confiable si tenemos ambos precios)
        if price_current and price_original:
            calc_discount = calculate_discount(price_current, price_original)
            if calc_discount:
                if not result["discount_percent"] or abs(calc_discount - result["discount_percent"]) < 5:
                    result["discount_percent"] = calc_discount
                    result["discount_source"] = "calculated"

        # Cupón adicional
        coupon_discount = await _extract_coupon(page, pp)
        if coupon_discount:
            result["has_coupon"] = True
            # Sumar cupón al descuento total si aplica
            if result["discount_percent"]:
                result["discount_percent"] = min(99, result["discount_percent"] + coupon_discount)
            else:
                result["discount_percent"] = coupon_discount
            result["discount_source"] = "coupon+badge" if result["discount_source"] else "coupon"

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

    except Exception as e:
        logger.error(f"Error extrayendo datos de {url}: {e}")

    return result


async def _extract_current_price(page, pp: dict) -> Optional[float]:
    """Extrae precio actual con múltiples selectores de fallback."""
    selectors_to_try = [
        pp.get("price_current", ""),
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
        "#corePrice_desktop .a-price .a-offscreen",
        "#price_inside_buybox",
        ".a-price[data-a-color='price'] .a-offscreen",
        "#price .a-offscreen",
        ".priceToPay .a-offscreen",
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
