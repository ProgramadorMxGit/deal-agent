"""
browser_worker.py — Navegador autónomo anti-detección para Amazon.com.mx.
Usa stealth mode, delays humanos y rotación de fingerprint para evitar captchas.
"""
import asyncio
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page

from .price_parser import extract_products_from_search, extract_product_data_from_page
from .dom_healer import DomHealer, DegradationMonitor
from .memory_store import MemoryStore
from .ai_evaluator import AIEvaluator

logger = logging.getLogger("browser_worker")
AMAZON_BASE = "https://www.amazon.com.mx"

# User agents Chrome recientes — rotar para evitar fingerprinting
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.128 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36 Edg/124.0.2478.109",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36",
]

# Viewports realistas
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
]

# URLs que Amazon protege más — evitar como seeds directos
HIGH_RISK_PATTERNS = [
    r'amazon\.com\.mx/deals$',
    r'amazon\.com\.mx/gp/goldbox',
    r'/errors/validateCaptcha',
    r'/ap/signin', r'/ap/register',
    r'/gp/help', r'/gp/cart', r'/gp/wishlist',
    r'/gp/css', r'/gp/your-account', r'/gp/orders',
    r'affiliate-program', r'associates',
    r'javascript:', r'#$',
]

# Script stealth — oculta señales de automatización
STEALTH_SCRIPT = """
() => {
    // Ocultar webdriver
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    // Plugins realistas
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [
                {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer'},
                {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                {name:'Native Client', filename:'internal-nacl-plugin'},
            ];
            arr.__proto__ = PluginArray.prototype;
            return arr;
        }
    });
    // Languages realistas
    Object.defineProperty(navigator, 'languages', {get: () => ['es-MX', 'es', 'en-US', 'en']});
    // Chrome runtime
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    // Permissions
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
        Promise.resolve({state: Notification.permission}) :
        originalQuery(parameters)
    );
    // Hardware concurrency realista
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    // DeviceMemory
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
    // Platform
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    // Vendor
    Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
}
"""


class BrowserWorker:
    def __init__(self, settings: dict, memory: MemoryStore, ai: AIEvaluator):
        self.settings = settings
        self.memory = memory
        self.ai = ai
        self.min_discount = settings.get("min_discount_percent", 50)
        self.headless = settings.get("headless", True)
        self.degradation_monitor = DegradationMonitor(
            threshold=settings.get("dom_heal_threshold", 0.4),
            window=settings.get("dom_heal_window", 20),
        )
        self.dom_healer = DomHealer(memory_store=memory)
        self.frontier: list = []
        self.visited: set = set()
        self.offers_found: list = []
        self.pages_visited: int = 0
        self._running = False
        self._captcha_count = 0
        self._consecutive_captchas = 0
        self._load_state()

    # ── Persistencia ─────────────────────────────────────────────────────────

    def _load_state(self):
        path = Path(self.settings.get("state_file", "data/state.json"))
        if path.exists():
            try:
                s = json.loads(path.read_text(encoding="utf-8"))
                self.frontier = s.get("frontier", [])
                self.visited = set(s.get("visited", []))
                logger.info(f"Estado: {len(self.frontier)} frontier, {len(self.visited)} visitadas")
            except Exception as e:
                logger.warning(f"Error cargando estado: {e}")

    def _save_state(self):
        path = Path(self.settings.get("state_file", "data/state.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps({
                "frontier": self.frontier[:500],
                "visited": list(self.visited)[-3000:],
                "pages_visited": self.pages_visited,
                "offers_found": len(self.offers_found),
                "last_updated": time.time(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando estado: {e}")

    def _save_offers(self):
        path = Path(self.settings.get("output_file", "data/offers.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = []
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            seen_asins = {o.get("asin") for o in existing if o.get("asin")}
            new = [o for o in self.offers_found if o.get("asin") not in seen_asins]
            all_offers = existing + new
            path.write_text(json.dumps(all_offers, ensure_ascii=False, indent=2), encoding="utf-8")
            if new:
                logger.info(f"💾 +{len(new)} ofertas nuevas ({len(all_offers)} total)")
        except Exception as e:
            logger.error(f"Error guardando ofertas: {e}")

    # ── URL helpers ───────────────────────────────────────────────────────────

    def _is_blocked(self, url: str) -> bool:
        for p in HIGH_RISK_PATTERNS:
            if re.search(p, url, re.IGNORECASE):
                return True
        return False

    def _is_amazon(self, url: str) -> bool:
        try:
            return "amazon.com.mx" in urlparse(url).netloc
        except Exception:
            return False

    def _normalize(self, url: str) -> str:
        try:
            m = re.search(r'/dp/([A-Z0-9]{10})', url)
            if m:
                return f"{AMAZON_BASE}/dp/{m.group(1)}"
            if "/s?" in url or "/s/" in url:
                return url.split("&ref=")[0].split("&qid=")[0]
            return url.split("?")[0] if "/dp/" not in url else url
        except Exception:
            return url

    def _seed_frontier(self, seeds: list):
        added = sum(1 for s in seeds
                    if s not in self.visited and s not in self.frontier
                    and not (self.frontier.append(s) or False))
        logger.info(f"🌱 +{added} seeds ({len(self.frontier)} en frontier)")

    # ── Browser setup anti-detección ─────────────────────────────────────────

    async def _setup_browser(self, playwright):
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)

        browser = await playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--start-maximized",
                f"--user-agent={ua}",
            ]
        )

        context = await browser.new_context(
            user_agent=ua,
            viewport=vp,
            locale="es-MX",
            timezone_id="America/Mexico_City",
            extra_http_headers={
                "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "max-age=0",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
        )

        # Inyectar stealth en cada nueva página
        await context.add_init_script(STEALTH_SCRIPT)
        return browser, context

    # ── Comportamiento humano ─────────────────────────────────────────────────

    async def _human_delay(self, min_s: float = 2.0, max_s: float = 5.0):
        """Delay aleatorio con distribución más humana (no uniforme)."""
        # Usar distribución normal truncada para parecer más humano
        base = random.gauss((min_s + max_s) / 2, (max_s - min_s) / 4)
        delay = max(min_s, min(max_s, base))
        await asyncio.sleep(delay)

    async def _human_scroll(self, page: Page):
        """Scroll humano: gradual, con pausas, a veces hacia arriba."""
        try:
            height = await page.evaluate("document.body.scrollHeight")
            current = 0
            while current < min(height, 3000):
                step = random.randint(200, 500)
                current += step
                await page.evaluate(f"window.scrollTo(0, {current})")
                await asyncio.sleep(random.uniform(0.1, 0.4))
            # Scroll parcial hacia arriba (comportamiento humano)
            if random.random() > 0.5:
                await page.evaluate(f"window.scrollTo(0, {random.randint(100, 400)})")
                await asyncio.sleep(random.uniform(0.2, 0.5))
        except Exception:
            pass

    async def _move_mouse_randomly(self, page: Page):
        """Mueve el mouse a posiciones aleatorias."""
        try:
            vp = page.viewport_size or {"width": 1366, "height": 768}
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, vp["width"] - 100)
                y = random.randint(100, vp["height"] - 100)
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.05, 0.2))
        except Exception:
            pass

    # ── Navegación y detección de captcha ────────────────────────────────────

    async def _navigate(self, page: Page, url: str, timeout: int = 30000) -> bool:
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if resp:
                if resp.status == 200:
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                    return True
                elif resp.status in [301, 302]:
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    return True
                elif resp.status == 503:
                    # Rate limiting — esperar y reintentar una vez
                    wait = random.uniform(15, 30)
                    logger.warning(f"503 en {url[:50]} — esperando {wait:.0f}s")
                    await asyncio.sleep(wait)
                    # Reintentar
                    try:
                        resp2 = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                        if resp2 and resp2.status == 200:
                            await asyncio.sleep(random.uniform(1, 2))
                            return True
                    except Exception:
                        pass
                    return False
                elif resp.status == 404:
                    return False
            return resp is not None
        except Exception as e:
            logger.debug(f"Error navegando {url[:50]}: {e}")
            return False

    def _is_captcha_page(self, url: str, content: str) -> bool:
        """Detecta si la página actual es un captcha de Amazon."""
        captcha_signals = [
            "validateCaptcha" in url,
            "robot" in url.lower(),
            "Enter the characters you see" in content,
            "Escribe los caracteres que ves" in content,
            "Type the characters you see" in content,
            'name="amzn-captcha' in content,
            'id="captchacharacters"' in content,
            "api.prod.captcha.us-east-1.amazonaws.com" in content,
        ]
        return any(captcha_signals)

    async def _handle_captcha(self, page: Page) -> bool:
        """
        Detecta captcha. Si lo hay, espera con backoff exponencial
        y recarga la página. Retorna True si había captcha.
        """
        try:
            url = page.url
            content = await page.content()
            if not self._is_captcha_page(url, content):
                return False

            self._captcha_count += 1
            self._consecutive_captchas += 1

            # Backoff exponencial: 30s, 60s, 120s, 240s...
            wait = min(30 * (2 ** (self._consecutive_captchas - 1)), 300)
            logger.warning(f"🤖 CAPTCHA #{self._captcha_count} — esperando {wait}s antes de continuar")

            # Si hay demasiados captchas seguidos, rotar user agent
            if self._consecutive_captchas >= 3:
                logger.warning("⚠️  Demasiados captchas — la sesión puede estar bloqueada")
                logger.warning("   Tip: ejecuta con --no-headless para ver el browser")

            await asyncio.sleep(wait)
            return True

        except Exception:
            return False

    async def _warmup(self, page: Page):
        """
        Calienta el browser visitando Amazon homepage primero.
        Esto establece cookies y hace que el browser parezca legítimo.
        """
        try:
            logger.info("🔥 Calentando browser (visitando homepage)...")
            await page.goto(AMAZON_BASE, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(random.uniform(2, 4))
            await self._human_scroll(page)
            await self._move_mouse_randomly(page)
            await asyncio.sleep(random.uniform(1, 2))
            logger.info("   Browser calentado ✓")
        except Exception as e:
            logger.debug(f"Warmup error: {e}")

    # ── Procesamiento de páginas ──────────────────────────────────────────────

    async def _process_search_page(self, page: Page, url: str) -> tuple:
        good_offers, new_links = [], []
        try:
            await self._human_scroll(page)
            await asyncio.sleep(random.uniform(0.3, 0.8))

            products = await extract_products_from_search(page, url)
            logger.info(f"  📦 {len(products)} productos | {url[:55]}...")
            self.degradation_monitor.record(len(products) > 0)

            for p in products:
                disc = p.get("discount_percent")
                asin = p.get("asin")
                prod_url = p.get("url")

                if not prod_url or not asin:
                    continue

                if disc and disc >= self.min_discount:
                    if not self.memory.is_asin_seen(asin):
                        good_offers.append(p)
                        logger.info(f"  🎯 {disc}% OFF (búsqueda) — {p.get('title','')[:45]}")
                elif p.get("price_original") and p.get("price_current"):
                    # Tiene precio tachado — puede tener más descuento en la página de producto
                    # Agregar al frontier para verificar
                    if not self.memory.is_asin_seen(asin):
                        new_links.append(prod_url)

            # Extraer links de navegación (páginas siguientes, categorías)
            try:
                all_links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                for link in all_links:
                    if not link or not self._is_amazon(link) or self._is_blocked(link):
                        continue
                    norm = self._normalize(link)
                    if norm not in self.visited and norm not in self.frontier:
                        if "/dp/" not in norm:  # Solo links de navegación, no productos
                            new_links.append(norm)
            except Exception:
                pass

            # Página siguiente al frente
            next_pg = await self._get_next_page(page)
            if next_pg and next_pg not in self.visited:
                new_links.insert(0, next_pg)

        except Exception as e:
            logger.error(f"Error en búsqueda {url[:50]}: {e}")
            self.degradation_monitor.record(False)
        return good_offers, new_links

    async def _get_next_page(self, page: Page) -> Optional[str]:
        for sel in [
            ".s-pagination-next",
            "a[aria-label='Ir a la página siguiente']",
            "a[aria-label='Go to next page']",
            ".a-pagination .a-last a",
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    href = await el.get_attribute("href")
                    if href:
                        return AMAZON_BASE + href if href.startswith("/") else href
            except Exception:
                continue
        return None

    async def _process_product_page(self, page: Page, url: str) -> Optional[dict]:
        try:
            product = await extract_product_data_from_page(page, url)
            has_price = product.get("price_current") is not None
            self.degradation_monitor.record(has_price)

            if not has_price and product.get("raw_html_snippet"):
                ai_prices = self.ai.analyze_html_for_prices(product["raw_html_snippet"])
                if ai_prices:
                    product.update({k: v for k, v in ai_prices.items() if v is not None})
                    logger.info(f"  🤖 IA extrajo precios para {product.get('asin')}")

            disc = product.get("discount_percent")
            if disc and disc >= self.min_discount:
                return product
        except Exception as e:
            logger.error(f"Error en producto {url[:50]}: {e}")
            self.degradation_monitor.record(False)
        return None

    async def _enrich_with_ai(self, products: list) -> list:
        if not self.settings.get("ai_eval_enabled", True) or not products:
            return products
        enriched = []
        try:
            if len(products) >= 3:
                evals = self.ai.evaluate_batch(products)
                by_asin = {e.get("asin"): e for e in evals}
                for p in products:
                    ev = by_asin.get(p.get("asin", ""))
                    if ev:
                        p["ai_evaluation"] = ev
                        if ev.get("verdict") != "DESCARTAR":
                            enriched.append(p)
                    else:
                        enriched.append(p)
            else:
                for p in products:
                    ev = self.ai.evaluate_offer(p)
                    p["ai_evaluation"] = ev
                    if ev.get("verdict") != "DESCARTAR":
                        enriched.append(p)
                    else:
                        logger.info(f"  ❌ IA descartó: {p.get('title','')[:45]}")
        except Exception as e:
            logger.warning(f"Error en enriquecimiento IA: {e}")
            return products
        return enriched

    # ── Sesión principal ──────────────────────────────────────────────────────

    async def run_session(self, seeds: list, max_offers: int = 100) -> list:
        session_id = self.memory.start_session()
        self._running = True
        session_offers = []
        session_pages = 0
        self._consecutive_captchas = 0

        logger.info(f"🚀 Sesión {session_id} | min_discount={self.min_discount}% | seeds={len(seeds)}")
        self._seed_frontier(seeds)

        async with async_playwright() as pw:
            browser, context = await self._setup_browser(pw)
            page = await context.new_page()

            # Warmup: visitar homepage primero para establecer cookies
            await self._warmup(page)

            try:
                while self.frontier and len(session_offers) < max_offers and self._running:
                    url = self.frontier.pop(0)

                    if url in self.visited or self.memory.is_url_visited(url):
                        continue

                    self.visited.add(url)
                    self.memory.mark_url_visited(url)
                    session_pages += 1
                    self.pages_visited += 1

                    logger.info(f"\n🌐 [{session_pages}] {url[:75]}")

                    ok = await self._navigate(page, url)
                    if not ok:
                        continue

                    # Detectar captcha
                    if await self._handle_captcha(page):
                        # Reintentar la misma URL después del wait
                        self.frontier.insert(0, url)
                        self.visited.discard(url)
                        # Si hay demasiados captchas, pausar más
                        if self._consecutive_captchas >= 5:
                            logger.warning("🛑 Demasiados captchas — pausa larga de 5 minutos")
                            await asyncio.sleep(300)
                            self._consecutive_captchas = 0
                        continue

                    # Captcha resuelto o no había
                    self._consecutive_captchas = 0

                    cur = page.url
                    is_product = "/dp/" in cur or "/gp/product/" in cur
                    is_search  = "/s?" in cur or "/s/" in cur
                    is_deals   = "goldbox" in cur or "/deals" in cur

                    if is_product:
                        prod = await self._process_product_page(page, cur)
                        if prod:
                            prod["found_at"] = time.time()
                            prod["source_url"] = url
                            session_offers.append(prod)
                            self.memory.mark_asin_seen(prod.get("asin", ""))
                            logger.info(
                                f"  ✅ OFERTA: {prod.get('title','')[:55]} "
                                f"— {prod.get('discount_percent')}% OFF "
                                f"— ${prod.get('price_current'):,.0f} MXN"
                            )

                    elif is_search or is_deals:
                        offers, new_links = await self._process_search_page(page, cur)
                        # Productos con descuento van al FRENTE del frontier (alta prioridad)
                        product_links = []
                        other_links = []
                        for link in new_links[:25]:
                            if link not in self.visited and link not in self.frontier:
                                if "/dp/" in link:
                                    product_links.append(link)
                                else:
                                    other_links.append(link)
                        # Insertar productos al frente, búsquedas al final
                        self.frontier = product_links + self.frontier + other_links

                        for offer in offers:
                            if offer.get("asin") and not self.memory.is_asin_seen(offer["asin"]):
                                if offer.get("url") and offer["url"] not in self.frontier:
                                    self.frontier.insert(0, offer["url"])

                    # DomHealer si hay degradación
                    if self.degradation_monitor.is_degraded() and self.dom_healer.can_heal():
                        ctx = "product" if is_product else "search"
                        logger.warning(f"🔧 DomHealer activado ({ctx})")
                        if await self.dom_healer.heal(page, ctx):
                            self.degradation_monitor.reset()

                    # Re-sembrar si frontier bajo
                    if len(self.frontier) < self.settings.get("reseed_threshold", 10):
                        best = self.memory.get_best_seeds(10)
                        self._seed_frontier(best if best else seeds[:10])

                    # Guardar cada 10 páginas
                    if session_pages % 10 == 0:
                        self._save_state()
                        self._save_offers()
                        self.memory.save()

                    # Delay humano entre páginas
                    delay_cfg = self.settings.get("delay_between_pages_ms", [2000, 5000])
                    await self._human_delay(delay_cfg[0] / 1000, delay_cfg[1] / 1000)

                    # Movimiento de mouse ocasional
                    if random.random() > 0.7:
                        await self._move_mouse_randomly(page)

            except KeyboardInterrupt:
                logger.info("⏹️  Interrumpido por usuario")
            except Exception as e:
                logger.error(f"Error en sesión: {e}", exc_info=True)
            finally:
                await context.close()
                await browser.close()

        # Enriquecer con IA
        if session_offers:
            logger.info(f"\n🤖 Evaluando {len(session_offers)} ofertas con IA...")
            session_offers = await self._enrich_with_ai(session_offers)

        self.offers_found.extend(session_offers)
        self._save_state()
        self._save_offers()

        for seed in seeds:
            self.memory.record_seed_result(seed, len(session_offers), session_pages)
        self.memory.end_session(session_id, len(session_offers), session_pages)
        self.memory.save()

        logger.info(f"\n✅ Sesión: {session_pages} páginas | {len(session_offers)} ofertas | {self._captcha_count} captchas")
        return session_offers
