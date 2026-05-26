#!/usr/bin/env python
"""Prueba manual de Playwright + AmazonProductParser.

Modos:

    # Smoke: abre una URL pública neutra (no Amazon) y reporta si Playwright funciona.
    python scripts/test_playwright.py --smoke

    # Validar una URL de Amazon real:
    python scripts/test_playwright.py --url "https://www.amazon.com.mx/dp/B0CZ2FW5R8"

    # Modo no-headless (debugging)
    python scripts/test_playwright.py --url "..." --no-headless
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


async def _smoke(headless: bool) -> int:
    try:
        from ofertas_hunter.browser.browser_context import BrowserConfig
        from ofertas_hunter.browser.playwright_worker import (
            PlaywrightBrowserWorker,
            PlaywrightImportError,
        )
    except ImportError as exc:
        print(f"ERROR import: {exc}")
        return 2

    try:
        worker = PlaywrightBrowserWorker(BrowserConfig(headless=headless))
        async with worker:
            page = await worker.fetch("https://example.com/")
        print(f"smoke ok: status={page.status} html_len={len(page.html)} url={page.final_url}")
        return 0 if page.ok else 3
    except PlaywrightImportError as exc:
        print(f"ERROR Playwright no instalado: {exc}")
        return 4


async def _validate(url: str, headless: bool) -> int:
    from ofertas_hunter.browser.browser_context import BrowserConfig
    from ofertas_hunter.browser.playwright_worker import PlaywrightBrowserWorker
    from ofertas_hunter.extraction.amazon_product_parser import AmazonProductParser

    worker = PlaywrightBrowserWorker(BrowserConfig(headless=headless))
    async with worker:
        page = await worker.fetch(url)

    if not page.ok:
        print(
            f"FETCH FAILED status={page.status} blocked={page.blocked} error={page.error}"
        )
        return 2

    product = AmazonProductParser().parse(page.html, page.final_url)
    print(f"  title:            {product.title}")
    print(f"  asin:             {product.asin}")
    print(f"  current_price:    {product.current_price}")
    print(f"  previous_price:   {product.previous_price}")
    print(f"  discount_percent: {product.discount_percent}")
    print(f"  image_url:        {product.image_url}")
    print(f"  in_stock:         {product.in_stock}")
    print(f"  is_publishable:   {product.is_publishable}")
    print(f"  reasons:          {product.not_publishable_reasons}")
    print(f"  warnings:         {product.extraction_warnings}")
    return 0 if product.is_publishable else 3


def main() -> int:
    parser = argparse.ArgumentParser(description="Prueba manual Playwright + Amazon")
    parser.add_argument("--smoke", action="store_true", help="Smoke test (no Amazon).")
    parser.add_argument("--url", help="URL de Amazon a validar.")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    if not args.smoke and not args.url:
        parser.error("usar --smoke o --url <amazon-url>")

    if args.smoke:
        return asyncio.run(_smoke(not args.no_headless))
    return asyncio.run(_validate(args.url, not args.no_headless))


if __name__ == "__main__":
    sys.exit(main())
