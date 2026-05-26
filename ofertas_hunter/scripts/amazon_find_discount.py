"""Busca productos con descuento >=50% navegando bestsellers + recommendations.

Usa el listing extractor del bot para localizar deals reales.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from ofertas_hunter.browser.browser_context import BrowserConfig
from ofertas_hunter.browser.playwright_worker import PlaywrightBrowserWorker


SEED_LISTINGS = [
    "https://www.amazon.com.mx/gp/bestsellers/home/",
    "https://www.amazon.com.mx/gp/goldbox/",
    "https://www.amazon.com.mx/-/es/deals",
    "https://www.amazon.com.mx/s?k=ofertas&rh=p_n_pct-off-with-tax%3A50-",
]


def extract_dp_with_discount(html: str, *, min_discount: int = 50) -> list[dict]:
    """Encuentra productos con savings (-NN%) y su ASIN cercano."""
    found: list[dict] = []
    seen_asins: set[str] = set()
    # Cada card típico tiene structure: ... data-asin="ASIN" ... -NN% ...
    # Usamos un regex que capture el ASIN de un bloque y luego busque -%
    # dentro de los siguientes 3000 chars (aproximación).
    pattern = re.compile(
        r'data-asin="([A-Z0-9]{10})"',
        re.IGNORECASE,
    )
    discount_re = re.compile(r"-(\d{2,3})\s*%")
    for m in pattern.finditer(html):
        asin = m.group(1)
        if asin in seen_asins:
            continue
        chunk = html[m.start() : min(len(html), m.end() + 5000)]
        d = discount_re.search(chunk)
        if not d:
            continue
        try:
            disc = int(d.group(1))
        except ValueError:
            continue
        if disc < min_discount:
            continue
        seen_asins.add(asin)
        # Título: aproximación
        t = re.search(r'aria-label="([^"]+)"', chunk)
        if not t:
            t = re.search(r'<span[^>]*>([^<]{15,120})</span>', chunk)
        title = (t.group(1).strip() if t else "")[:80]
        found.append(
            {
                "asin": asin,
                "discount_percent": disc,
                "title": title,
                "url": f"https://www.amazon.com.mx/dp/{asin}",
            }
        )
    return found


async def main() -> int:
    config = BrowserConfig(
        headless=True,
        user_data_dir="secrets/browser_profiles/amazon",
        warmup_amazon_homepage=True,
        delay_between_requests_ms=(8000, 15000),
    )

    candidatos: list[dict] = []
    async with PlaywrightBrowserWorker(config) as worker:
        for seed in SEED_LISTINGS:
            print(f"\n>>> Fetch {seed}")
            page = await worker.fetch(seed)
            print(f"  status: {page.status} bytes: {len(page.html or '')}")
            if not page.html or page.blocked:
                print("  (skip)")
                continue
            found = extract_dp_with_discount(page.html, min_discount=50)
            print(f"  productos con -50% encontrados: {len(found)}")
            for p in found[:10]:
                print(f"    -{p['discount_percent']:>3}%  {p['asin']}  {p['title'][:60]}")
            candidatos.extend(found)
            # Salir tan pronto encontremos al menos 1
            if candidatos:
                break

    if not candidatos:
        print("\n(ningún listing trajo productos con descuento -50% en este momento)")
        return 0

    # Tomar uno con descuento alto
    candidatos.sort(key=lambda p: -p["discount_percent"])
    top = candidatos[0]
    print(f"\n>>> Procesando con AmazonHunterAgent: {top['url']} -{top['discount_percent']}%")

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
                    print(f"  image_url:         {(p.image_url or '')[:80]}")
                    print(f"  in_stock:          {p.in_stock}")
                    print(f"  is_publishable:    {p.is_publishable}")
                    print(f"  reasons:           {p.not_publishable_reasons}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
