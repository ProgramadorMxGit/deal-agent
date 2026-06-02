#!/usr/bin/env python
"""SPIKE (prueba, no toca el bot): captura screenshots recortados de la ficha
de producto para Mercado Libre y Amazon, al estilo de `_capture_detail_section`
del scraper legacy.

Objetivo: validar visualmente si un screenshot de la zona superior del PDP
(imagen + título + precio + buy box) se ve mejor que la imagen pública que el
bot manda hoy a WhatsApp.

NO modifica nada del bot. Abre un contexto Playwright controlado (viewport
desktop fijo 1366x900), inyecta las cookies de cada marketplace y navega a
URLs reales. Guarda PNGs en `data/screenshot_spike/`.

Hallazgos del spike (para la fase de implementación):
- ML: usar la forma de URL `www.mercadolibre.com.mx/.../p/MLM` (la subdominio
  `articulo.` devuelve 404 ahora). Requiere cookies + Accept-Language es-MX +
  esperar networkidle para que la galería colapse a carrusel (si no, ML rinde
  una variante "stacked" de 7000+ px).
- Amazon: layout estable de 3 columnas (#leftCol/#centerCol/#rightCol).

Uso:
    .\.venv\Scripts\python.exe scripts\spike_product_screenshots.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "data" / "screenshot_spike"
ML_COOKIES = REPO_ROOT / "secrets" / "mercadolibre_cookies.json"
AMAZON_COOKIES = REPO_ROOT / "secrets" / "amazon_cookies.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.6367.207 Safari/537.36"
)

VIEWPORT = {"width": 1366, "height": 900}

TEST_PRODUCTS: list[dict[str, str]] = [
    {
        "marketplace": "mercadolibre",
        "name": "ml_estante_especias",
        "url": "https://www.mercadolibre.com.mx/estante-de-cocina-de-dos-niveles-organizador-de-especias-negro/p/MLM63543475",
    },
    {
        "marketplace": "mercadolibre",
        "name": "ml_organizador_ollas",
        "url": "https://www.mercadolibre.com.mx/8-estante-organizador-para-ollas-y-sartenes-ajustable-cocina-kidomy/p/MLM62630579",
    },
    {
        "marketplace": "mercadolibre",
        "name": "ml_fregadero_acero",
        "url": "https://www.mercadolibre.com.mx/fregadero-de-acero-inoxidable-plateado-80x45x22cm-tarja-para-cocina-con-diseno-cascada-tipo-empotrar-y-kit-con-organizador/p/MLM63023077",
    },
    {
        "marketplace": "amazon",
        "name": "amazon_jbl_flip6",
        "url": "https://www.amazon.com.mx/dp/B09GK544PP",
    },
    {
        "marketplace": "amazon",
        "name": "amazon_lg_tv75",
        "url": "https://www.amazon.com.mx/dp/B0F7TKJMCR",
    },
    {
        "marketplace": "amazon",
        "name": "amazon_johnnie_walker",
        "url": "https://www.amazon.com.mx/dp/B00861CC7Y",
    },
]

# Selectores de columna por marketplace (unión = recorte preferido).
MARKETPLACE_SELECTORS: dict[str, dict[str, Any]] = {
    "amazon": {
        "columns": ["#leftCol", "#centerCol", "#rightCol"],
        "containers": ["#dp-container", "#ppd", "#dp"],
        "title_wait": "#productTitle",
        "cookie_accept": ["#sp-cc-accept", "input#sp-cc-accept", "button[name='accept']"],
    },
    "mercadolibre": {
        # Layout desktop limpio: galería (izq) + título/precio (centro) + buybox (der).
        "columns": [
            ".ui-pdp-gallery",
            ".ui-pdp-title",
            ".ui-pdp-price",
            ".ui-pdp-buybox",
        ],
        "containers": [".ui-pdp-container__row", ".ui-pdp-container"],
        "title_wait": "h1.ui-pdp-title",
        "cookie_accept": [
            "button[data-testid='action:understood-button']",
            "button.cookie-consent-banner-opt-out__action",
        ],
    },
}

MAX_SECTION_WIDTH = 1200.0
MAX_SECTION_HEIGHT = 980.0
PAD = 16


def load_cookies(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        print(f"[WARN] cookies no encontradas: {path}")
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[WARN] cookies inválidas en {path}: {exc}")
        return []
    if not isinstance(raw, list):
        return []
    samesite_map = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "none": "None"}
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        c = dict(entry)
        if "expirationDate" in c and "expires" not in c:
            c["expires"] = c.pop("expirationDate")
        if c.get("session") is True and "expires" in c:
            del c["expires"]
        ss = c.get("sameSite")
        if ss in {"Strict", "Lax", "None"}:
            pass
        elif isinstance(ss, str) and ss.lower() in samesite_map:
            c["sameSite"] = samesite_map[ss.lower()]
        else:
            c.pop("sameSite", None)
        for unknown in list(set(c.keys()) - allowed):
            del c[unknown]
        if c.get("expires") is not None:
            try:
                c["expires"] = float(c["expires"])
            except (TypeError, ValueError):
                del c["expires"]
        out.append(c)
    return out


def capture_detail_section(page: Any, marketplace: str, target_path: Path) -> str:
    """Captura recortada de la zona superior del PDP. Devuelve estrategia."""
    sel = MARKETPLACE_SELECTORS[marketplace]
    vp_h = VIEWPORT["height"]

    # 1) Unión de columnas/elementos clave dentro del viewport visible.
    boxes = []
    for selector in sel["columns"]:
        loc = page.locator(selector).first
        try:
            if loc.count() == 0:
                continue
            box = loc.bounding_box()
        except Exception:
            continue
        if not box or box.get("width", 0) <= 10 or box.get("height", 0) <= 10:
            continue
        # Ignorar elementos que arrancan fuera del viewport visible (la
        # galería ML "stacked" mide miles de px y rompe el clip).
        if box["y"] > vp_h:
            continue
        print(f"     col {selector} -> x={box['x']:.0f} y={box['y']:.0f} "
              f"w={box['width']:.0f} h={box['height']:.0f}")
        boxes.append(box)

    if boxes:
        min_x = min(b["x"] for b in boxes)
        min_y = min(b["y"] for b in boxes)
        max_x = max(b["x"] + b["width"] for b in boxes)
        max_y = max(b["y"] + b["height"] for b in boxes)
        width = min(max(1.0, (max_x - min_x) + PAD * 2), MAX_SECTION_WIDTH)
        height = min(max(1.0, (max_y - min_y) + PAD * 2), MAX_SECTION_HEIGHT)
        # Nunca exceder el viewport renderizado (clip fuera = error).
        height = min(height, vp_h - max(0, min_y - PAD) - 1)
        clip = {
            "x": float(max(0, min_x - PAD)),
            "y": float(max(0, min_y - PAD)),
            "width": float(width),
            "height": float(max(1.0, height)),
        }
        page.screenshot(path=str(target_path), clip=clip)
        return "columns"

    # 2) Fallback contenedor.
    for selector in sel["containers"]:
        loc = page.locator(selector).first
        try:
            if loc.count() == 0:
                continue
            box = loc.bounding_box()
        except Exception:
            continue
        if not box:
            continue
        height = min(MAX_SECTION_HEIGHT, max(1.0, box["height"] + PAD * 2))
        height = min(height, vp_h - max(0, box["y"] - PAD) - 1)
        clip = {
            "x": float(max(0, box["x"] - PAD)),
            "y": float(max(0, box["y"] - PAD)),
            "width": float(min(MAX_SECTION_WIDTH, max(1.0, box["width"] + PAD * 2))),
            "height": float(max(1.0, height)),
        }
        page.screenshot(path=str(target_path), clip=clip)
        return "container"

    # 3) Viewport visible.
    page.screenshot(path=str(target_path))
    return "viewport"


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ml_cookies = load_cookies(ML_COOKIES)
    amazon_cookies = load_cookies(AMAZON_COOKIES)
    print(f"[INFO] cookies ML={len(ml_cookies)} Amazon={len(amazon_cookies)}")
    print(f"[INFO] salida: {OUTPUT_DIR}")

    results: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
            ],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="es-MX",
            timezone_id="America/Mexico_City",
            viewport=VIEWPORT,
            extra_http_headers={
                "Accept-Language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
            },
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        for cookies, label in ((ml_cookies, "ML"), (amazon_cookies, "Amazon")):
            if cookies:
                try:
                    context.add_cookies(cookies)
                except Exception as exc:
                    print(f"[WARN] add_cookies {label} falló: {exc}")

        page = context.new_page()
        for product in TEST_PRODUCTS:
            marketplace = product["marketplace"]
            name = product["name"]
            url = product["url"]
            sel = MARKETPLACE_SELECTORS[marketplace]
            target = OUTPUT_DIR / f"{name}.png"
            print(f"\n[..] {name} ({marketplace})\n     {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                for selector in sel["cookie_accept"]:
                    loc = page.locator(selector).first
                    try:
                        if loc.count() > 0:
                            loc.click(timeout=1200)
                            page.wait_for_timeout(400)
                            break
                    except Exception:
                        pass
                try:
                    page.wait_for_selector(sel["title_wait"], timeout=12000)
                except Exception:
                    print(f"     [warn] no apareció {sel['title_wait']}")
                # Esperar a que la galería/JS termine (colapsa stacked → carrusel).
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(900)
                strategy = capture_detail_section(page, marketplace, target)
                title = page.title()
                print(f"[OK] {target.name} | estrategia={strategy} | {title[:60]}")
                results.append({"name": name, "marketplace": marketplace, "url": url,
                                "final_url": page.url, "strategy": strategy,
                                "title": title, "file": str(target.relative_to(REPO_ROOT))})
            except Exception as exc:
                print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
                results.append({"name": name, "marketplace": marketplace, "url": url, "error": str(exc)})

        context.close()
        browser.close()

    summary_path = OUTPUT_DIR / "_summary.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in results if "error" not in r)
    print(f"\n[INFO] resumen: {summary_path}")
    print(f"[INFO] capturados {ok}/{len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
