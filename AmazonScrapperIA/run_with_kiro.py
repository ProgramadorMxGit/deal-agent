#!/usr/bin/env python3
"""
run_with_kiro.py — Ejecuta el scraper de Amazon usando kiro-cli como cerebro IA.

Este script actúa como orquestador: kiro-cli (Claude Sonnet 4.6) toma decisiones
sobre qué URLs explorar, cómo interpretar el DOM, y qué ofertas son genuinas.

Uso:
    py run_with_kiro.py                    # Sesión normal
    py run_with_kiro.py --continuous       # Loop infinito
    py run_with_kiro.py --analyze-dom URL  # Analizar DOM de una URL específica
    py run_with_kiro.py --report           # Ver estadísticas
"""
import subprocess
import sys
import json
import time
import argparse
import os
import glob
from pathlib import Path


KIRO_MODEL = "claude-sonnet-4-5"
BASE_DIR = Path(__file__).parent


def _find_kiro() -> str:
    """Encuentra el ejecutable de kiro-cli."""
    candidates = [
        r"C:\Users\Programador Mx\AppData\Local\kiro-cli\kiro-cli.exe",
        r"C:\Users\Programador Mx\AppData\Local\Kiro-Cli\kiro-cli.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    appdata = os.environ.get("LOCALAPPDATA", "")
    for sub in ["kiro-cli", "Kiro-Cli"]:
        p = os.path.join(appdata, sub, "kiro-cli.exe")
        if os.path.exists(p):
            return p
    return ""

KIRO_CMD = _find_kiro()


def check_kiro_available() -> bool:
    """Verifica que kiro-cli esté disponible y autenticado."""
    if not KIRO_CMD:
        return False
    try:
        result = subprocess.run(
            [KIRO_CMD, "whoami"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and "Not logged in" not in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ask_kiro(prompt: str, timeout: int = 60) -> str:
    """Llama a kiro-cli chat y retorna la respuesta."""
    if not KIRO_CMD:
        return "[kiro-cli no encontrado]"
    try:
        result = subprocess.run(
            [KIRO_CMD, "chat", "--no-interactive", "--model", KIRO_MODEL, prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8',
            errors='replace',
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return f"[Error kiro-cli: {result.stderr[:200]}]"
    except subprocess.TimeoutExpired:
        return "[Timeout en kiro-cli]"
    except FileNotFoundError:
        return "[kiro-cli no encontrado]"
    except Exception as e:
        return f"[Error: {e}]"


def analyze_dom_with_kiro(url: str) -> dict:
    """
    Usa kiro-cli para analizar el DOM de una URL de Amazon
    y extraer selectores CSS óptimos.
    """
    print(f"\n🔍 Analizando DOM de: {url}")
    print("   Usando kiro-cli (Claude Sonnet 4.6)...")

    prompt = f"""Eres un experto en web scraping de Amazon.com.mx.

Necesito que analices la estructura del DOM de esta URL de Amazon México y me des los selectores CSS más robustos para extraer:

URL: {url}

1. Precio actual del producto
2. Precio original/anterior (tachado)
3. Porcentaje de descuento
4. Título del producto
5. ASIN del producto
6. Badge de oferta/deal
7. Cupones disponibles

Basándote en tu conocimiento de la estructura HTML de Amazon.com.mx (2024-2026), proporciona:

RESPONDE con JSON puro (sin markdown):
{{
  "selectors": {{
    "price_current": "selector_css",
    "price_original": "selector_css",
    "discount_badge": "selector_css",
    "title": "selector_css",
    "asin": "selector_css",
    "deal_badge": "selector_css",
    "coupon": "selector_css"
  }},
  "notes": "observaciones sobre la estructura de Amazon MX",
  "fallback_strategy": "estrategia si los selectores principales fallan"
}}"""

    response = ask_kiro(prompt, timeout=45)
    print(f"\n🤖 Respuesta de Claude:\n{response[:500]}...")

    # Intentar parsear JSON
    try:
        import re
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    return {"raw_response": response}


def get_kiro_search_strategy(memory_data: dict) -> dict:
    """Pide a kiro-cli una estrategia de búsqueda optimizada."""
    print("\n🧠 Consultando estrategia con kiro-cli...")

    stats = memory_data.get("stats", {})
    best_cats = memory_data.get("best_categories", [])

    prompt = f"""Eres un experto cazador de ofertas en Amazon México.

El scraper ha recopilado estos datos de rendimiento:
- Sesiones ejecutadas: {stats.get('sessions', 0)}
- Páginas visitadas: {stats.get('total_pages_visited', 0)}
- Ofertas encontradas: {stats.get('total_offers_found', 0)}
- Mejores categorías: {best_cats}

Basándote en tu conocimiento de Amazon.com.mx, sugiere:
1. Las 10 mejores URLs de búsqueda para encontrar ofertas ≥50% ahora mismo
2. Keywords específicos que suelen tener grandes descuentos en Amazon MX
3. Categorías con más liquidaciones frecuentes
4. Patrones de URL de Amazon MX para páginas de deals

RESPONDE con JSON puro:
{{
  "priority_urls": ["url1", "url2", ...],
  "keywords": ["kw1", "kw2", ...],
  "best_categories": ["cat1", "cat2", ...],
  "deal_url_patterns": ["pattern1", ...],
  "tips": "consejos adicionales"
}}"""

    response = ask_kiro(prompt, timeout=45)

    try:
        import re
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    return {}


def run_scraper(args_list: list = None) -> int:
    """Ejecuta el scraper principal."""
    cmd = [sys.executable, str(BASE_DIR / "scraper.py")]
    if args_list:
        cmd.extend(args_list)

    print(f"\n▶️  Ejecutando: {' '.join(cmd)}")
    print("─" * 60)

    try:
        result = subprocess.run(cmd, cwd=str(BASE_DIR))
        return result.returncode
    except KeyboardInterrupt:
        print("\n⏹️  Interrumpido")
        return 0
    except Exception as e:
        print(f"Error ejecutando scraper: {e}")
        return 1


def show_report():
    """Muestra reporte de ofertas encontradas."""
    offers_path = BASE_DIR / "data" / "offers.json"
    memory_path = BASE_DIR / "data" / "memory.json"

    print("\n" + "="*60)
    print("📊 REPORTE DEL SISTEMA")
    print("="*60)

    # Ofertas
    if offers_path.exists():
        try:
            offers = json.loads(offers_path.read_text(encoding="utf-8"))
            print(f"\n🛍️  Total ofertas guardadas: {len(offers)}")

            # Top 10 por descuento
            sorted_offers = sorted(offers, key=lambda x: x.get("discount_percent", 0), reverse=True)
            print("\n🏆 TOP 10 MEJORES DESCUENTOS:")
            for i, o in enumerate(sorted_offers[:10], 1):
                title = o.get("title", "Sin título")[:50]
                disc = o.get("discount_percent", 0)
                price = o.get("price_current", 0)
                asin = o.get("asin", "N/A")
                verdict = o.get("ai_evaluation", {}).get("verdict", "")
                print(f"  {i:2}. [{disc}% OFF] ${price:,.0f} MXN — {title}")
                print(f"      ASIN: {asin} | IA: {verdict}")
        except Exception as e:
            print(f"Error leyendo ofertas: {e}")
    else:
        print("📭 No hay ofertas guardadas aún")

    # Memoria
    if memory_path.exists():
        try:
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
            stats = memory.get("stats", {})
            print(f"\n📈 ESTADÍSTICAS:")
            print(f"  Sesiones: {stats.get('sessions', 0)}")
            print(f"  Páginas visitadas: {stats.get('total_pages_visited', 0)}")
            print(f"  Productos evaluados: {stats.get('total_products_evaluated', 0)}")
            print(f"  Llamadas a IA: {stats.get('total_ai_calls', 0)}")
            print(f"  DOM heals: {stats.get('total_dom_heals', 0)}")
            print(f"  ASINs únicos vistos: {len(memory.get('seen_asins', []))}")
        except Exception as e:
            print(f"Error leyendo memoria: {e}")

    print("\n" + "="*60)


def main():
    parser = argparse.ArgumentParser(
        description="Amazon Offer Hunter — Orquestador con kiro-cli"
    )
    parser.add_argument("--continuous", action="store_true",
                        help="Modo continuo (loop infinito)")
    parser.add_argument("--max-offers", type=int, default=50,
                        help="Máximo ofertas por sesión")
    parser.add_argument("--min-discount", type=int, default=50,
                        help="Descuento mínimo %%")
    parser.add_argument("--no-headless", action="store_true",
                        help="Mostrar ventana del browser")
    parser.add_argument("--no-ai", action="store_true",
                        help="Sin evaluación IA")
    parser.add_argument("--analyze-dom", type=str, metavar="URL",
                        help="Analizar DOM de una URL con kiro-cli")
    parser.add_argument("--report", action="store_true",
                        help="Ver reporte y salir")
    parser.add_argument("--get-strategy", action="store_true",
                        help="Pedir estrategia a kiro-cli y salir")
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════════════════╗
║     🛒 AMAZON.COM.MX OFFER HUNTER — POWERED BY KIRO-CLI     ║
║                  Claude Sonnet 4.6                           ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Verificar kiro-cli
    if check_kiro_available():
        print("✅ kiro-cli disponible")
    else:
        print("⚠️  kiro-cli no encontrado en PATH — continuando sin IA")

    # Modo análisis DOM
    if args.analyze_dom:
        result = analyze_dom_with_kiro(args.analyze_dom)
        print("\n📋 Resultado del análisis:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Modo reporte
    if args.report:
        show_report()
        return

    # Modo estrategia
    if args.get_strategy:
        memory_path = BASE_DIR / "data" / "memory.json"
        memory_data = {}
        if memory_path.exists():
            try:
                memory_data = json.loads(memory_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        strategy = get_kiro_search_strategy(memory_data)
        print("\n🎯 Estrategia sugerida por IA:")
        print(json.dumps(strategy, ensure_ascii=False, indent=2))

        # Guardar URLs sugeridas en seeds
        if strategy.get("priority_urls"):
            seeds_path = BASE_DIR / "config" / "seeds.json"
            existing = json.loads(seeds_path.read_text(encoding="utf-8"))
            new_urls = [u for u in strategy["priority_urls"] if u not in existing]
            if new_urls:
                updated = strategy["priority_urls"] + existing
                seeds_path.write_text(
                    json.dumps(updated[:60], ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                print(f"\n✅ {len(new_urls)} nuevas URLs agregadas a seeds.json")
        return

    # Construir argumentos para el scraper
    scraper_args = []
    if args.max_offers:
        scraper_args.extend(["--max-offers", str(args.max_offers)])
    if args.min_discount:
        scraper_args.extend(["--min-discount", str(args.min_discount)])
    if args.no_headless:
        scraper_args.append("--no-headless")
    if args.no_ai:
        scraper_args.append("--no-ai")
    if args.continuous:
        scraper_args.append("--continuous")

    # Ejecutar scraper
    exit_code = run_scraper(scraper_args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
