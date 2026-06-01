#!/usr/bin/env python3
"""
test_scraper.py — Prueba rápida del scraper en una URL específica de Amazon.
Verifica que Playwright, los selectores y kiro-cli funcionan correctamente.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

# Agregar src al path
sys.path.insert(0, str(Path(__file__).parent))


async def test_price_extraction():
    """Prueba extracción de precios en una página de Amazon MX."""
    from playwright.async_api import async_playwright
    from src.price_parser import extract_products_from_search, extract_product_data_from_page

    # URL de prueba — página de deals de Amazon MX
    test_urls = [
        "https://www.amazon.com.mx/s?k=audifonos&rh=p_n_deal_type%3A23566064011",
        "https://www.amazon.com.mx/gp/goldbox",
        "https://www.amazon.com.mx/s?k=laptop+oferta&s=price-desc-rank",
    ]

    print("\n🧪 TEST 1: Extracción de precios con Playwright")
    print("─" * 50)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="es-MX",
            timezone_id="America/Mexico_City",
        )
        page = await context.new_page()

        for url in test_urls[:1]:  # Solo probar la primera URL
            print(f"\n📡 Navegando a: {url[:70]}...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)

                # Verificar que cargó Amazon
                title = await page.title()
                print(f"   Título de página: {title[:60]}")

                # Extraer productos
                products = await extract_products_from_search(page, url)
                print(f"   Productos encontrados: {len(products)}")

                if products:
                    print(f"\n   Primeros 3 productos:")
                    for p_item in products[:3]:
                        title_p = p_item.get("title", "Sin título")[:50]
                        price = p_item.get("price_current")
                        original = p_item.get("price_original")
                        discount = p_item.get("discount_percent")
                        asin = p_item.get("asin", "N/A")
                        print(f"   • {title_p}")
                        print(f"     ASIN: {asin} | Precio: ${price} | Original: ${original} | Descuento: {discount}%")

                    # Contar ofertas ≥50%
                    good_offers = [p for p in products if (p.get("discount_percent") or 0) >= 50]
                    print(f"\n   ✅ Ofertas ≥50%: {len(good_offers)}/{len(products)}")
                else:
                    print("   ⚠️  No se encontraron productos — posible cambio en selectores")

            except Exception as e:
                print(f"   ❌ Error: {e}")

        await context.close()
        await browser.close()


def test_kiro_cli():
    """Prueba que kiro-cli responde correctamente."""
    import subprocess
    import os
    import glob

    print("\n🧪 TEST 2: kiro-cli (Claude Sonnet 4.6)")
    print("─" * 50)

    # Encontrar kiro-cli
    kiro_cmd = "kiro"
    appdata = os.environ.get("LOCALAPPDATA", "")
    for pattern in [
        r"Programs\Kiro\bin\kiro.cmd",
        r"Kiro-Cli\kiro.cmd",
        r"Kiro-Cli\kiro-cli.exe",
    ]:
        path = os.path.join(appdata, pattern)
        if os.path.exists(path):
            kiro_cmd = path
            break
    if kiro_cmd == "kiro":
        for p in glob.glob(os.path.join(appdata, "Kiro*", "bin", "kiro.cmd")):
            kiro_cmd = p
            break

    print(f"   kiro-cli encontrado en: {kiro_cmd}")

    prompt = """Responde SOLO con este JSON exacto (sin markdown):
{"status": "ok", "model": "claude-sonnet-4-5", "message": "kiro-cli funcionando correctamente para Amazon Scraper"}"""

    try:
        start = time.time()
        result = subprocess.run(
            [kiro_cmd, "--model", "claude-sonnet-4-5", "--no-interactive"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=60,
            encoding='utf-8',
            errors='replace',
            shell=kiro_cmd.endswith(".cmd"),
        )
        elapsed = time.time() - start

        if result.returncode == 0 and result.stdout:
            print(f"   ✅ kiro-cli respondió en {elapsed:.1f}s")
            print(f"   Respuesta: {result.stdout.strip()[:200]}")
            return True
        else:
            print(f"   ❌ kiro-cli error (código {result.returncode})")
            if result.stderr:
                print(f"   stderr: {result.stderr[:300]}")
            return False
    except subprocess.TimeoutExpired:
        print("   ❌ Timeout esperando kiro-cli")
        return False
    except FileNotFoundError:
        print(f"   ❌ kiro-cli no encontrado: {kiro_cmd}")
        return False


def test_memory_store():
    """Prueba el sistema de memoria."""
    print("\n🧪 TEST 3: MemoryStore")
    print("─" * 50)

    try:
        from src.memory_store import MemoryStore
        memory = MemoryStore("data/test_memory.json")

        # Probar operaciones básicas
        memory.mark_url_visited("https://www.amazon.com.mx/test")
        assert memory.is_url_visited("https://www.amazon.com.mx/test")

        memory.mark_asin_seen("B0TEST12345")
        assert memory.is_asin_seen("B0TEST12345")

        memory.record_category_result("Electrónicos", True, 65)
        memory.record_ai_evaluation("B0TEST12345", "Test Product", 65, "EXCELENTE", "Buen descuento")

        session_id = memory.start_session()
        memory.end_session(session_id, 5, 20)
        memory.save()

        summary = memory.get_summary()
        print(f"   ✅ MemoryStore funcionando")
        print(f"   Stats: {summary['stats']}")

        # Limpiar archivo de prueba
        Path("data/test_memory.json").unlink(missing_ok=True)
        return True
    except Exception as e:
        print(f"   ❌ Error en MemoryStore: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_price_parser():
    """Prueba el parser de precios."""
    print("\n🧪 TEST 4: Price Parser")
    print("─" * 50)

    try:
        from src.price_parser import parse_price_text, extract_discount_from_text, calculate_discount

        # Casos de prueba
        test_cases = [
            ("$1,299.00", 1299.0),
            ("$15,999", 15999.0),
            ("$299.99", 299.99),
            ("1299", 1299.0),
            ("$1.299,00", 1299.0),  # Formato europeo
        ]

        all_pass = True
        for text, expected in test_cases:
            result = parse_price_text(text)
            status = "✅" if result == expected else "❌"
            if result != expected:
                all_pass = False
            print(f"   {status} parse_price_text('{text}') = {result} (esperado: {expected})")

        # Prueba de descuento
        discount_cases = [
            ("-65%", 65),
            ("65% de descuento", 65),
            ("Ahorra 50%", 50),
            ("70% OFF", 70),
        ]
        for text, expected in discount_cases:
            result = extract_discount_from_text(text)
            status = "✅" if result == expected else "❌"
            if result != expected:
                all_pass = False
            print(f"   {status} extract_discount('{text}') = {result} (esperado: {expected})")

        # Prueba de cálculo
        calc = calculate_discount(500, 1000)
        status = "✅" if calc == 50 else "❌"
        print(f"   {status} calculate_discount(500, 1000) = {calc}% (esperado: 50%)")

        return all_pass
    except Exception as e:
        print(f"   ❌ Error en price_parser: {e}")
        import traceback
        traceback.print_exc()
        return False


async def run_all_tests():
    """Ejecuta todos los tests."""
    print("""
╔══════════════════════════════════════════════════════════════╗
║           🧪 AMAZON SCRAPER IA — TEST SUITE                  ║
╚══════════════════════════════════════════════════════════════╝
""")

    results = {}

    # Tests síncronos
    results["price_parser"] = test_price_parser()
    results["memory_store"] = test_memory_store()
    results["kiro_cli"] = test_kiro_cli()

    # Test asíncrono (Playwright)
    print("\n🧪 TEST 5: Playwright + Amazon.com.mx")
    print("─" * 50)
    try:
        await test_price_extraction()
        results["playwright"] = True
    except Exception as e:
        print(f"   ❌ Error en Playwright: {e}")
        results["playwright"] = False

    # Resumen
    print("\n" + "="*50)
    print("📋 RESUMEN DE TESTS:")
    all_pass = True
    for test, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status} — {test}")
        if not passed:
            all_pass = False

    print("\n" + ("✅ TODOS LOS TESTS PASARON" if all_pass else "⚠️  ALGUNOS TESTS FALLARON"))
    print("="*50)

    if all_pass:
        print("\n🚀 El scraper está listo para ejecutarse:")
        print("   py run_with_kiro.py                    # Sesión normal")
        print("   py run_with_kiro.py --continuous       # Loop infinito")
        print("   py run_with_kiro.py --report           # Ver estadísticas")
        print("   py run_with_kiro.py --get-strategy     # Estrategia IA")
        print("   py run_with_kiro.py --analyze-dom URL  # Analizar DOM")

    return all_pass


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
