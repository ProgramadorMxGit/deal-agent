"""Test E2E real: usa el listing extractor del bot para descubrir
productos en bestsellers/deals, y procesa los que tengan descuento alto.
"""

from __future__ import annotations

import asyncio
import sys

from ofertas_hunter.browser.browser_context import BrowserConfig
from ofertas_hunter.browser.playwright_worker import PlaywrightBrowserWorker


# URLs de listing que el bot sabe parsear via su listing_extractor
SEEDS = [
    "https://www.amazon.com.mx/gp/goldbox/",
    "https://www.amazon.com.mx/-/es/deals",
    "https://www.amazon.com.mx/gp/bestsellers/home/",
]


async def main() -> int:
    config = BrowserConfig(
        headless=True,
        user_data_dir="secrets/browser_profiles/amazon",
        warmup_amazon_homepage=True,
        delay_between_requests_ms=(8000, 15000),
    )

    from ofertas_hunter.exploration.listing_extractor import (
        extract_amazon_deals,
        extract_amazon_listing,
    )

    productos: list = []
    async with PlaywrightBrowserWorker(config) as worker:
        for seed in SEEDS:
            print(f"\n>>> Fetch {seed}")
            page = await worker.fetch(seed)
            print(f"  status: {page.status}  bytes: {len(page.html or '')}  blocked: {page.blocked}")
            if not page.html or page.blocked:
                continue
            try:
                if "/deals" in seed or "/goldbox" in seed:
                    items = extract_amazon_deals(page.html, seed)
                else:
                    items = extract_amazon_listing(page.html, seed)
            except Exception as exc:
                print(f"  listing_extractor failed: {exc}")
                continue
            print(f"  items extraídos: {len(items)}")
            for it in items[:5]:
                print(f"    {it}")
            productos.extend([
                {"url": it.url, "kind": it.kind} for it in items if "/dp/" in it.url
            ])
            if productos:
                break

    if not productos:
        print("\n(ningún listing produjo URLs procesables — fallback a seeds.json)")
        # Fallback: tomar las primeras 3 URLs de seeds.json
        import json
        seeds_path = "config/seeds/amazon.json"
        urls = json.loads(open(seeds_path, encoding="utf-8").read())
        productos = [{"url": u, "kind": "product"} for u in urls if "/dp/" in u][:3]
        print(f"  cargados {len(productos)} desde {seeds_path}")

    # Procesar los primeros 3 productos
    productos = [p for p in productos if "/dp/" in p.get("url", "")][:3]
    if not productos:
        print("ningún URL de producto disponible")
        return 1

    print(f"\n>>> Procesando {len(productos)} productos con AmazonHunterAgent")
    from ofertas_hunter.agents.amazon_hunter_agent import AmazonHunterAgent
    from ofertas_hunter.db import connect, init_db

    init_db()
    conn = connect()
    try:
        async with PlaywrightBrowserWorker(config) as worker:
            agent = AmazonHunterAgent(browser=worker, db_conn=conn)
            urls = [p["url"] for p in productos]
            outcomes = await agent.hunt_urls(urls)
            for o in outcomes:
                print(f"\n--- {o.url}")
                print(f"  classification:    {o.classification}")
                print(f"  outbox_type:       {o.suggested_outbox_type}")
                print(f"  outbox_id:         {o.enqueued_outbox_id}")
                print(f"  discarded_reason:  {o.discarded_reason}")
                if o.extracted:
                    p = o.extracted
                    print(f"  title:             {p.title and p.title[:80]}")
                    print(f"  current_price:     {p.current_price}")
                    print(f"  previous_price:    {p.previous_price}")
                    print(f"  discount_percent:  {p.discount_percent}")
                    print(f"  is_publishable:    {p.is_publishable}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
