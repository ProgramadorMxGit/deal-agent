#!/usr/bin/env python3
"""
scraper.py — Amazon.com.mx Offer Hunter con IA
Ejecutar: py scraper.py [--headless] [--max-offers N] [--min-discount N]

Usa kiro-cli (Claude Sonnet 4.6) para:
- Evaluar si las ofertas son genuinas
- Sanar selectores CSS cuando Amazon cambia su DOM
- Aprender qué categorías tienen más descuentos
- Generar estrategias de búsqueda mejoradas
"""
import asyncio
import json
import logging
import argparse
import sys
import time
from pathlib import Path

# Configurar logging con colores
class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
        'CRITICAL': '\033[35m',
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging(log_file: str = "data/scraper.log"):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # Console handler con colores
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ColorFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))

    # File handler sin colores
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)

    # Silenciar loggers ruidosos
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger("scraper")


def load_settings(overrides: dict = None) -> dict:
    settings_path = Path("config/settings.json")
    settings = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if overrides:
        settings.update({k: v for k, v in overrides.items() if v is not None})
    return settings


def load_seeds(settings: dict, memory=None) -> list:
    """Carga seeds priorizando los que mejor rendimiento han tenido."""
    seeds_path = Path("config/seeds.json")
    base_seeds = []
    if seeds_path.exists():
        base_seeds = json.loads(seeds_path.read_text(encoding="utf-8"))

    # Si hay memoria, priorizar mejores seeds
    if memory:
        best = memory.get_best_seeds(15)
        if best:
            # Poner mejores seeds primero
            prioritized = best + [s for s in base_seeds if s not in best]
            logger.info(f"🧠 Seeds priorizados por rendimiento histórico: {len(best)} seeds top")
            return prioritized

    return base_seeds


def print_banner():
    banner = """
╔══════════════════════════════════════════════════════════════╗
║          🛒 AMAZON.COM.MX OFFER HUNTER CON IA 🤖            ║
║                                                              ║
║  Motor: Claude Sonnet 4.6 via kiro-cli                      ║
║  Objetivo: Ofertas ≥ 50% de descuento                       ║
║  Modo: Autónomo + Auto-aprendizaje                          ║
╚══════════════════════════════════════════════════════════════╝
"""
    print(banner)


def print_offer_summary(offers: list):
    """Imprime resumen de ofertas encontradas."""
    if not offers:
        print("\n📭 No se encontraron ofertas en esta sesión.")
        return

    print(f"\n{'='*60}")
    print(f"🎯 OFERTAS ENCONTRADAS: {len(offers)}")
    print(f"{'='*60}")

    # Ordenar por descuento
    sorted_offers = sorted(offers, key=lambda x: x.get("discount_percent", 0), reverse=True)

    for i, offer in enumerate(sorted_offers[:20], 1):
        title = offer.get("title", "Sin título")[:55]
        discount = offer.get("discount_percent", 0)
        price = offer.get("price_current", 0)
        original = offer.get("price_original", 0)
        asin = offer.get("asin", "N/A")
        ai_eval = offer.get("ai_evaluation", {})
        verdict = ai_eval.get("verdict", "")
        emoji = ai_eval.get("emoji", "🛍️")

        verdict_color = {
            "EXCELENTE": "\033[92m",
            "BUENA": "\033[32m",
            "REGULAR": "\033[33m",
            "DESCARTAR": "\033[31m",
        }.get(verdict, "")
        reset = "\033[0m"

        print(f"\n{i:2}. {emoji} {title}")
        print(f"    💰 ${price:,.0f} MXN (antes ${original:,.0f}) — {discount_color(discount)}{discount}% OFF{reset}")
        print(f"    🔗 https://www.amazon.com.mx/dp/{asin}")
        if verdict:
            print(f"    🤖 IA: {verdict_color}{verdict}{reset} — {ai_eval.get('reasoning', '')[:80]}")

    if len(sorted_offers) > 20:
        print(f"\n... y {len(sorted_offers) - 20} ofertas más en data/offers.json")


def discount_color(discount: int) -> str:
    if discount >= 70:
        return "\033[92m"  # Verde brillante
    elif discount >= 60:
        return "\033[32m"  # Verde
    elif discount >= 50:
        return "\033[33m"  # Amarillo
    return "\033[0m"


async def run_adaptive_session(settings: dict, seeds: list, memory, ai, max_offers: int):
    """
    Ejecuta sesión adaptativa: aprende y mejora estrategia entre ciclos.
    """
    from src.browser_worker import BrowserWorker

    worker = BrowserWorker(settings, memory, ai)

    # Cada N sesiones, pedir a la IA una estrategia mejorada
    session_count = memory.get_stat("sessions")
    if session_count > 0 and session_count % 5 == 0:
        logger.info("🧠 Generando estrategia mejorada con IA...")
        summary = memory.get_summary()
        strategy = ai.generate_search_strategy(summary)
        if strategy:
            # Agregar nuevas URLs sugeridas por IA
            new_urls = strategy.get("new_search_urls", [])
            if new_urls:
                logger.info(f"🤖 IA sugirió {len(new_urls)} nuevas URLs de búsqueda")
                seeds = new_urls + seeds

            # Aprender keywords
            for kw in strategy.get("new_keywords", []):
                memory.learn_keyword(kw)
                # Crear URL de búsqueda para el keyword
                search_url = f"https://www.amazon.com.mx/s?k={kw.replace(' ', '+')}"
                if search_url not in seeds:
                    seeds.insert(0, search_url)

            logger.info(f"📝 Estrategia IA: {strategy.get('strategy_notes', '')[:100]}")

    offers = await worker.run_session(seeds, max_offers=max_offers)
    return offers


async def main():
    parser = argparse.ArgumentParser(
        description="Amazon.com.mx Offer Hunter con IA — Encuentra ofertas ≥50%"
    )
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Ejecutar browser en modo headless (default: True)")
    parser.add_argument("--no-headless", action="store_true",
                        help="Mostrar ventana del browser")
    parser.add_argument("--max-offers", type=int, default=50,
                        help="Máximo de ofertas por sesión (default: 50)")
    parser.add_argument("--min-discount", type=int, default=50,
                        help="Descuento mínimo en %% (default: 50)")
    parser.add_argument("--no-ai", action="store_true",
                        help="Desactivar evaluación con IA")
    parser.add_argument("--continuous", action="store_true",
                        help="Ejecutar en modo continuo (loop infinito)")
    parser.add_argument("--report", action="store_true",
                        help="Mostrar reporte de memoria y salir")
    parser.add_argument("--heal-test", action="store_true",
                        help="Probar DomHealer en una URL de Amazon")
    args = parser.parse_args()

    # Setup
    print_banner()
    settings = load_settings({
        "headless": not args.no_headless,
        "min_discount_percent": args.min_discount,
        "ai_eval_enabled": not args.no_ai,
    })
    setup_logging(settings.get("log_file", "data/scraper.log"))

    # Inicializar componentes
    from src.memory_store import MemoryStore
    from src.ai_evaluator import AIEvaluator

    memory = MemoryStore(settings.get("memory_file", "data/memory.json"))
    ai = AIEvaluator(
        memory_store=memory,
        model=settings.get("ai_model", "claude-sonnet-4-5")
    )

    # Modo reporte
    if args.report:
        summary = memory.get_summary()
        print("\n📊 REPORTE DE MEMORIA:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    # Cargar seeds
    seeds = load_seeds(settings, memory)
    logger.info(f"📋 {len(seeds)} seeds cargados")
    logger.info(f"⚙️  Configuración: min_discount={settings['min_discount_percent']}%, "
                f"headless={settings['headless']}, ai={settings['ai_eval_enabled']}")

    start_time = time.time()
    total_offers = []

    try:
        if args.continuous:
            logger.info("🔄 Modo continuo activado — Ctrl+C para detener")
            cycle = 0
            while True:
                cycle += 1
                logger.info(f"\n{'='*50}")
                logger.info(f"🔄 CICLO {cycle}")
                logger.info(f"{'='*50}")

                offers = await run_adaptive_session(
                    settings, seeds, memory, ai, args.max_offers
                )
                total_offers.extend(offers)

                elapsed = time.time() - start_time
                rate = len(total_offers) / (elapsed / 3600) if elapsed > 0 else 0
                logger.info(f"📈 Total acumulado: {len(total_offers)} ofertas | {rate:.1f} ofertas/hora")

                # Pausa entre ciclos
                pause = 60  # 1 minuto entre ciclos
                logger.info(f"⏸️  Pausa de {pause}s antes del siguiente ciclo...")
                await asyncio.sleep(pause)
        else:
            # Sesión única
            offers = await run_adaptive_session(
                settings, seeds, memory, ai, args.max_offers
            )
            total_offers = offers

    except KeyboardInterrupt:
        logger.info("\n⏹️  Detenido por usuario")

    # Resumen final
    elapsed = time.time() - start_time
    print_offer_summary(total_offers)

    print(f"\n{'='*60}")
    print(f"⏱️  Tiempo total: {elapsed/60:.1f} minutos")
    print(f"📦 Ofertas encontradas: {len(total_offers)}")
    print(f"💾 Guardadas en: data/offers.json")
    print(f"🧠 Memoria actualizada: data/memory.json")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
