#!/usr/bin/env python3
"""
mcp_server.py — Servidor MCP del Amazon Offer Hunter.

Claude Sonnet 4.5 es el cerebro: genera sus propias URLs, decide qué explorar,
aprende qué funciona y navega Amazon MX de forma infinita y autónoma.
"""
import asyncio, json, sys, time, re, random
from pathlib import Path
from urllib.parse import urlparse, urlencode, urljoin

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE      = Path(__file__).parent
DATA_DIR  = BASE / "data";  DATA_DIR.mkdir(exist_ok=True)
OFERTS_DIR = BASE / "oferts"; OFERTS_DIR.mkdir(exist_ok=True)
OFFERS_F  = OFERTS_DIR / "oferts.json"          # ← ruta solicitada
MEMORY_F  = DATA_DIR / "memory.json"
VISITED_F = DATA_DIR / "visited_urls.json"
SETTINGS  = json.loads((BASE / "config" / "settings.json").read_text(encoding="utf-8"))
MIN_DISC  = SETTINGS.get("min_discount_percent", 50)

server = Server("amazon-offer-hunter")

# ── Browser singleton ─────────────────────────────────────────────────────────
_B = {"pw": None, "browser": None, "ctx": None, "page": None}

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36",
]

STEALTH = """() => {
    Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
    Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
    Object.defineProperty(navigator,'languages',{get:()=>['es-MX','es','en-US','en']});
    Object.defineProperty(navigator,'platform',{get:()=>'Win32'});
    Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
    window.chrome={runtime:{},loadTimes:function(){},csi:function(){}};
}"""

async def _page():
    from playwright.async_api import async_playwright
    if _B["page"] and not _B["page"].is_closed():
        return _B["page"]
    if not _B["pw"]:
        pw = await async_playwright().start()
        _B["pw"] = pw
        browser = await pw.chromium.launch(headless=True, args=[
            "--no-sandbox","--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage","--disable-extensions",
        ])
        _B["browser"] = browser
        ctx = await browser.new_context(
            user_agent=random.choice(UAS),
            viewport={"width": random.choice([1366,1440,1920]), "height": random.choice([768,900,1080])},
            locale="es-MX", timezone_id="America/Mexico_City",
            extra_http_headers={
                "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Fetch-Dest":"document","Sec-Fetch-Mode":"navigate",
                "Sec-Fetch-Site":"none","Sec-Fetch-User":"?1",
                "Upgrade-Insecure-Requests":"1",
            }
        )
        await ctx.add_init_script(STEALTH)
        _B["ctx"] = ctx
        page = await ctx.new_page()
        _B["page"] = page
        # Warmup silencioso
        try:
            await page.goto("https://www.amazon.com.mx", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(random.uniform(2,3))
        except Exception:
            pass
    return _B["page"]

# ── Memoria de aprendizaje ────────────────────────────────────────────────────

def _mem() -> dict:
    if MEMORY_F.exists():
        try: return json.loads(MEMORY_F.read_text(encoding="utf-8"))
        except: pass
    return {
        "sessions": 0, "pages_visited": 0, "offers_found": 0,
        "url_performance": {},   # url -> {visits, offers, efficiency}
        "category_scores": {},   # categoria -> score
        "learned_patterns": [],  # patrones URL que dan descuentos
        "visited_asins": [],
        "best_hours": [],
    }

def _save_mem(m: dict):
    MEMORY_F.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

def _visited() -> set:
    if VISITED_F.exists():
        try: return set(json.loads(VISITED_F.read_text(encoding="utf-8")))
        except: pass
    return set()

def _mark_visited(url: str):
    v = _visited()
    v.add(url)
    VISITED_F.write_text(json.dumps(list(v)[-5000:], ensure_ascii=False), encoding="utf-8")

def _offers() -> list:
    if OFFERS_F.exists():
        try: return json.loads(OFFERS_F.read_text(encoding="utf-8"))
        except: pass
    return []

def _save_offer(o: dict) -> bool:
    offers = _offers()
    seen = {x.get("asin") for x in offers if x.get("asin")}
    if o.get("asin") and o["asin"] not in seen:
        offers.append(o)
        OFFERS_F.write_text(json.dumps(offers, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    return False

# ── Helpers DOM ───────────────────────────────────────────────────────────────

def _price(text: str):
    if not text: return None
    c = re.sub(r'[^\d,.]','',text.strip())
    if not c: return None
    if ',' in c and '.' in c:
        c = c.replace('.','').replace(',','') if c.index('.') < c.index(',') else c.replace(',','')
    elif ',' in c:
        c = c.replace(',','.') if len(c.split(',')[-1])==2 else c.replace(',','')
    try: return float(c)
    except: return None

def _disc_text(text: str):
    if not text: return None
    m = re.search(r'(\d{1,3})\s*%', text)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= 99 else None
    return None

def _norm_url(url: str) -> str:
    m = re.search(r'/dp/([A-Z0-9]{10})', url)
    if m: return f"https://www.amazon.com.mx/dp/{m.group(1)}"
    return url.split("&ref=")[0].split("&qid=")[0]

def _is_captcha(url: str, content: str) -> bool:
    return ("validateCaptcha" in url or
            'name="amzn-captcha' in content or
            "Escribe los caracteres" in content or
            "Enter the characters" in content)

# ── HERRAMIENTAS MCP ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools():
    return [
        Tool(name="navigate_and_extract",
             description="""Navega cualquier URL de Amazon.com.mx y extrae TODA la información útil:
- En páginas de búsqueda/listado: lista de productos con precios, descuentos y URLs
- En páginas de producto (/dp/): precio actual, original, descuento, título, ASIN, rating
- En páginas de deals/ofertas: todos los deals disponibles
- También extrae links de navegación (categorías, subcategorías, páginas siguientes)
Retorna datos estructurados + links para seguir explorando.""",
             inputSchema={"type":"object","properties":{
                 "url":{"type":"string","description":"Cualquier URL de amazon.com.mx"},
                 "wait_seconds":{"type":"number","description":"Segundos a esperar (default 3, aumentar si hay 503)","default":3}
             },"required":["url"]}),

        Tool(name="generate_exploration_urls",
             description="""Genera nuevas URLs de Amazon.com.mx para explorar basándote en tu conocimiento.
Úsala para crear fuentes infinitas de búsqueda. Puedes generar URLs de:
- Búsquedas por keyword con filtros de descuento
- Categorías y subcategorías específicas
- Páginas de deals y ofertas del día
- Filtros por precio, marca, rating
- Páginas de liquidación y clearance
Retorna lista de URLs listas para usar con navigate_and_extract.""",
             inputSchema={"type":"object","properties":{
                 "strategy":{"type":"string","description":"Estrategia: 'keywords'|'categories'|'deals'|'brands'|'mixed'","default":"mixed"},
                 "focus":{"type":"string","description":"Enfoque opcional: 'electronica'|'hogar'|'ropa'|'juguetes'|etc"},
                 "count":{"type":"integer","description":"Cuántas URLs generar (default 10)","default":10}
             }}),

        Tool(name="save_offer",
             description="Guarda una oferta con descuento ≥50% en oferts/oferts.json Y la envía automáticamente a Telegram con foto. Llámala cada vez que confirmes un descuento real.",
             inputSchema={"type":"object","properties":{
                 "asin":{"type":"string"},"title":{"type":"string"},
                 "url":{"type":"string"},"price_current":{"type":"number"},
                 "price_original":{"type":"number"},"discount_percent":{"type":"integer"},
                 "category":{"type":"string"},"rating":{"type":"number"},
                 "reviews":{"type":"integer"},
                 "image_url":{"type":"string","description":"URL de la imagen del producto (extraída de la página)"},
                 "verdict":{"type":"string","description":"EXCELENTE|BUENA|REGULAR"},
                 "why":{"type":"string","description":"Por qué es buena oferta"}
             },"required":["asin","title","url","price_current","discount_percent"]}),

        Tool(name="record_url_performance",
             description="""Registra el rendimiento de una URL explorada para que el sistema aprenda.
Llámala después de explorar cada URL para que la IA mejore su estrategia con el tiempo.""",
             inputSchema={"type":"object","properties":{
                 "url":{"type":"string"},"offers_found":{"type":"integer"},
                 "products_seen":{"type":"integer"},
                 "category":{"type":"string","description":"Categoría principal de la página"},
                 "notes":{"type":"string","description":"Observaciones sobre esta fuente"}
             },"required":["url","offers_found","products_seen"]}),

        Tool(name="get_intelligence_report",
             description="""Retorna el reporte completo de inteligencia del sistema:
- Ofertas encontradas hasta ahora
- URLs con mejor rendimiento (más ofertas por visita)
- Categorías más productivas
- Patrones aprendidos
- Sugerencias para la próxima sesión""",
             inputSchema={"type":"object","properties":{
                 "include_offers":{"type":"boolean","default":True},
                 "include_stats":{"type":"boolean","default":True}
             }}),

        Tool(name="extract_links_from_current_page",
             description="""Extrae todos los links de navegación de la página actual del browser.
Útil para descubrir subcategorías, páginas siguientes, y nuevas secciones de Amazon
sin necesidad de navegar a una URL específica.""",
             inputSchema={"type":"object","properties":{
                 "filter":{"type":"string","description":"Filtrar links: 'products'|'categories'|'deals'|'all'","default":"all"}
             }}),
    ]

# ── Implementaciones ──────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "navigate_and_extract":
        return await _navigate_and_extract(arguments["url"], arguments.get("wait_seconds", 3))
    elif name == "generate_exploration_urls":
        return await _generate_urls(
            arguments.get("strategy","mixed"),
            arguments.get("focus",""),
            arguments.get("count",10)
        )
    elif name == "save_offer":
        return await _do_save_offer(arguments)
    elif name == "record_url_performance":
        return _record_perf(arguments)
    elif name == "get_intelligence_report":
        return _intelligence_report(arguments)
    elif name == "extract_links_from_current_page":
        return await _extract_links(arguments.get("filter","all"))
    return [TextContent(type="text", text=f"Herramienta desconocida: {name}")]


async def _navigate_and_extract(url: str, wait: float = 3):
    """Navega y extrae todo lo relevante de la página."""
    try:
        page = await _page()
        _mark_visited(url)

        resp = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        status = resp.status if resp else 0

        if status == 503:
            await asyncio.sleep(random.uniform(20, 35))
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            status = resp.status if resp else 0

        await asyncio.sleep(random.uniform(max(1, wait-1), wait+1))

        # Scroll humano
        for _ in range(random.randint(2,4)):
            await page.evaluate(f"window.scrollBy(0,{random.randint(250,500)})")
            await asyncio.sleep(random.uniform(0.2,0.5))

        content = await page.content()
        cur_url = page.url

        if _is_captcha(cur_url, content):
            return [TextContent(type="text", text=(
                "⚠️ CAPTCHA detectado.\n"
                "Acción: espera 45 segundos y reintenta la misma URL.\n"
                f"URL: {url}"
            ))]

        if status == 404:
            return [TextContent(type="text", text=f"404 — URL no encontrada: {url}")]

        # Detectar tipo de página
        is_product = bool(re.search(r'/dp/[A-Z0-9]{10}', cur_url))
        is_search  = "/s?" in cur_url or "/s/" in cur_url
        is_deals   = any(x in cur_url for x in ["goldbox","deals","ofertas","liquidacion"])
        is_category = "/b?" in cur_url or "/b/" in cur_url

        lines = [f"📍 URL: {cur_url}", f"   Tipo: {'PRODUCTO' if is_product else 'BÚSQUEDA/LISTADO' if is_search else 'DEALS' if is_deals else 'CATEGORÍA' if is_category else 'OTRA'}", ""]

        if is_product:
            lines += await _extract_product_data(page, cur_url)
        else:
            lines += await _extract_listing_data(page, cur_url)

        # Siempre incluir links de navegación útiles
        nav_links = await _get_nav_links(page)
        if nav_links:
            lines.append("")
            lines.append(f"🔗 Links para explorar ({len(nav_links)}):")
            for lnk in nav_links[:15]:
                lines.append(f"  {lnk}")

        # Actualizar memoria
        m = _mem()
        m["pages_visited"] = m.get("pages_visited", 0) + 1
        _save_mem(m)

        return [TextContent(type="text", text="\n".join(lines))]

    except Exception as e:
        return [TextContent(type="text", text=f"❌ Error navegando {url}: {e}")]


async def _extract_product_data(page, url: str) -> list:
    """Extrae datos completos de una página de producto."""
    lines = []
    try:
        asin = re.search(r'/dp/([A-Z0-9]{10})', url)
        asin = asin.group(1) if asin else None

        # Título
        title = None
        for sel in ["#productTitle","#title span","h1 span"]:
            el = await page.query_selector(sel)
            if el:
                title = (await el.inner_text()).strip()
                break

        # Precios con múltiples estrategias
        price_current = price_original = None
        for sel, is_orig in [
            ("#apex_offerDisplay_desktop .a-price .a-offscreen", False),
            ("#corePrice_desktop .a-price .a-offscreen", False),
            (".priceToPay .a-offscreen", False),
            ("#price_inside_buybox", False),
            ("#priceblock_ourprice", False),
            (".basisPrice .a-offscreen", True),
            ("#apex_offerDisplay_desktop .basisPrice .a-offscreen", True),
            (".a-price.a-text-price .a-offscreen", True),
        ]:
            els = await page.query_selector_all(sel)
            for el in els:
                v = _price(await el.inner_text())
                if v and v > 0:
                    if is_orig and not price_original: price_original = v
                    elif not is_orig and not price_current: price_current = v
            if price_current and price_original: break

        # Descuento
        discount = None
        for sel in ["#savingsPercentage",".savingsPercentage","[class*='discountText']","[class*='discount']"]:
            el = await page.query_selector(sel)
            if el:
                d = _disc_text(await el.inner_text())
                if d: discount = d; break
        if not discount and price_current and price_original and price_original > price_current:
            discount = int(round((price_original - price_current) / price_original * 100))

        # Rating y reseñas
        rating = reviews = None
        for sel in ["#acrPopover .a-icon-alt","#averageCustomerReviews .a-icon-alt"]:
            el = await page.query_selector(sel)
            if el:
                t = await el.get_attribute("title") or await el.inner_text()
                m = re.search(r'(\d+[.,]\d+)', t or "")
                if m: rating = float(m.group(1).replace(',','.')); break
        el = await page.query_selector("#acrCustomerReviewText")
        if el:
            m = re.search(r'([\d,]+)', await el.inner_text())
            if m: reviews = int(m.group(1).replace(',',''))

        # Categoría
        category = None
        crumbs = await page.query_selector_all("#wayfinding-breadcrumbs_feature_div a")
        if crumbs: category = (await crumbs[-1].inner_text()).strip()

        # Disponibilidad
        avail = "Disponible"
        el = await page.query_selector("#availability span")
        if el: avail = (await el.inner_text()).strip()

        # Imagen del producto (alta resolución)
        image_url = None
        for img_sel in ["#landingImage", "#imgBlkFront", "#main-image", ".a-dynamic-image"]:
            el = await page.query_selector(img_sel)
            if el:
                # Intentar data-old-hires primero (mayor resolución)
                hires = await el.get_attribute("data-old-hires")
                src   = await el.get_attribute("src")
                img   = hires if (hires and hires.startswith("http")) else src
                if img and img.startswith("http") and "images" in img:
                    image_url = img
                    break

        if discount and discount >= MIN_DISC:
            lines.append(f"🎯 ¡OFERTA {discount}% OFF — GUARDAR CON save_offer!")
            lines.append("")

        lines += [
            f"🛍️  {title or 'Sin título'}",
            f"   ASIN: {asin}",
            f"   Precio actual:   ${price_current:,.2f} MXN" if price_current else "   Precio actual: No detectado",
            f"   Precio original: ${price_original:,.2f} MXN" if price_original else "   Precio original: N/A",
            f"   Descuento: {discount}% OFF" if discount else "   Descuento: No detectado",
            f"   Rating: {rating}/5 ({reviews} reseñas)" if rating else "   Rating: N/A",
            f"   Categoría: {category or 'N/A'}",
            f"   Disponibilidad: {avail}",
            f"   Imagen: {image_url or 'No encontrada'}",
        ]

        # Recordatorio con todos los datos listos para save_offer
        if discount and discount >= MIN_DISC:
            lines.append("")
            lines.append("📋 Llama save_offer con estos datos:")
            lines.append(f"   asin=\"{asin}\"")
            lines.append(f"   title=\"{(title or '')[:80]}\"")
            lines.append(f"   url=\"https://www.amazon.com.mx/dp/{asin}\"")
            lines.append(f"   price_current={price_current}")
            lines.append(f"   price_original={price_original}")
            lines.append(f"   discount_percent={discount}")
            lines.append(f"   image_url=\"{image_url or ''}\"")
            lines.append(f"   category=\"{category or ''}\"")
            lines.append(f"   rating={rating}  reviews={reviews}")

        if not price_current:
            lines.append("   ⚠️ Precio no detectado — intenta con otro producto")

    except Exception as e:
        lines.append(f"Error extrayendo producto: {e}")
    return lines


async def _extract_listing_data(page, url: str) -> list:
    """Extrae productos de una página de listado/búsqueda."""
    lines = []
    try:
        containers = await page.query_selector_all("[data-component-type='s-search-result']")
        products = []

        for container in containers:
            try:
                asin = await container.get_attribute("data-asin")
                if not asin: continue

                title = None
                for sel in ["h2.a-size-base-plus span","h2 a span","h2 span"]:
                    el = await container.query_selector(sel)
                    if el:
                        t = (await el.inner_text()).strip()
                        if t: title = t; break

                url_prod = None
                link_el = await container.query_selector("h2 a.a-link-normal, a.a-link-normal.s-line-clamp-4")
                if link_el:
                    href = await link_el.get_attribute("href")
                    if href:
                        href = "https://www.amazon.com.mx" + href if href.startswith("/") else href
                        url_prod = _norm_url(href)

                # Precios
                pc = po = None
                all_p = []
                for el in await container.query_selector_all(".a-price .a-offscreen"):
                    v = _price(await el.inner_text())
                    if v and v > 0:
                        pc_cls = await el.evaluate("el => el.parentElement ? el.parentElement.className : ''")
                        all_p.append((v, "a-text-price" in pc_cls))
                if all_p:
                    struck = [p for p,s in all_p if s]
                    normal = [p for p,s in all_p if not s]
                    if struck and normal: pc = min(normal); po = max(struck)
                    elif len(all_p) >= 2: vals=[p for p,_ in all_p]; pc=min(vals); po=max(vals)
                    else: pc = all_p[0][0]

                disc = None
                if pc and po and po > pc:
                    disc = int(round((po - pc) / po * 100))

                if title and url_prod:
                    products.append({"asin":asin,"title":title[:80],"url":url_prod,
                                     "price_current":pc,"price_original":po,"discount_percent":disc})
            except Exception:
                continue

        over50 = [p for p in products if (p.get("discount_percent") or 0) >= MIN_DISC]
        with_disc = [p for p in products if p.get("discount_percent")]

        lines.append(f"📦 {len(products)} productos | Con descuento: {len(with_disc)} | ≥{MIN_DISC}%: {len(over50)}")

        if over50:
            lines.append(f"\n🎯 OFERTAS ≥{MIN_DISC}% (visitar con navigate_and_extract para confirmar):")
            for p in sorted(over50, key=lambda x: x["discount_percent"], reverse=True):
                lines.append(f"  • {p['discount_percent']}% OFF — {p['title'][:55]}")
                lines.append(f"    ${p['price_current']:,.0f} → ${p['price_original']:,.0f} MXN")
                lines.append(f"    {p['url']}")

        if with_disc and not over50:
            lines.append("\nMejores descuentos (< 50%):")
            for p in sorted(with_disc, key=lambda x: x["discount_percent"], reverse=True)[:5]:
                lines.append(f"  {p['discount_percent']}% — {p['title'][:55]} — ${p['price_current']:,.0f}")

        if not with_disc:
            lines.append("Sin descuentos detectados en listado. Los descuentos reales están en páginas de producto.")

        # URLs de productos con precio tachado para visitar
        to_visit = [p["url"] for p in products if p.get("price_original") and p.get("price_current")]
        if to_visit:
            lines.append(f"\n📋 {len(to_visit)} productos con precio tachado para verificar:")
            for u in to_visit[:12]:
                lines.append(f"  {u}")

    except Exception as e:
        lines.append(f"Error extrayendo listado: {e}")
    return lines


async def _get_nav_links(page) -> list:
    """Extrae links de navegación útiles de la página actual."""
    try:
        all_links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        useful = set()
        for link in all_links:
            if not link or "amazon.com.mx" not in link: continue
            if any(x in link for x in ["/ap/signin","/gp/cart","/gp/help","/gp/css",
                                         "javascript:","#","mailto:"]): continue
            norm = _norm_url(link)
            if norm and len(norm) > 30:
                useful.add(norm)
        # Priorizar: productos > búsquedas > categorías
        products = [u for u in useful if "/dp/" in u]
        searches = [u for u in useful if "/s?" in u or "/s/" in u]
        cats     = [u for u in useful if "/b?" in u or "/b/" in u]
        others   = [u for u in useful if u not in products+searches+cats]
        return (products[:5] + searches[:5] + cats[:3] + others[:2])[:15]
    except Exception:
        return []


async def _generate_urls(strategy: str, focus: str, count: int) -> list:
    """
    Genera URLs de Amazon MX para explorar.
    Claude conoce la estructura de Amazon — aquí se materializa ese conocimiento.
    """
    urls = []
    visited = _visited()
    m = _mem()
    best_cats = sorted(m.get("category_scores",{}).items(), key=lambda x:x[1], reverse=True)[:5]
    best_cat_names = [c[0] for c in best_cats]

    # ── Patrones de URL de Amazon MX que Claude conoce ────────────────────────

    # 1. Páginas de deals y ofertas directas
    deal_urls = [
        "https://www.amazon.com.mx/deals?deals-widget=%7B%22version%22%3A1%2C%22viewIndex%22%3A0%2C%22presetId%22%3A%22deals-collection-all-deals%22%7D",
        "https://www.amazon.com.mx/gp/goldbox?deals-widget=%7B%22version%22%3A1%2C%22viewIndex%22%3A0%7D",
        "https://www.amazon.com.mx/s?i=specialty-aps&bbn=12453009011&rh=n%3A12453009011%2Cp_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=oferta+del+dia&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=liquidacion&s=price-desc-rank",
        "https://www.amazon.com.mx/s?k=descuento+50+por+ciento",
        "https://www.amazon.com.mx/s?k=precio+especial&rh=p_n_deal_type%3A23566064011",
    ]

    # 2. Categorías con IDs de nodo de Amazon MX (conocimiento de Claude)
    category_urls = [
        # Electrónica
        "https://www.amazon.com.mx/s?i=electronics&rh=n%3A9669515011%2Cp_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?i=electronics&rh=n%3A9669515011&s=price-desc-rank",
        "https://www.amazon.com.mx/b?node=9669515011",
        # Computadoras
        "https://www.amazon.com.mx/s?i=computers&rh=n%3A9669515011%2Cp_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/b?node=13764951011",
        # Videojuegos
        "https://www.amazon.com.mx/s?i=videogames&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/b?node=9669516011",
        # Hogar y cocina
        "https://www.amazon.com.mx/s?i=kitchen&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/b?node=9669517011",
        # Herramientas
        "https://www.amazon.com.mx/s?i=tools&rh=p_n_deal_type%3A23566064011",
        # Ropa
        "https://www.amazon.com.mx/s?i=apparel&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/b?node=9669518011",
        # Deportes
        "https://www.amazon.com.mx/s?i=sporting-goods&rh=p_n_deal_type%3A23566064011",
        # Belleza
        "https://www.amazon.com.mx/s?i=beauty&rh=p_n_deal_type%3A23566064011",
        # Juguetes
        "https://www.amazon.com.mx/s?i=toys-and-games&rh=p_n_deal_type%3A23566064011",
        # Bebé
        "https://www.amazon.com.mx/s?i=baby-products&rh=p_n_deal_type%3A23566064011",
        # Mascotas
        "https://www.amazon.com.mx/s?i=pet-supplies&rh=p_n_deal_type%3A23566064011",
        # Automotriz
        "https://www.amazon.com.mx/s?i=automotive&rh=p_n_deal_type%3A23566064011",
        # Oficina
        "https://www.amazon.com.mx/s?i=office-products&rh=p_n_deal_type%3A23566064011",
        # Salud
        "https://www.amazon.com.mx/s?i=hpc&rh=p_n_deal_type%3A23566064011",
        # Libros
        "https://www.amazon.com.mx/s?i=stripbooks&rh=p_n_deal_type%3A23566064011",
        # Alimentos
        "https://www.amazon.com.mx/s?i=grocery&rh=p_n_deal_type%3A23566064011",
    ]

    # 3. Keywords de alta conversión con filtro de deals
    keyword_urls = [
        "https://www.amazon.com.mx/s?k=audifonos+bluetooth&rh=p_n_deal_type%3A23566064011&s=price-desc-rank",
        "https://www.amazon.com.mx/s?k=smart+tv+4k&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=laptop+gaming&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=tablet+android&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=aspiradora+robot&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=freidora+aire&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=cafetera+espresso&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=silla+ergonomica&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=monitor+curvo&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=camara+seguridad&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=bocina+portatil&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=reloj+smartwatch&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=impresora+laser&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=disco+ssd&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=memoria+ram&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=teclado+mecanico&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=mouse+gaming&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=microfono+streaming&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=camara+mirrorless&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=drone+camara&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=proyector+4k&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=consola+nintendo&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=juegos+ps5&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=perfume+original&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=crema+antiedad&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=proteina+whey&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=colchon+memory+foam&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=set+ollas+acero&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=taladro+inalambrico&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=mochila+viaje&rh=p_n_deal_type%3A23566064011",
    ]

    # 4. Marcas conocidas con descuentos frecuentes
    brand_urls = [
        "https://www.amazon.com.mx/s?k=sony+audifonos&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=samsung+tv&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=apple+accesorios&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=xiaomi+productos&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=logitech+gaming&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=asus+laptop&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=hp+impresora&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=philips+electrodomesticos&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=oster+cocina&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/s?k=ninja+blender&rh=p_n_deal_type%3A23566064011",
    ]

    # Seleccionar según estrategia
    pool = []
    if strategy == "deals":     pool = deal_urls + keyword_urls[:10]
    elif strategy == "categories": pool = category_urls + deal_urls[:3]
    elif strategy == "keywords":   pool = keyword_urls + deal_urls[:3]
    elif strategy == "brands":     pool = brand_urls + keyword_urls[:5]
    else:  # mixed
        pool = deal_urls[:3] + category_urls[:8] + keyword_urls[:10] + brand_urls[:5]

    # Filtrar por focus si se especificó
    if focus:
        focus_lower = focus.lower()
        focused = [u for u in pool if focus_lower in u.lower()]
        if focused: pool = focused + [u for u in pool if u not in focused]

    # Priorizar URLs no visitadas
    not_visited = [u for u in pool if u not in visited]
    already_visited = [u for u in pool if u in visited]

    # Mezclar: 80% no visitadas, 20% re-visitar las mejores
    perf = m.get("url_performance", {})
    best_revisit = sorted(
        [(u, perf[u]["efficiency"]) for u in already_visited if u in perf],
        key=lambda x: x[1], reverse=True
    )[:max(1, count//5)]

    selected = (not_visited + [u for u,_ in best_revisit])[:count]
    random.shuffle(selected)

    lines = [
        f"🌐 {len(selected)} URLs generadas (estrategia: {strategy})",
        f"   {len(not_visited)} nuevas | {len(best_revisit)} re-visitas de alto rendimiento",
        "",
    ]
    for u in selected:
        perf_info = ""
        if u in perf:
            e = perf[u]
            perf_info = f" [hist: {e.get('offers',0)} ofertas / {e.get('visits',0)} visitas]"
        lines.append(f"  {u}{perf_info}")

    lines.extend([
        "",
        "💡 Usa navigate_and_extract(url) con cada una.",
        "   Después de cada visita, llama record_url_performance() para aprender.",
    ])

    return [TextContent(type="text", text="\n".join(lines))]


async def _do_save_offer(args: dict) -> list:
    offer = {
        "asin": args.get("asin"),
        "title": args.get("title"),
        "url": args.get("url"),
        "price_current": args.get("price_current"),
        "price_original": args.get("price_original"),
        "discount_percent": args.get("discount_percent"),
        "category": args.get("category",""),
        "rating": args.get("rating"),
        "reviews": args.get("reviews"),
        "image_url": args.get("image_url",""),
        "found_at": time.time(),
        "source": "kiro-agent",
        "telegram_sent": False,
        "ai_evaluation": {
            "verdict": args.get("verdict","BUENA"),
            "reasoning": args.get("why",""),
        }
    }
    saved = _save_offer(offer)

    # Actualizar memoria
    m = _mem()
    lines = []

    if saved:
        m["offers_found"] = m.get("offers_found",0) + 1
        cat = args.get("category","")
        if cat:
            scores = m.setdefault("category_scores",{})
            scores[cat] = scores.get(cat,0) + 1
        _save_mem(m)

        lines.append(f"✅ OFERTA GUARDADA #{m['offers_found']}")
        lines.append(f"   {offer['title'][:60]}")
        lines.append(f"   {offer['discount_percent']}% OFF — ${offer['price_current']:,.0f} MXN")
        lines.append(f"   ASIN: {offer['asin']}")

        # Enviar a Telegram automáticamente
        try:
            from telegram_sender import send_offer_async, is_already_sent
            asin = offer.get("asin","")
            if asin and not is_already_sent(asin):
                result = await send_offer_async(offer)   # ← await directo, ya estamos en async
                if result.get("ok"):
                    lines.append(f"   📱 Telegram ✓ (método: {result['method']})")
                    _mark_offer_sent(asin)
                else:
                    lines.append(f"   ⚠️ Telegram error: {result.get('error','')}")
            else:
                lines.append("   📱 Ya enviado a Telegram anteriormente")
        except Exception as e:
            lines.append(f"   ⚠️ Error Telegram: {e}")
    else:
        lines.append(f"⚠️ ASIN {offer['asin']} ya estaba guardado")

    return [TextContent(type="text", text="\n".join(lines))]


def _mark_offer_sent(asin: str):
    """Marca una oferta como enviada en el JSON."""
    offers = _offers()
    for o in offers:
        if o.get("asin") == asin:
            o["telegram_sent"] = True
            break
    OFFERS_F.write_text(json.dumps(offers, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_perf(args: dict) -> list:
    m = _mem()
    url = args["url"]
    perf = m.setdefault("url_performance", {})
    if url not in perf:
        perf[url] = {"visits":0,"offers":0,"products":0,"efficiency":0.0,"category":"","notes":""}
    p = perf[url]
    p["visits"] += 1
    p["offers"] += args.get("offers_found",0)
    p["products"] += args.get("products_seen",0)
    p["efficiency"] = p["offers"] / max(p["visits"],1)
    if args.get("category"): p["category"] = args["category"]
    if args.get("notes"): p["notes"] = args["notes"]
    # Aprender patrones
    if args.get("offers_found",0) > 0:
        patterns = m.setdefault("learned_patterns",[])
        # Extraer patrón de la URL
        parsed = urlparse(url)
        pattern = f"{parsed.path}?{parsed.query[:50]}" if parsed.query else parsed.path
        if pattern not in patterns:
            patterns.append(pattern)
    _save_mem(m)
    return [TextContent(type="text", text=(
        f"📊 Rendimiento registrado: {url[:60]}\n"
        f"   Visitas: {p['visits']} | Ofertas: {p['offers']} | Eficiencia: {p['efficiency']:.2f}"
    ))]


def _intelligence_report(args: dict) -> list:
    m = _mem()
    offers = _offers()
    over50 = [o for o in offers if (o.get("discount_percent") or 0) >= MIN_DISC]
    over50.sort(key=lambda x: x.get("discount_percent",0), reverse=True)

    perf = m.get("url_performance",{})
    best_urls = sorted(perf.items(), key=lambda x: x[1].get("efficiency",0), reverse=True)[:5]
    best_cats = sorted(m.get("category_scores",{}).items(), key=lambda x:x[1], reverse=True)[:5]
    patterns = m.get("learned_patterns",[])

    lines = [
        "🧠 REPORTE DE INTELIGENCIA — Amazon Offer Hunter",
        "="*50,
        f"📈 Estadísticas globales:",
        f"   Sesiones: {m.get('sessions',0)}",
        f"   Páginas visitadas: {m.get('pages_visited',0)}",
        f"   Ofertas encontradas: {m.get('offers_found',0)} ({len(over50)} con ≥{MIN_DISC}%)",
        f"   URLs únicas exploradas: {len(perf)}",
        "",
    ]

    if best_urls:
        lines.append("🏆 URLs más productivas:")
        for url, data in best_urls:
            lines.append(f"   {data.get('efficiency',0):.2f} ofertas/visita — {url[:60]}")
        lines.append("")

    if best_cats:
        lines.append("📂 Categorías más productivas:")
        for cat, score in best_cats:
            lines.append(f"   {score} ofertas — {cat}")
        lines.append("")

    if patterns:
        lines.append(f"🔍 Patrones aprendidos ({len(patterns)}):")
        for pat in patterns[:5]:
            lines.append(f"   {pat}")
        lines.append("")

    if args.get("include_offers", True) and over50:
        lines.append(f"🎯 TOP OFERTAS ({len(over50)} total):")
        for o in over50[:10]:
            lines.append(f"   {o['discount_percent']}% OFF — {o.get('title','')[:50]}")
            lines.append(f"   ${o.get('price_current',0):,.0f} MXN | {o.get('url','')[:50]}")
        lines.append("")

    lines.extend([
        "💡 PRÓXIMOS PASOS SUGERIDOS:",
        "   1. generate_exploration_urls(strategy='deals') — URLs de deals activos",
        "   2. generate_exploration_urls(strategy='categories') — explorar categorías",
        f"   3. Enfocarse en: {best_cats[0][0] if best_cats else 'electrónica'} (mejor rendimiento)",
    ])

    return [TextContent(type="text", text="\n".join(lines))]


async def _extract_links(filter_type: str) -> list:
    """Extrae links de la página actual del browser."""
    try:
        page = await _page()
        all_links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        result = set()
        for link in all_links:
            if not link or "amazon.com.mx" not in link: continue
            if any(x in link for x in ["/ap/signin","/gp/cart","/gp/help","javascript:","#","mailto:"]): continue
            norm = _norm_url(link)
            if not norm or len(norm) < 30: continue
            if filter_type == "products" and "/dp/" not in norm: continue
            if filter_type == "categories" and "/b?" not in norm and "/b/" not in norm: continue
            if filter_type == "deals" and not any(x in norm for x in ["deals","goldbox","deal_type"]): continue
            result.add(norm)

        lines = [f"🔗 {len(result)} links extraídos (filtro: {filter_type}):"]
        for lnk in sorted(result)[:20]:
            lines.append(f"  {lnk}")
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return [TextContent(type="text", text=f"❌ Error: {e}")]


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
