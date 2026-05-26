"""Entry point CLI.

Comandos:

    python -m ofertas_hunter init-db          # crea/actualiza schema
    python -m ofertas_hunter check-config     # imprime config (sin secretos)
    python -m ofertas_hunter dispatch         # corre dispatcher (dry-run por defecto)
    python -m ofertas_hunter dispatch --once  # un solo tick y termina
    python -m ofertas_hunter enqueue-sample   # encola una oferta de prueba
    python -m ofertas_hunter run              # (Fase 3+) arranca todos los agentes

Por defecto **PUBLISHING_DRY_RUN=true** y **PUBLISHING_ENABLED=false**: el
dispatcher no manda nada real hasta que el operador edite `.env`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Settings, get_settings
from .db import connection, init_db
from .dispatching.cooldown import CooldownPolicy
from .dispatching.dispatcher import OutboxDispatcher, make_sqlite_published_recorder
from .dispatching.outbox import OutboxConfig, SqliteOutbox
from .logging_setup import configure_logging
from .models import OutboxItem, OutboxType
from .publishing.evolution_client import EvolutionClient
from .publishing.whatsapp_publisher import WhatsAppPublisher
from .telegram.candidate_builder import (
    LISTENER_DEAL,
    LISTENER_IGNORED_ML,
    LISTENER_NOISE,
    LISTENER_PRICE_ERROR,
    TelegramCandidateBuilder,
)
from .telegram.channel_config import parse_channels
from .telegram.message_parser import parse_message


logger = logging.getLogger(__name__)


def _redact(value: Optional[str]) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 4:
        return "***"
    return value[:2] + "***" + value[-2:]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init_db(_args: argparse.Namespace) -> int:
    s = get_settings()
    target = init_db()
    logger.info("DB inicializada en %s (db_path=%s)", target, s.db_path)
    return 0


def cmd_check_config(_args: argparse.Namespace) -> int:
    s = get_settings()
    print(f"env: {s.env}")
    print(f"log_level: {s.log_level}")
    print(f"db_path: {s.db_path_resolved}")
    print(f"amazon_enabled: {s.amazon_enabled}")
    print(f"mercadolibre_enabled: {s.mercadolibre_enabled}")
    print(f"telegram_enabled: {s.telegram_enabled}")
    print(f"telegram_channels: {s.telegram_channel_list}")
    print(f"whatsapp_enabled: {s.whatsapp_enabled}")
    print(f"publishing_enabled: {s.publishing_enabled}")
    print(f"publishing_dry_run: {s.publishing_dry_run}")
    print(f"evolution_base_url: {s.evolution_base_url or '(unset)'}")
    print(f"evolution_instance: {s.evolution_instance or '(unset)'}")
    print(f"whatsapp_group: {s.whatsapp_group or '(unset)'}")
    print(f"evolution_api_key: {_redact(s.evolution_api_key)}")
    print(f"anthropic_api_key: {_redact(s.anthropic_api_key)}")
    print(
        f"thresholds: confirmed>={s.price_error_threshold_confirmed} "
        f"possible>={s.price_error_threshold_possible} "
        f"suspicious>={s.price_error_threshold_suspicious} "
        f"normal>={s.normal_offer_min_discount}"
    )
    return 0


def _build_dispatcher(s: Settings):
    """Construye el dispatcher leyendo config + DB. Devuelve también la
    conexión y el publisher para que el caller los cierre.
    """
    init_db()
    from .db import connect
    conn = connect()  # responsabilidad del caller cerrarla

    outbox = SqliteOutbox(
        conn,
        OutboxConfig(
            revalidate_age_seconds=s.whatsapp_outbox_revalidate_age_seconds,
            cooldown=CooldownPolicy(cooldown_seconds=s.whatsapp_cooldown_seconds),
        ),
    )

    client = EvolutionClient(
        base_url=s.evolution_base_url,
        api_key=s.evolution_api_key,
        instance=s.evolution_instance,
        dry_run=s.publishing_dry_run,
    )
    publisher = WhatsAppPublisher(
        client=client,
        target_group_id=s.whatsapp_group,
        enabled=s.publishing_enabled,
    )
    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        published_recorder=make_sqlite_published_recorder(conn),
        idle_sleep_seconds=s.dispatcher_idle_sleep_seconds,
    )
    return dispatcher, client, conn


def cmd_dispatch(args: argparse.Namespace) -> int:
    s = get_settings()
    if not s.publishing_enabled:
        logger.warning(
            "PUBLISHING_ENABLED=false — el dispatcher correrá pero todos los "
            "intentos quedarán como skipped (publishing_disabled)."
        )
    if s.publishing_dry_run:
        logger.warning(
            "PUBLISHING_DRY_RUN=true — los envíos serán simulados (no llaman a "
            "Evolution API)."
        )

    dispatcher, client, conn = _build_dispatcher(s)

    async def _run():
        try:
            if args.once:
                await dispatcher.tick()
            else:
                await dispatcher.run_forever()
        finally:
            await client.aclose()
            conn.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Dispatcher interrumpido por usuario")
    return 0


def cmd_enqueue_sample(args: argparse.Namespace) -> int:
    """Encola una oferta de prueba en el outbox SQLite.

    Útil para validar el dispatcher sin tener todavía hunters reales.
    """
    s = get_settings()
    init_db()

    if args.kind == "price_error":
        payload = {
            "title": "Apple iPhone 16 Pro Max 256GB Titanio Natural",
            "current_price": 3899,
            "url": "https://www.liverpool.com.mx/p/example-i",
            "image_url": "https://m.media-amazon.com/images/I/example.jpg",
            "confidence_label": "very high",
            "marketplace": "liverpool",
        }
        item_type = OutboxType.PRICE_ERROR.value
    else:
        payload = {
            "title": "JBL Tune 510BT - Auriculares in-Ear inalámbricos con Sonido Purebass, Color Azul",
            "current_price": 388,
            "previous_price": 899,
            "discount_percent": 57,
            "url": "https://amzn.to/4e3yTjG",
            "image_url": "https://m.media-amazon.com/images/I/61sjXQq8nVL._AC_SL1500_.jpg",
        }
        item_type = OutboxType.NORMAL.value

    with connection() as conn:
        # Crear o reutilizar product (UNIQUE en url_canonical).
        existing = conn.execute(
            "SELECT id FROM products WHERE url_canonical = ?",
            (payload["url"],),
        ).fetchone()
        if existing is not None:
            product_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO products (marketplace, url_canonical, title, condition, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, 'new', ?, ?)",
                (
                    payload.get("marketplace") or "amazon",
                    payload["url"],
                    payload["title"],
                    _now_iso(),
                    _now_iso(),
                ),
            )
            product_id = cur.lastrowid

        cur = conn.execute(
            "INSERT INTO offers (product_id, classification, score, reasons_json, "
            "discount_percent, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                product_id,
                "price_error_confirmed" if item_type == OutboxType.PRICE_ERROR.value else "normal_offer",
                95 if item_type == OutboxType.PRICE_ERROR.value else 60,
                "[\"sample\"]",
                payload.get("discount_percent"),
                "eligible",
                _now_iso(),
                _now_iso(),
            ),
        )
        offer_id = cur.lastrowid

        outbox = SqliteOutbox(
            conn,
            OutboxConfig(
                revalidate_age_seconds=s.whatsapp_outbox_revalidate_age_seconds,
                cooldown=CooldownPolicy(cooldown_seconds=s.whatsapp_cooldown_seconds),
            ),
        )
        item = outbox.enqueue(
            OutboxItem(offer_id=offer_id, type=item_type, message_payload=payload)
        )

    print(
        f"Encolado outbox id={item.id} offer_id={offer_id} type={item_type} "
        f"title={payload['title'][:50]}"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Arranca el orchestrator completo (todos los agentes registrados)."""
    from .config import PROJECT_ROOT
    from .db import connect
    from .orchestrator import (
        Orchestrator,
        OrchestratorConfig,
        load_amazon_seeds,
        load_mercadolibre_seeds,
    )

    s = get_settings()
    init_db()
    if s.publishing_enabled and not s.publishing_dry_run:
        logger.warning(
            "PUBLISHING_ENABLED=true y PUBLISHING_DRY_RUN=false: el dispatcher "
            "ENVIARÁ MENSAJES REALES a WhatsApp."
        )
    else:
        logger.info(
            "publishing_enabled=%s publishing_dry_run=%s — "
            "modo seguro activo",
            s.publishing_enabled,
            s.publishing_dry_run,
        )

    config = OrchestratorConfig(
        once=getattr(args, "once", False),
        amazon_seeds=load_amazon_seeds(s),
        mercadolibre_seeds=load_mercadolibre_seeds(s),
        amazon_hunt_limit=getattr(args, "limit", None) or 5,
        mercadolibre_hunt_limit=getattr(args, "limit", None) or 5,
        schedule_enabled=s.schedule_enabled,
        schedule_timezone=s.schedule_timezone,
        hibernate_start=s.hibernate_start,
        hibernate_end=s.hibernate_end,
        warmup_start=s.warmup_start,
        active_start=s.active_start,
        warmup_loop_interval=s.warmup_loop_interval_seconds,
        hibernation_check_interval=s.hibernation_check_interval_seconds,
    )

    async def _run():
        conn = connect()
        try:
            orch = Orchestrator(conn, settings=s, config=config)
            await orch.run()
        finally:
            conn.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("orchestrator interrumpido por usuario")
    return 0


# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------


def cmd_telegram_check_config(_args: argparse.Namespace) -> int:
    s = get_settings()
    print(f"telegram_enabled: {s.telegram_enabled}")
    print(f"api_id: {_redact(str(s.resolved_telegram_api_id) if s.resolved_telegram_api_id else None)}")
    print(f"api_hash: {_redact(s.resolved_telegram_api_hash)}")
    print(f"session_path: {s.telegram_session_path_resolved}")
    entries = parse_channels(s.telegram_target_channels or s.telegram_channels)
    print(f"target_channels ({len(entries)}):")
    for entry in entries:
        print(f"  - {entry.raw} (kind={entry.kind})")
    print(f"backfill_limit: {s.telegram_backfill_limit_per_channel}")
    print(f"backfill_budget: {s.telegram_backfill_process_budget_per_channel}")
    print(f"workers: {s.telegram_channel_workers}")
    print(f"ignore_mercadolibre: {s.telegram_ignore_mercadolibre_links}")
    print(f"link_resolver_timeout: {s.telegram_link_resolver_timeout_seconds}s")
    if s.telegram_enabled and not (
        s.resolved_telegram_api_id and s.resolved_telegram_api_hash
    ):
        print("\n⚠️ TELEGRAM_ENABLED=true pero faltan credenciales (api_id / api_hash).")
        return 2
    return 0


def cmd_telegram_parse_sample(args: argparse.Namespace) -> int:
    """Parsea un fixture local sin tocar Telegram."""
    fixture_path = Path(args.path)
    if not fixture_path.exists():
        # Permitir nombrar sólo el archivo dentro de tests/fixtures/telegram.
        candidates = [
            Path("tests/fixtures/telegram") / args.path,
            Path("tests/fixtures/telegram") / f"{args.path}.txt",
        ]
        for c in candidates:
            if c.exists():
                fixture_path = c
                break
    if not fixture_path.exists():
        print(f"ERROR: fixture no encontrado: {args.path}")
        return 2

    text = fixture_path.read_text(encoding="utf-8")
    parsed = parse_message(
        text,
        channel="cli",
        message_id=0,
        captured_at=datetime.now(timezone.utc),
        image_path=args.image_path,
    )
    builder = TelegramCandidateBuilder(
        ignore_mercadolibre_links=get_settings().telegram_ignore_mercadolibre_links,
    )
    candidate = builder.build(parsed)

    print("--- parsed ---")
    print(f"  marketplace:      {parsed.marketplace}")
    print(f"  brand:            {parsed.brand}")
    print(f"  category:         {parsed.category}")
    print(f"  written_price:    {parsed.written_price}")
    print(f"  discount_visible: {parsed.discount_visible}")
    print(f"  urgency_score:    {parsed.urgency_score}")
    print(f"  urgency_terms:    {parsed.urgency_terms}")
    print(f"  is_price_error:   {parsed.is_price_error_keyword}")
    print(f"  hashtags:         {parsed.hashtags}")
    print(f"  original_url:     {parsed.original_url}")
    print(f"  skip_reason:      {parsed.skip_reason}")
    print()
    print("--- candidate ---")
    print(f"  internal:         {candidate.internal_classification}")
    if candidate.scoring is not None:
        print(f"  score:            {candidate.scoring.score}")
        print(f"  classification:   {candidate.scoring.classification}")
        print(f"  confidence:       {candidate.scoring.confidence_label}")
        print(f"  is_publishable:   {candidate.scoring.is_publishable}")
    if candidate.outbox_item is not None:
        print(f"  outbox_type:      {candidate.outbox_item.type}")
        print(f"  requires_live:    {candidate.outbox_item.message_payload.get('requires_live_validation')}")
    return 0


def cmd_telegram_listen(args: argparse.Namespace) -> int:
    """Modo `--once`: hace un backfill corto y termina.

    Si TELEGRAM_ENABLED=false, sólo imprime que está deshabilitado.
    Si las credenciales no están, falla con MissingCredentialsError de forma
    clara (no rompe el resto del paquete).
    """
    s = get_settings()
    if not s.telegram_enabled:
        print(
            "TELEGRAM_ENABLED=false — el listener no se conectará. "
            "Configura el .env y vuelve a intentar."
        )
        return 0

    # Importación tardía para no exigir Telethon cuando está deshabilitado.
    from .agents.telegram_listener_agent import (
        TelegramListenerAgent,
        TelegramListenerConfig,
    )

    config = TelegramListenerConfig(
        enabled=True,
        api_id=s.resolved_telegram_api_id,
        api_hash=s.resolved_telegram_api_hash,
        session_path=str(s.telegram_session_path_resolved),
        target_channels=parse_channels(s.telegram_target_channels or s.telegram_channels),
        backfill_limit_per_channel=args.limit or s.telegram_backfill_limit_per_channel,
        backfill_process_budget_per_channel=s.telegram_backfill_process_budget_per_channel,
        ignore_mercadolibre_links=s.telegram_ignore_mercadolibre_links,
        link_resolver_timeout_seconds=s.telegram_link_resolver_timeout_seconds,
        link_resolver_max_redirects=s.telegram_link_resolver_max_redirects,
    )

    try:
        from .telegram.telethon_listener import TelethonAdapter
    except Exception as exc:
        logger.error("No se pudo importar Telethon: %s", exc)
        return 2

    init_db()
    from .db import connect

    adapter = TelethonAdapter(
        api_id=config.api_id,
        api_hash=config.api_hash,
        session_path=config.session_path,
    )

    async def _run():
        conn = connect()
        try:
            agent = TelegramListenerAgent(config=config, adapter=adapter, db_conn=conn)
            try:
                if args.command == "telegram-backfill" or args.once:
                    outcomes = await agent.backfill_once()
                    actionable = sum(1 for o in outcomes if o.candidate.is_actionable)
                    duplicates = sum(1 for o in outcomes if o.duplicate)
                    print(
                        f"Backfill done: total={len(outcomes)} actionable={actionable} "
                        f"duplicates={duplicates}"
                    )
                else:
                    outcome = await agent.listen_once()
                    if outcome:
                        print(
                            f"channel={outcome.message.channel} "
                            f"msg_id={outcome.message.message_id} "
                            f"class={outcome.candidate.internal_classification}"
                        )
            finally:
                await agent.aclose()
        finally:
            conn.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("Telegram listener interrumpido.")
    return 0


# ---------------------------------------------------------------------------
# Amazon commands
# ---------------------------------------------------------------------------


def _build_browser(no_headless: bool, *, user_data_dir: Optional[str] = None):
    """Construye un PlaywrightBrowserWorker. Lazy-imports para que el resto
    del CLI funcione sin Playwright.

    Si `user_data_dir` está set, el browser usa `launch_persistent_context`
    para reutilizar la sesión entre runs (cookies, localStorage, etc.).
    """
    from .browser.browser_context import BrowserConfig
    from .browser.playwright_worker import PlaywrightBrowserWorker

    return PlaywrightBrowserWorker(
        BrowserConfig(
            headless=not no_headless,
            user_data_dir=user_data_dir,
        )
    )


def cmd_amazon_parse_url(args: argparse.Namespace) -> int:
    """Descarga una URL Amazon y muestra los datos extraídos."""
    from .extraction.amazon_product_parser import AmazonProductParser

    async def _run():
        browser = _build_browser(args.no_headless)
        try:
            async with browser:
                page = await browser.fetch(args.url)
        finally:
            await browser.aclose()

        if not page.ok:
            print(f"FETCH FAILED status={page.status} error={page.error} blocked={page.blocked}")
            return 2

        product = AmazonProductParser().parse(page.html, page.final_url)
        print(f"--- ExtractedProduct ({page.final_url}) ---")
        print(f"  title:            {product.title}")
        print(f"  asin:             {product.asin}")
        print(f"  current_price:    {product.current_price}")
        print(f"  previous_price:   {product.previous_price}")
        print(f"  discount_percent: {product.discount_percent}")
        print(f"  image_url:        {product.image_url}")
        print(f"  in_stock:         {product.in_stock}")
        print(f"  brand:            {product.brand_guess}")
        print(f"  category:         {product.category_guess}")
        print(f"  monthly_payment:  {product.is_monthly_payment}")
        print(f"  is_publishable:   {product.is_publishable}")
        print(f"  reasons:          {product.not_publishable_reasons}")
        print(f"  warnings:         {product.extraction_warnings}")
        print(f"  confidence:       {product.extraction_confidence}")

        if args.command == "amazon-validate-url" and not product.is_publishable:
            return 3
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 1


def cmd_amazon_hunt(args: argparse.Namespace) -> int:
    """Corre el AmazonHunterAgent contra N seeds."""
    from .agents.amazon_hunter_agent import AmazonHunterAgent
    from .db import connect

    init_db()

    seeds = list(args.seed)
    if not seeds:
        seeds_file = PROJECT_ROOT / "config" / "seeds" / "amazon.json"
        if seeds_file.exists():
            seeds = json.loads(seeds_file.read_text(encoding="utf-8"))
        else:
            print(
                "ERROR: no se proporcionaron --seed y no existe config/seeds/amazon.json. "
                "Usa --seed <url> al menos una vez."
            )
            return 2

    seeds = seeds[: args.limit]

    async def _run():
        browser = _build_browser(args.no_headless)
        conn = connect()
        try:
            async with browser:
                agent = AmazonHunterAgent(browser=browser, db_conn=conn)
                outcomes = await agent.hunt_urls(seeds, max_urls=args.limit)
        finally:
            await browser.aclose()
            conn.close()

        for o in outcomes:
            status = (
                "ENQUEUED" if o.enqueued_outbox_id is not None
                else f"DISCARDED ({o.discarded_reason})"
            )
            title = (o.extracted.title if o.extracted and o.extracted.title else "(no title)")[:60]
            print(f"{status:30s} | {o.url} | {title}")
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 1


def cmd_revalidate_outbox(args: argparse.Namespace) -> int:
    """Revalida los items pendientes que requieren validación o tengan >1h."""
    from .agents.amazon_hunter_agent import AmazonHunterAgent  # noqa: F401  (asegura import branch)
    from .db import connect
    from .dispatching.cooldown import CooldownPolicy
    from .dispatching.outbox import OutboxConfig, SqliteOutbox
    from .revalidation.playwright_revalidator import PlaywrightRevalidator

    s = get_settings()
    init_db()

    async def _run():
        browser = _build_browser(args.no_headless)
        conn = connect()
        try:
            async with browser:
                outbox = SqliteOutbox(
                    conn,
                    OutboxConfig(
                        revalidate_age_seconds=s.whatsapp_outbox_revalidate_age_seconds,
                        cooldown=CooldownPolicy(cooldown_seconds=s.whatsapp_cooldown_seconds),
                    ),
                )
                revalidator = PlaywrightRevalidator(browser=browser, db_conn=conn)
                pending = list(outbox.pending())[: args.limit]
                if not pending:
                    print("No hay items pending en outbox.")
                    return 0
                for item in pending:
                    detail = await revalidator.revalidate_detailed(item)
                    if detail.ok:
                        print(
                            f"OK   outbox_id={item.id:<5} type={item.type:<13} "
                            f"score_class={detail.classification:<25} confidence={detail.confidence_label}"
                        )
                    else:
                        print(
                            f"BAD  outbox_id={item.id:<5} fatal={detail.fatal_reason} "
                            f"reasons={detail.reasons}"
                        )
        finally:
            await browser.aclose()
            conn.close()
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 1


def cmd_revalidate_url(args: argparse.Namespace) -> int:
    """Revalida una URL como si fuera un item del outbox (sin tocar SQLite)."""
    from .models import OutboxItem, OutboxType
    from .revalidation.playwright_revalidator import PlaywrightRevalidator

    async def _run():
        browser = _build_browser(args.no_headless)
        try:
            async with browser:
                revalidator = PlaywrightRevalidator(browser=browser)
                item = OutboxItem(
                    offer_id=0,
                    type=OutboxType.NORMAL.value,
                    message_payload={
                        "title": args.expected_title or "",
                        "url": args.url,
                        "image_url": "",
                        "current_price": 0,
                        "marketplace": "amazon",
                        "source": "manual",
                    },
                )
                detail = await revalidator.revalidate_detailed(item)
        finally:
            await browser.aclose()

        print(f"--- revalidate-url ({args.url}) ---")
        print(f"  ok:                  {detail.ok}")
        print(f"  classification:      {detail.classification}")
        print(f"  confidence:          {detail.confidence_label}")
        if detail.extracted:
            p = detail.extracted
            print(f"  title:               {p.title}")
            print(f"  asin:                {p.asin}")
            print(f"  current_price:      {p.current_price}")
            print(f"  previous_price:     {p.previous_price}")
            print(f"  discount_percent:   {p.discount_percent}")
            print(f"  image_url:          {p.image_url}")
            print(f"  in_stock:           {p.in_stock}")
            print(f"  is_publishable:     {p.is_publishable}")
            print(f"  not_publishable:    {p.not_publishable_reasons}")
        if detail.fatal_reason:
            print(f"  fatal_reason:       {detail.fatal_reason}")
        return 0 if detail.ok else 3

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 1


def cmd_ml_enrich_affiliates(args: argparse.Namespace) -> int:
    """Enriquece items ML existentes del outbox con affiliate_url.

    Reglas:
    - Sólo items marketplace=mercadolibre.
    - Sólo items source != telegram.
    - Sólo items sin affiliate_url.
    - Usa canonical_url para abrir el producto.
    - No publica nada.
    - Si Playwright o cookies no están disponibles, marca status=pending.
    """
    from .agents.mercadolibre_affiliate_enricher import (
        MercadoLibreAffiliateEnricher,
    )
    from .db import connect

    init_db()

    async def _run():
        extractor = None
        browser = None

        if not args.no_extractor:
            try:
                from .browser.browser_context import BrowserConfig
                from .browser.playwright_worker import (
                    PlaywrightBrowserWorker,
                    PlaywrightImportError,
                )
                from .marketplaces.mercadolibre_affiliate import (
                    PlaywrightAffiliateExtractor,
                )
                from .session.mercadolibre_session import MercadoLibreSession

                browser = PlaywrightBrowserWorker(
                    BrowserConfig(headless=not args.no_headless)
                )
                # _ensure_started es interno; lo invocamos para tener el context
                # listo y poder inyectar cookies antes de extraer.
                await browser._ensure_started()  # noqa: SLF001
                s = get_settings()
                session = MercadoLibreSession.from_settings(
                    cookies_path=s.mercadolibre_cookies_path,
                )
                cookies, health = session.load()
                if cookies and browser._context is not None:  # noqa: SLF001
                    await browser._context.add_cookies(cookies)  # noqa: SLF001
                    print(f"[ml-enrich] cookies cargadas: {health.loaded}")
                else:
                    print(
                        "[ml-enrich] aviso: sin cookies — el modal Compartir no aparecerá; "
                        "items se marcarán como failed"
                    )
                extractor = PlaywrightAffiliateExtractor(browser._context)  # noqa: SLF001
            except PlaywrightImportError as exc:  # type: ignore[name-defined]
                print(f"[ml-enrich] Playwright no instalado: {exc}")
                extractor = None
            except Exception as exc:
                print(f"[ml-enrich] no se pudo iniciar Playwright: {exc}")
                extractor = None

        conn = connect()
        try:
            enricher = MercadoLibreAffiliateEnricher(conn, extractor)
            report = await enricher.run(limit=args.limit)
        finally:
            conn.close()
            if browser is not None:
                await browser.aclose()

        print("--- ml-enrich-affiliates ---")
        print(f"  candidates: {report.total_candidates}")
        print(f"  enriched:   {report.enriched}")
        print(f"  failed:     {report.failed}")
        print(f"  skipped:    {report.skipped}")
        for outcome in report.outcomes[:20]:
            label = outcome.status.upper()
            tail = (
                f"affiliate={outcome.affiliate_url}"
                if outcome.status == "ok"
                else f"error={outcome.error}"
            )
            print(f"  [{label:7s}] outbox_id={outcome.outbox_id} | {tail}")
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 1


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Maintenance commands (Fase 4)
# ---------------------------------------------------------------------------


def cmd_compress_memory(_args: argparse.Namespace) -> int:
    from .db import connect
    from .memory.compressor import MemoryCompressor

    init_db()
    conn = connect()
    try:
        report = MemoryCompressor(conn).run()
    finally:
        conn.close()

    print("--- compress-memory ---")
    print(f"  dom_snapshots deleted:        {report.deleted_dom_snapshots}")
    print(f"  runtime_events deleted:       {report.deleted_runtime_events}")
    print(f"  discarded_candidates deleted: {report.deleted_discarded_candidates}")
    print(f"  agent_runs deleted:           {report.deleted_agent_runs}")
    print(f"  summaries:                    {report.summary_kinds}")
    return 0


def cmd_export_memory_summary(args: argparse.Namespace) -> int:
    from .db import connect

    init_db()
    conn = connect()
    try:
        if args.kind:
            row = conn.execute(
                "SELECT content FROM memory_summaries WHERE kind = ? "
                "ORDER BY id DESC LIMIT 1",
                (args.kind,),
            ).fetchone()
            payload = (
                json.loads(row["content"]) if row else {"error": f"kind={args.kind} not found"}
            )
        else:
            rows = conn.execute(
                "SELECT kind, content, generated_at FROM memory_summaries "
                "WHERE id IN ("
                "  SELECT MAX(id) FROM memory_summaries GROUP BY kind"
                ") ORDER BY kind"
            ).fetchall()
            payload = {
                r["kind"]: {
                    "generated_at": r["generated_at"],
                    "content": json.loads(r["content"]),
                }
                for r in rows
            }
    finally:
        conn.close()

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"export-memory-summary → {args.out}")
    else:
        print(text)
    return 0


def cmd_audit_amazon_captcha(args: argparse.Namespace) -> int:
    """Audita snapshots Amazon catalogados como captcha y reclasifica
    falsos positivos según el nuevo `AmazonCaptchaDetector`.

    Ejemplo:
        python -m ofertas_hunter audit-amazon-captcha --recent --fix
    """
    from .browser.amazon_captcha_audit import run_audit
    from .db import connect

    init_db()
    conn = connect()
    try:
        report = run_audit(
            conn,
            fix=args.fix,
            days=args.days,
            limit=args.limit,
        )
        if args.fix:
            conn.commit()
    finally:
        conn.close()

    print("--- audit-amazon-captcha ---")
    print(f"  scanned_snapshots:           {report.scanned_snapshots}")
    print(f"  real_captcha (high):         {report.real_captcha}")
    print(f"  reclassified_false_positives:{report.reclassified_false_positives}")
    print(f"  discarded_reclassified:      {report.discarded_reclassified}")
    print(f"  runtime_events_emitted:      {report.runtime_events_emitted}")
    print(f"  fix:                         {args.fix}")
    if not report.findings:
        print("  (sin snapshots Amazon recientes — todo limpio)")
        return 0
    print()
    for f in report.findings[:30]:
        url_short = (f.url or "")[:60]
        signals = ",".join(f.new_strong_signals) or "(none)"
        print(
            f"  [{f.action:<32s}] snap={f.snapshot_id:<6} "
            f"new_conf={f.new_confidence:<6} strong={signals[:40]:<40s} "
            f"url={url_short}"
        )
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Abre Chromium NO headless con un perfil persistente y deja que el
    operador se autentique manualmente. La sesión queda guardada en disco
    para los siguientes runs del bot.

    Ejemplos:

        python -m ofertas_hunter login --marketplace mercadolibre
        python -m ofertas_hunter login --marketplace amazon
        python -m ofertas_hunter login --url https://www.netflix.com --profile-name custom
    """
    from .browser.browser_context import BrowserConfig
    from .browser.playwright_worker import PlaywrightBrowserWorker

    s = get_settings()

    # Resolver URL inicial y user_data_dir según marketplace.
    presets = {
        "mercadolibre": (
            s.mercadolibre_user_data_dir or "secrets/browser_profiles/mercadolibre",
            "https://www.mercadolibre.com.mx",
        ),
        "amazon": (
            s.amazon_user_data_dir or "secrets/browser_profiles/amazon",
            "https://www.amazon.com.mx",
        ),
    }

    marketplace = (args.marketplace or "").lower()
    if args.profile_name:
        user_data_dir = f"secrets/browser_profiles/{args.profile_name}"
    elif marketplace in presets:
        user_data_dir = presets[marketplace][0]
    else:
        print(
            "ERROR: especifica --marketplace {mercadolibre|amazon} o "
            "--profile-name <nombre>"
        )
        return 2

    initial_url = args.url
    if not initial_url and marketplace in presets:
        initial_url = presets[marketplace][1]
    if not initial_url:
        initial_url = "about:blank"

    profile_path = Path(user_data_dir).expanduser()
    profile_path.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 60)
    print(" Login interactivo — sesión persistente del navegador")
    print("=" * 60)
    print(f"  Marketplace:    {marketplace or '(custom)'}")
    print(f"  Perfil:         {profile_path}")
    print(f"  URL inicial:    {initial_url}")
    print()
    print(" Se abrirá Chromium con un perfil persistente. Inicia sesión")
    print(" manualmente como lo harías en cualquier navegador. Cuando")
    print(" termines, vuelve a esta terminal y presiona ENTER para cerrar.")
    print()
    print(" La sesión queda guardada en el perfil. El bot la reutiliza")
    print(" automáticamente en los siguientes runs (sin re-login).")
    print()

    config = BrowserConfig(
        headless=False,
        user_data_dir=str(profile_path),
        # No bloqueamos recursos durante el login para que la página
        # cargue todo (imágenes captcha, fuentes, CSS, JS).
        block_resource_types=(),
        capture_screenshot_on_failure=False,
        # Headers HTTP mínimos durante el login: dejamos que Chromium
        # mande los suyos sin sobrescribir nada. Eso evita que sitios
        # como ML reciban combinaciones raras de Sec-Fetch-* que
        # disparan el flow degradado.
        extra_http_headers_amazon={},
        # Viewport fijo razonable (no aleatorio) para que la UI del
        # marketplace se renderice como en un navegador normal.
        viewport={"width": 1366, "height": 768},
    )

    async def _run() -> int:
        worker = PlaywrightBrowserWorker(config)
        # Entramos manualmente al lifecycle para tener control total y
        # poder reaccionar al cierre del navegador por parte del usuario.
        await worker._ensure_started()  # noqa: SLF001
        try:
            assert worker._context is not None  # noqa: SLF001
            ctx = worker._context  # noqa: SLF001
            page = await ctx.new_page()
            try:
                await page.goto(initial_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                logger.warning("goto inicial falló: %s — la ventana sigue abierta", exc)

            # Evento que se setea si el usuario cierra el navegador con
            # la X. Playwright dispara `close` en el contexto/página.
            closed_event = asyncio.Event()
            loop = asyncio.get_event_loop()

            def _on_close(*_args: object) -> None:
                # Callback síncrono de Playwright: marca el evento async.
                loop.call_soon_threadsafe(closed_event.set)

            try:
                ctx.on("close", _on_close)
                page.on("close", _on_close)
            except Exception:
                pass

            print(" Navegador abierto. Inicia sesión en la pestaña que apareció.")
            print(" Cuando hayas terminado puedes:")
            print("   - presionar ENTER en esta terminal,  o")
            print("   - cerrar la ventana del navegador.")
            print(" Cualquiera de las dos guarda la sesión correctamente.")

            async def _wait_input() -> str:
                return await loop.run_in_executor(None, input, " > ")

            input_task = asyncio.create_task(_wait_input())
            close_task = asyncio.create_task(closed_event.wait())
            done, pending = await asyncio.wait(
                {input_task, close_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

            try:
                final_url = page.url
                print(f"\n Última URL visitada: {final_url}")
            except Exception:
                pass

            # Forzamos un flush limpio del contexto persistente: cerramos
            # las pages primero para que Chromium escriba los SQLite del
            # perfil (Login Data, Cookies, etc.) antes de salir.
            try:
                await page.close()
            except Exception:
                pass
        finally:
            # IMPORTANTE: aclose hace `context.close()` que es lo que
            # garantiza el flush del perfil persistente a disco. Si no
            # se llama, las cookies se pierden cuando el proceso muere.
            await worker.aclose()

        print()
        print(f" ✓ Sesión guardada en {profile_path}")
        # Mostrar tamaño en disco para confirmar que se persistió.
        try:
            total = sum(
                f.stat().st_size for f in profile_path.rglob("*") if f.is_file()
            )
            count = sum(1 for f in profile_path.rglob("*") if f.is_file())
            print(f"   {count} archivos · {total / 1024:.1f} KB en disco")
            if count == 0:
                print(
                    "\n ⚠ El perfil quedó vacío. Probablemente cerraste la "
                    "ventana antes de que Chromium escribiera el perfil."
                )
                print(" Intenta de nuevo y espera a que aparezca el botón ENTER.")
                return 2
        except Exception:
            pass
        print(" El bot reutilizará esta sesión en próximos runs.")
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 1



    """Audita snapshots Amazon catalogados como captcha y reclasifica
    falsos positivos según el nuevo `AmazonCaptchaDetector`.

    Ejemplo:
        python -m ofertas_hunter audit-amazon-captcha --recent --fix
    """
    from .browser.amazon_captcha_audit import run_audit
    from .db import connect

    init_db()
    conn = connect()
    try:
        report = run_audit(
            conn,
            fix=args.fix,
            days=args.days,
            limit=args.limit,
        )
        if args.fix:
            conn.commit()
    finally:
        conn.close()

    print("--- audit-amazon-captcha ---")
    print(f"  scanned_snapshots:           {report.scanned_snapshots}")
    print(f"  real_captcha (high):         {report.real_captcha}")
    print(f"  reclassified_false_positives:{report.reclassified_false_positives}")
    print(f"  discarded_reclassified:      {report.discarded_reclassified}")
    print(f"  runtime_events_emitted:      {report.runtime_events_emitted}")
    print(f"  fix:                         {args.fix}")
    if not report.findings:
        print("  (sin snapshots Amazon recientes — todo limpio)")
        return 0
    print()
    for f in report.findings[:30]:
        url_short = (f.url or "")[:60]
        signals = ",".join(f.new_strong_signals) or "(none)"
        print(
            f"  [{f.action:<32s}] snap={f.snapshot_id:<6} "
            f"new_conf={f.new_confidence:<6} strong={signals[:40]:<40s} "
            f"url={url_short}"
        )
    return 0


def cmd_amazon_captcha_check(args: argparse.Namespace) -> int:
    """Ejecuta el detector contra una URL (vivo) o un snapshot (path local).

    Ejemplos:
        python -m ofertas_hunter amazon-captcha-check https://www.amazon.com.mx/dp/B0X
        python -m ofertas_hunter amazon-captcha-check ./snapshot.html
    """
    from .browser.amazon_captcha_detector import AmazonCaptchaDetector

    target = args.url_or_path
    detector = AmazonCaptchaDetector()
    final_url = target if target.startswith("http") else ""
    html: str

    path = Path(target)
    if path.exists() and path.is_file():
        html = path.read_text(encoding="utf-8", errors="ignore")
    elif target.startswith("http"):
        # Fetch en vivo con Playwright
        async def _fetch() -> str:
            browser = _build_browser(args.no_headless)
            try:
                async with browser:
                    page = await browser.fetch(target)
            finally:
                await browser.aclose()
            return page.html or "", page.final_url

        html, final_url = asyncio.run(_fetch())
    else:
        print(f"ERROR: '{target}' no es URL ni archivo existente")
        return 2

    result = detector.assess(html=html, final_url=final_url, status=200)
    print("--- amazon-captcha-check ---")
    print(f"  target:                   {target}")
    print(f"  final_url:                {final_url or '(local)'}")
    print(f"  bytes:                    {len(html)}")
    print(f"  is_captcha:               {result.is_captcha}")
    print(f"  confidence:               {result.confidence}")
    print(f"  strong_signals:           {list(result.strong_signals)}")
    print(f"  weak_signals:             {list(result.weak_signals)}")
    print(f"  visible_signals:          {list(result.visible_signals)}")
    print(f"  should_pause_marketplace: {result.should_pause_marketplace}")
    print(f"  reasons:                  {list(result.reasons)}")
    return 0 if not result.is_high_confidence else 3


def cmd_audit_false_price_errors(args: argparse.Namespace) -> int:
    """REGLA 9: limpieza de outbox/published recientes con falsos PE.

    Ejemplo:
        python -m ofertas_hunter audit-false-price-errors --fix
    """
    from .db import connect
    from .intelligence.false_price_error_audit import run_audit

    init_db()
    conn = connect()
    try:
        report = run_audit(
            conn,
            fix=args.fix,
            days=args.days,
            limit=args.limit,
        )
        if args.fix:
            conn.commit()
    finally:
        conn.close()

    print("--- audit-false-price-errors ---")
    print(f"  scanned:      {report.scanned}")
    print(f"  findings:     {len(report.findings)}")
    print(f"  discarded:    {report.discarded}")
    print(f"  degraded:     {report.degraded}")
    print(f"  logged_only:  {report.logged_only}")
    print(f"  fix:          {args.fix}")
    if not report.findings:
        print("  (sin falsos positivos detectados — todo limpio)")
        return 0
    for f in report.findings[:50]:
        title = (f.title or "")[:60]
        price = f"${f.current_price}" if f.current_price is not None else "?"
        print(
            f"  [{f.action:<22s}] outbox_id={f.outbox_id:<5} state={f.state:<10s} "
            f"market={f.marketplace or '?'} price={price} "
            f"compat={f.mentions_compatible_with_premium} "
            f"acc={f.is_generic_accessory} "
            f"title={title!r}"
        )
    return 0


def cmd_check_db(_args: argparse.Namespace) -> int:
    from .db import connect

    init_db()
    conn = connect()
    try:
        tables = [
            "products",
            "price_observations",
            "offers",
            "outbox",
            "published_messages",
            "discarded_candidates",
            "telegram_messages",
            "dom_snapshots",
            "selector_versions",
            "agent_runs",
            "memory_summaries",
            "runtime_events",
        ]
        print("--- check-db ---")
        for t in tables:
            try:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
                print(f"  {t:<25s} {row['n']:>10}")
            except Exception as exc:
                print(f"  {t:<25s} ERROR: {exc}")
        # Pragmas relevantes
        ((mode,),) = conn.execute("PRAGMA journal_mode").fetchall()
        ((fk,),) = conn.execute("PRAGMA foreign_keys").fetchall()
        print(f"\n  journal_mode={mode}  foreign_keys={fk}")
    finally:
        conn.close()
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    from .db import connect
    from .runtime.scheduler import OperatingScheduler, ScheduleConfig

    s = get_settings()
    init_db()
    conn = connect()
    try:
        print("--- ofertas_hunter status ---")
        print(f"  env: {s.env}  log_level: {s.log_level}")
        print(f"  publishing_enabled: {s.publishing_enabled}")
        print(f"  publishing_dry_run: {s.publishing_dry_run}")
        print(f"  amazon_enabled:     {s.amazon_enabled}")
        print(f"  mercadolibre_enabled: {s.mercadolibre_enabled}")
        print(f"  telegram_enabled:   {s.telegram_enabled}")
        print(f"  ml_affiliate_required: {s.mercadolibre_affiliate_required_for_publish}")

        # Schedule actual
        sched = OperatingScheduler(
            ScheduleConfig.from_env(
                enabled=s.schedule_enabled,
                timezone_name=s.schedule_timezone,
                hibernate_start=s.hibernate_start,
                hibernate_end=s.hibernate_end,
                warmup_start=s.warmup_start,
                active_start=s.active_start,
            )
        )
        decision = sched.decide()
        local_now = sched.now()
        print()
        print(f"  schedule:           {decision.mode.value}")
        print(f"  local_time ({s.schedule_timezone}): {local_now.strftime('%H:%M:%S')}")
        print(
            f"  next_change:        {decision.next_mode.value} en "
            f"{decision.next_change_in}"
        )
        print()

        # Agent runs activos
        rows = conn.execute(
            "SELECT agent_name, status, started_at, last_heartbeat FROM agent_runs "
            "WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 10"
        ).fetchall()
        if rows:
            print("  active agents:")
            for r in rows:
                print(
                    f"    - {r['agent_name']:<20s} status={r['status']} "
                    f"started={r['started_at']} hb={r['last_heartbeat']}"
                )
        else:
            print("  active agents: (ninguno)")

        # Outbox snapshot
        rows = conn.execute(
            "SELECT type, state, COUNT(*) AS n FROM outbox GROUP BY type, state ORDER BY type"
        ).fetchall()
        print(f"\n  outbox:")
        if rows:
            for r in rows:
                print(f"    {r['type']:<12s} {r['state']:<12s} {r['n']}")
        else:
            print("    (vacío)")

        # Últimos runtime_events críticos / errores
        rows = conn.execute(
            "SELECT created_at, severity, kind FROM runtime_events "
            "WHERE severity IN ('error', 'critical') ORDER BY id DESC LIMIT 5"
        ).fetchall()
        print(f"\n  recent critical/error runtime_events:")
        if rows:
            for r in rows:
                print(f"    [{r['severity']:<8s}] {r['kind']:<25s} {r['created_at']}")
        else:
            print("    (ninguno)")

        # DB
        ((mode,),) = conn.execute("PRAGMA journal_mode").fetchall()
        print(f"\n  db_path: {s.db_path_resolved}  journal_mode={mode}")
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# mcp-serve
# ---------------------------------------------------------------------------


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Arranca el servidor MCP por stdio para clientes como kiro-cli.

    El servidor reusa la misma DB, scheduler, settings y agentes del bot.
    Aplica Hard_Rules server-side (cooldown, gates, modo seguro, scheduler).
    Por defecto adquiere un lockfile en `data/mcp_serve.lock` para detectar
    instancias concurrentes con `python -m ofertas_hunter run`.

    IMPORTANTE: todos los logs van a stderr para no contaminar el stream
    JSON-RPC de MCP que usa stdout.
    """
    import logging as _logging

    from .db import connect
    from .mcp.context import ServerContext
    from .mcp.lockfile import FileLock, LockConflict
    from .mcp.server import MCPServer

    # Redirigir TODOS los logs a stderr — stdout es exclusivo del protocolo MCP
    for handler in _logging.root.handlers[:]:
        _logging.root.removeHandler(handler)
    _logging.basicConfig(
        stream=sys.stderr,
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    s = get_settings()
    init_db()

    lock_path = s.db_path_resolved.parent / "mcp_serve.lock"
    lock: Optional[FileLock] = None
    if not getattr(args, "no_lock", False):
        lock = FileLock(lock_path, scope="mcp-serve")
        try:
            lock.acquire()
        except LockConflict as exc:
            print(
                f"ERROR: ya hay otra instancia activa: {exc}",
                file=sys.stderr,
            )
            return 2

    conn = connect()

    async def _run() -> None:
        try:
            ctx = ServerContext.build(db=conn, settings=s)
            server = MCPServer(ctx, lockfile=lock)
            logger.info(
                "mcp-serve: %d tools registradas, scheduler %s",
                len(server.registry),
                ctx.scheduler.decide().mode.value,
            )
            await server.serve_stdio()
        finally:
            try:
                await ctx.aclose()
            except Exception:
                logger.exception("aclose ctx falló")
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    logger.exception("lock.release falló")
            conn.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("mcp-serve interrumpido por usuario")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ofertas-hunter",
        description="Bot autónomo de ofertas y errores de precio.",
    )
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Crea o actualiza el schema SQLite.")
    sub.add_parser("check-config", help="Imprime configuración resuelta.")

    sp_dispatch = sub.add_parser("dispatch", help="Corre el dispatcher de outbox a WhatsApp.")
    sp_dispatch.add_argument("--once", action="store_true", help="Ejecuta un solo tick y termina.")

    sp_sample = sub.add_parser(
        "enqueue-sample",
        help="Inserta una oferta de prueba en el outbox para validar el dispatcher.",
    )
    sp_sample.add_argument(
        "--kind", choices=("normal", "price_error"), default="normal"
    )

    sp_run = sub.add_parser("run", help="Arranca todos los agentes (orchestrator).")
    sp_run.add_argument("--once", action="store_true",
                        help="Cada agente hace un solo ciclo y termina (CI/dev).")
    sp_run.add_argument("--limit", type=int, default=None,
                        help="Override del límite de URLs por ciclo de hunters.")

    sub.add_parser(
        "telegram-check-config", help="Imprime la config Telegram resuelta."
    )

    sp_parse = sub.add_parser(
        "telegram-parse-sample",
        help="Parsea un fixture .txt y muestra el candidate generado.",
    )
    sp_parse.add_argument("path", help="Path al fixture o nombre dentro de tests/fixtures/telegram.")
    sp_parse.add_argument("--image-path", default=None)

    sp_listen = sub.add_parser(
        "telegram-listen",
        help="Conecta a Telegram (Telethon) y escucha mensajes.",
    )
    sp_listen.add_argument("--once", action="store_true", help="Hace un backfill corto y termina.")
    sp_listen.add_argument("--limit", type=int, default=None, help="Override de backfill_limit_per_channel.")

    sp_backfill = sub.add_parser(
        "telegram-backfill",
        help="Hace un backfill de N mensajes por canal y termina.",
    )
    sp_backfill.add_argument("--limit", type=int, default=20)

    # ---- Amazon / Revalidator ------------------------------------------------
    sp_apu = sub.add_parser(
        "amazon-parse-url",
        help="Descarga una URL Amazon con Playwright y muestra el ExtractedProduct.",
    )
    sp_apu.add_argument("url")
    sp_apu.add_argument("--no-headless", action="store_true")

    sp_avu = sub.add_parser(
        "amazon-validate-url",
        help="Igual que amazon-parse-url pero levanta exit code != 0 si no es publicable.",
    )
    sp_avu.add_argument("url")
    sp_avu.add_argument("--no-headless", action="store_true")

    sp_hunt = sub.add_parser(
        "amazon-hunt",
        help="Procesa N seeds de Amazon (URLs de producto) y crea offers/outbox.",
    )
    sp_hunt.add_argument("--once", action="store_true")
    sp_hunt.add_argument("--limit", type=int, default=5)
    sp_hunt.add_argument("--no-headless", action="store_true")
    sp_hunt.add_argument("--seed", action="append", default=[],
                         help="URL de producto (puede repetirse). Si está vacío, lee de config/seeds/amazon.json.")

    sp_rev = sub.add_parser(
        "revalidate-outbox",
        help="Revalida items pendientes con Playwright (>1h o telegram).",
    )
    sp_rev.add_argument("--once", action="store_true")
    sp_rev.add_argument("--limit", type=int, default=10)
    sp_rev.add_argument("--no-headless", action="store_true")

    sp_revu = sub.add_parser(
        "revalidate-url",
        help="Revalida una URL específica como si fuera un item del outbox.",
    )
    sp_revu.add_argument("url")
    sp_revu.add_argument("--expected-title", default=None)
    sp_revu.add_argument("--no-headless", action="store_true")

    # ---- Mercado Libre affiliate enrichment ---------------------------------
    sp_enr = sub.add_parser(
        "ml-enrich-affiliates",
        help="Regenera affiliate_url en items existentes del outbox (ML, no-Telegram, sin afiliado).",
    )
    sp_enr.add_argument("--limit", type=int, default=20)
    sp_enr.add_argument("--no-headless", action="store_true")
    sp_enr.add_argument(
        "--no-extractor",
        action="store_true",
        help="Marca todos los candidatos con status=pending sin abrir Playwright.",
    )

    # ---- Watchdog / Memory --------------------------------------------------
    sp_watch = sub.add_parser(
        "watchdog-tick",
        help="Ejecuta un tick del watchdog (útil para tests/CI).",
    )
    sp_watch.add_argument("--once", action="store_true", default=True)

    sp_compress = sub.add_parser(
        "compress-memory",
        help="Compacta tablas auditables y genera memory_summaries.",
    )

    sp_export = sub.add_parser(
        "export-memory-summary",
        help="Exporta los últimos memory_summaries a JSON en stdout o archivo.",
    )
    sp_export.add_argument("--kind", default=None,
                           help="discard_reasons | unstable_selectors | runtime_events_by_severity | marketplace_observations")
    sp_export.add_argument("--out", default=None, help="Archivo destino (default stdout)")

    sp_audit_pe = sub.add_parser(
        "audit-false-price-errors",
        help=(
            "Audita outbox/published recientes en busca de accesorios "
            "publicados como ERROR DE PRECIO (REGLA 9)."
        ),
    )
    sp_audit_pe.add_argument(
        "--fix",
        action="store_true",
        help="Aplica correcciones (degrade / discard) en items pending.",
    )
    sp_audit_pe.add_argument(
        "--days",
        type=int,
        default=7,
        help="Ventana de tiempo a auditar (días).",
    )
    sp_audit_pe.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Tope de items a revisar.",
    )

    sp_audit_amz = sub.add_parser(
        "audit-amazon-captcha",
        help=(
            "Re-clasifica snapshots Amazon catalogados como captcha "
            "usando el nuevo AmazonCaptchaDetector. Detecta falsos positivos."
        ),
    )
    sp_audit_amz.add_argument(
        "--recent",
        action="store_true",
        help="Sólo audita los últimos --days (default 7).",
    )
    sp_audit_amz.add_argument(
        "--fix",
        action="store_true",
        help="Reclasifica discarded_candidates si ya no son captcha real.",
    )
    sp_audit_amz.add_argument(
        "--days",
        type=int,
        default=7,
        help="Ventana de tiempo a auditar (días).",
    )
    sp_audit_amz.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Tope de snapshots a revisar.",
    )

    sp_amz_check = sub.add_parser(
        "amazon-captcha-check",
        help=(
            "Aplica AmazonCaptchaDetector a una URL o snapshot HTML. "
            "Imprime is_captcha / confidence / strong_signals / weak_signals."
        ),
    )
    sp_amz_check.add_argument(
        "url_or_path",
        help="URL https:// o path local a un snapshot HTML.",
    )
    sp_amz_check.add_argument("--no-headless", action="store_true")

    sp_login = sub.add_parser(
        "login",
        help=(
            "Abre Chromium NO headless con un perfil persistente para "
            "iniciar sesión manualmente. La sesión se reutiliza en runs."
        ),
    )
    sp_login.add_argument(
        "--marketplace",
        choices=("mercadolibre", "amazon"),
        default=None,
        help="Marketplace conocido (preset de URL inicial + perfil).",
    )
    sp_login.add_argument(
        "--url",
        default=None,
        help="URL inicial (override del preset).",
    )
    sp_login.add_argument(
        "--profile-name",
        default=None,
        help=(
            "Nombre de perfil custom dentro de secrets/browser_profiles/. "
            "Útil para sesiones que no son ML/Amazon."
        ),
    )

    sub.add_parser(
        "check-db",
        help="Reporte rápido del estado de la DB (counts de tablas clave).",
    )

    sub.add_parser(
        "status",
        help="Estado consolidado del bot (DB + agent_runs + outbox + summaries).",
    )

    sp_mcp = sub.add_parser(
        "mcp-serve",
        help="Arranca el servidor MCP (stdio) para que kiro-cli orqueste el bot.",
    )
    sp_mcp.add_argument(
        "--no-lock",
        action="store_true",
        help="No tomar el lockfile (usar SOLO en tests in-process).",
    )

    args = parser.parse_args(argv)
    configure_logging(args.log_level or get_settings().log_level)

    if args.command == "init-db":
        return cmd_init_db(args)
    if args.command == "check-config":
        return cmd_check_config(args)
    if args.command == "dispatch":
        return cmd_dispatch(args)
    if args.command == "enqueue-sample":
        return cmd_enqueue_sample(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "telegram-check-config":
        return cmd_telegram_check_config(args)
    if args.command == "telegram-parse-sample":
        return cmd_telegram_parse_sample(args)
    if args.command in ("telegram-listen", "telegram-backfill"):
        # Normalizar args: backfill = listen --once con --limit.
        if args.command == "telegram-backfill":
            args.once = True
        return cmd_telegram_listen(args)
    if args.command in ("amazon-parse-url", "amazon-validate-url"):
        return cmd_amazon_parse_url(args)
    if args.command == "amazon-hunt":
        return cmd_amazon_hunt(args)
    if args.command == "revalidate-outbox":
        return cmd_revalidate_outbox(args)
    if args.command == "revalidate-url":
        return cmd_revalidate_url(args)
    if args.command == "ml-enrich-affiliates":
        return cmd_ml_enrich_affiliates(args)
    if args.command == "compress-memory":
        return cmd_compress_memory(args)
    if args.command == "export-memory-summary":
        return cmd_export_memory_summary(args)
    if args.command == "check-db":
        return cmd_check_db(args)
    if args.command == "audit-false-price-errors":
        return cmd_audit_false_price_errors(args)
    if args.command == "audit-amazon-captcha":
        return cmd_audit_amazon_captcha(args)
    if args.command == "amazon-captcha-check":
        return cmd_amazon_captcha_check(args)
    if args.command == "login":
        return cmd_login(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "mcp-serve":
        return cmd_mcp_serve(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
