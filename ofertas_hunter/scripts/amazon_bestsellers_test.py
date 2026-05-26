"""Prueba real: descarga bestsellers Amazon, extrae productos con -72%
o más descuento, y los procesa con el AmazonHunterAgent.
"""

from __future__ import annotations

import asyncio
import re
import sys

from ofertas_hunter.browser.browser_context import BrowserConfig
from ofertas_hunter.browser.playwright_worker import PlaywrightBrowserWorker


URL = "https://www.amazon.com.mx/gp/bestsellers/home/"


def find_high_discount_products(html: str, *, min_discount: int = 50) -> list[dict]:
    """Extrae todos los productos en la página con descuento >= min_discount."""
    out: list[dict] = []
    # Buscamos patrones tipo "-72%" cerca de un precio. Y luego retrocedemos
    # para encontrar el ASIN del producto. Patrón Amazon bestsellers:
    # - cada card tiene `data-asin="ASIN"` o un link `/dp/ASIN/`.
    # - el porcentaje aparece como "-NN%" en un span.
    discount_re = re.compile(r"-(\d{2,3})%")
    asin_re = re.compile(r"/dp/([A-Z0-9]{10})")
    for m in discount_re.finditer(html):
        try:
            disc = int(m.group(1))
        except ValueError:
            continue
        if disc < min_discount:
            continue
        # Buscamos el ASIN más cercano antes y después.
        start = max(0, m.start() - 4000)
        end = min(len(html), m.end() + 4000)
        chunk = html[start:end]
        asin_match = asin_re.search(chunk)
        if not asin_match:
            continue
        asin = asin_match.group(1)
        # Título: aproximación, primer elemento "<span ...>...</span>" cercano.
        title_match = re.search(
            r'<span[^>]*class="[^"]*p13n-sc-truncate[^"]*"[^>]*>([^<]+)</span>'
            r'|<span[^>]*class="[^"]*a-text-bold[^"]*"[^>]*>([^<]+)</span>'
            r'|<a[^>]+href="/[^"]*/dp/[^"]+"[^>]*>\s*<span[^>]*>([^<]+)</span>',
            chunk,
        )
        title = ""
        if title_match:
            title = next((g for g in title_match.groups() if g), "").strip()
        item = {
            "asin": asin,
            "discount_percent": disc,
            "title": title[:80],
            "url": f"https://www.amazon.com.mx/dp/{asin}",
        }
        # Evitar duplicados por ASIN
        if not any(o["asin"] == asin for o in out):
            out.append(item)
    return out


async def main() -> int:
    config = BrowserConfig(
        headless=True,
        user_data_dir="secrets/browser_profiles/amazon",
        warmup_amazon_homepage=True,
        delay_between_requests_ms=(8000, 20000),
    )

    print(f"\n>>> Fetch {URL}")
    async with PlaywrightBrowserWorker(config) as worker:
        page = await worker.fetch(URL)

    print(f"  status: {page.status}")
    print(f"  bytes:  {len(page.html or '')}")
    print(f"  blocked: {page.blocked}")
    if page.error:
        print(f"  error: {page.error}")

    if not page.html or page.blocked:
        print("\n❌ Fetch falló — no se puede continuar")
        return 1

    productos = find_high_discount_products(page.html, min_discount=50)
    print(f"\n>>> Productos con descuento >=50%: {len(productos)}")
    for p in productos[:20]:
        print(
            f"  -{p['discount_percent']}%  ASIN={p['asin']}  "
            f"{(p['title'] or '(sin título extraíble)')[:60]}"
        )
    if not productos:
        print("\n(no se detectó ningún -50% o más en la página)")
        return 0

    # Tomamos el de descuento más alto.
    top = max(productos, key=lambda p: p["discount_percent"])
    print(f"\n>>> Top descuento: -{top['discount_percent']}% → {top['url']}")
    print("\n>>> Procesando con AmazonHunterAgent...")

    from ofertas_hunter.agents.amazon_hunter_agent import AmazonHunterAgent
    from ofertas_hunter.db import connect, init_db

    init_db()
    conn = connect()
    try:
        async with PlaywrightBrowserWorker(config) as worker:
            agent = AmazonHunterAgent(browser=worker, db_conn=conn)
            outcomes = await agent.hunt_urls([top["url"]])
            for o in outcomes:
                print(f"\nResultado:")
                print(f"  classification:    {o.classification}")
                print(f"  outbox_type:       {o.suggested_outbox_type}")
                print(f"  outbox_id:         {o.enqueued_outbox_id}")
                print(f"  discarded_reason:  {o.discarded_reason}")
                if o.extracted:
                    p = o.extracted
                    print(f"  title:             {p.title}")
                    print(f"  current_price:     {p.current_price}")
                    print(f"  previous_price:    {p.previous_price}")
                    print(f"  discount_percent:  {p.discount_percent}")
                    print(f"  image_url:         {p.image_url and p.image_url[:80]}")
                    print(f"  is_publishable:    {p.is_publishable}")
                    print(f"  reasons:           {p.not_publishable_reasons}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
