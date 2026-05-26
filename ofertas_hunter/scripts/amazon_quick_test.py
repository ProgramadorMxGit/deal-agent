"""Prueba rápida: fetch directo de una URL Amazon y dump del HTML."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from ofertas_hunter.browser.browser_context import BrowserConfig
from ofertas_hunter.browser.playwright_worker import PlaywrightBrowserWorker


async def main(url: str) -> int:
    config = BrowserConfig(
        headless=True,
        user_data_dir="secrets/browser_profiles/amazon",
        warmup_amazon_homepage=True,
        delay_between_requests_ms=(8000, 20000),
    )
    print(f"\n>>> Fetch {url}")
    async with PlaywrightBrowserWorker(config) as worker:
        page = await worker.fetch(url)

    print(f"  status:    {page.status}")
    print(f"  final_url: {page.final_url}")
    print(f"  bytes:     {len(page.html or '')}")
    print(f"  blocked:   {page.blocked}")

    out = Path("data/debug/amazon_test.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page.html or "", encoding="utf-8")
    print(f"  saved:     {out}")

    # Buscar elementos claves del producto
    html = page.html or ""
    title_m = re.search(r'<title[^>]*>([^<]+)</title>', html)
    productTitle = re.search(r'id="productTitle"[^>]*>([^<]+)</span>', html, re.DOTALL)
    print(f"\n  <title>:   {(title_m.group(1)[:80] if title_m else '???').strip()}")
    print(f"  productTitle: {(productTitle.group(1).strip()[:80] if productTitle else '(no encontrado)')}")

    # Precios visibles
    for label, pat in [
        ("a-offscreen", r'<span class="a-offscreen">([^<]+)</span>'),
        ("savingsPercentage", r'id="savingsPercentage"[^>]*>([^<]+)</'),
        ("priceblock", r'id="priceblock_[^"]+"[^>]*>([^<]+)</'),
        ("savingsAmount", r'reinventPriceSavings[^"]*"[^>]*>([^<]+)</'),
    ]:
        for m in re.finditer(pat, html)[:3] if False else list(re.finditer(pat, html))[:3]:
            print(f"  {label:20s} {m.group(1)[:60]}")

    # Producto disponible?
    for label, needle in [
        ("captcha?", "validateCaptcha"),
        ("robot check?", "Robot Check"),
        ("AirBox?", "amzn-captcha"),
        ("dp page?", 'id="ppd"'),
        ("buybox?", 'id="apex_desktop"'),
    ]:
        present = needle in html
        print(f"  {label:25s} {present}")

    return 0


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.amazon.com.mx/dp/B07C1WLPL3"
    sys.exit(asyncio.run(main(url)))
