"""Configuración global con Pydantic Settings.

Resuelve valores en este orden:

1. Variables de entorno (incluido `.env` cargado por dotenv).
2. Defaults definidos en cada `Settings`.

No se carga ningún YAML/JSON automáticamente: la lectura de seeds y selectores
es responsabilidad de cada marketplace (que lee de `config/seeds/*` y
`config/selectors/*`).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Configuración global del bot."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Runtime
    log_level: str = "INFO"
    env: str = "development"

    # Database
    db_path: str = "data/ofertas_hunter.db"

    # Amazon
    amazon_enabled: bool = True
    amazon_headless: bool = True
    amazon_max_pages_per_seed: int = 10
    amazon_delay_between_pages_ms_min: int = 5000
    amazon_delay_between_pages_ms_max: int = 12000
    # Sesión persistente Amazon (login manual del operador). Si está set,
    # Playwright usa `launch_persistent_context` y reutiliza el perfil
    # entre runs, manteniendo cookies, localStorage y session storage.
    amazon_user_data_dir: Optional[str] = "secrets/browser_profiles/amazon"
    amazon_cookies_path: str = "secrets/amazon_cookies.json"
    amazon_warmup_homepage: bool = False

    # Switch entre el hunter Amazon nuevo (`AmazonHunterAgent`) y el
    # adapter del scraper legacy `AmazonScrapperIA`
    # (`LegacyAmazonHunterAgent`). El legacy es más robusto contra
    # captchas (browser efímero + UA random + stealth + delays gaussianos).
    # El scoring final lo sigue haciendo `PriceErrorScorer` (bot nuevo).
    # Ver `docs/AMAZON_LEGACY_INTEGRATION.md`.
    amazon_hunter_legacy: bool = False

    # Mercado Libre
    mercadolibre_enabled: bool = True
    mercadolibre_headless: bool = True
    mercadolibre_cookies_path: str = "secrets/mercadolibre_cookies.json"
    # Path alternativo: si el operador prefiere reutilizar el example.
    mercadolibre_cookies_fallback_path: Optional[str] = "secrets/mercadolibre_cookies.example.json"
    mercadolibre_browser_cookies_path: Optional[str] = None
    mercadolibre_seeds_path: str = "config/seeds/mercadolibre.json"
    mercadolibre_max_concurrency: int = 1
    mercadolibre_request_delay_seconds: float = 6.0
    mercadolibre_backoff_seconds: int = 300
    mercadolibre_save_cookies_on_exit: bool = True
    mercadolibre_affiliate_required_for_publish: bool = True
    mercadolibre_affiliate_timeout_seconds: float = 15.0
    # Sesión persistente ML (login manual del operador). Cuando está set, el
    # bot reutiliza el perfil Chromium (cookies, localStorage, etc.) entre
    # runs. Las cookies del JSON quedan como fallback.
    mercadolibre_user_data_dir: Optional[str] = "secrets/browser_profiles/mercadolibre"
    bot_diversidad_global_cookies_path: Optional[str] = None
    ofertas_meli_browser_cookies_path: Optional[str] = None

    # Prefiltro de descuento en discovery ML: filtra productos por descuento
    # visible en la tarjeta del listing ANTES de abrir la página con Playwright
    # (caro). Reduce trabajo inútil en `hunt_from_frontier`. El filtro real
    # sigue en `hunt_one()` sobre la página del producto (fuente de verdad).
    mercadolibre_listing_discount_prefilter: bool = True
    # Si no se puede determinar el descuento en la tarjeta:
    #   strict=False → entra al frontier con score bajo (no perder oportunidades).
    #   strict=True  → se descarta.
    mercadolibre_listing_discount_strict: bool = False
    # Score asignado a productos con descuento desconocido (modo no estricto).
    mercadolibre_listing_unknown_discount_score: float = 1.0

    # ML session recovery: avisos al admin + webhook entrante para hot-reload
    # de cookies cuando ML detecta sesión expirada.
    # Ver `docs/ML_SESSION_RECOVERY.md`.
    ml_session_alert_enabled: bool = True
    ml_session_alert_channel: str = "whatsapp"
    # Lista CSV de números admin (sin '+'), por ejemplo
    # "528338498692,528112345678".
    ml_session_admin_numbers: str = ""
    ml_session_telegram_bot_token: Optional[str] = None
    ml_session_telegram_chat_id: Optional[str] = None
    ml_session_telegram_timeout_seconds: float = 15.0
    ml_session_telegram_poll_interval_seconds: int = 15
    # Cooldown entre avisos consecutivos al admin (default 30 min).
    ml_session_alert_cooldown_seconds: int = 1800
    # Servidor webhook entrante (Evolution API hace POST aquí cuando el
    # admin manda el JSON de cookies via WhatsApp).
    ml_session_inbound_enabled: bool = True
    ml_session_inbound_host: str = "127.0.0.1"
    ml_session_inbound_port: int = 9099
    # Token compartido para autenticar el webhook (Evolution lo manda
    # como header `X-Webhook-Secret`). Generado al deploy.
    ml_session_inbound_secret: Optional[str] = None
    # Cuántas versiones de cookies anteriores se preservan en disco.
    ml_session_cookie_backup_count: int = 5

    # ML session poller: pull periódico contra Evolution API para detectar
    # `/cookies_ml` cuando Evolution NO puede alcanzar el webhook local
    # (típico en deploys donde Evolution corre en otro servidor). Coexiste
    # con el webhook sin conflicto.
    ml_session_poller_enabled: bool = True
    ml_session_poller_interval_seconds: int = 15
    ml_session_poller_page_size: int = 20

    # Scheduler nocturno (hibernación + warmup)
    schedule_enabled: bool = True
    schedule_timezone: str = "America/Mexico_City"
    hibernate_start: str = "23:30"
    hibernate_end: str = "06:30"
    warmup_start: str = "06:30"
    active_start: str = "07:00"
    warmup_loop_interval_seconds: float = 90.0
    hibernation_check_interval_seconds: float = 60.0

    # Telegram
    telegram_enabled: bool = False
    telegram_api_id: Optional[int] = None
    telegram_api_hash: Optional[str] = None
    # Compat con env vars del legacy:
    ofertas_telegram_api_id: Optional[int] = None
    ofertas_telegram_api_hash: Optional[str] = None
    telegram_session_path: str = "secrets/telegram.session"
    telegram_channels: str = ""  # comma separated (legacy name)
    telegram_target_channels: str = ""  # nuevo nombre, prioritario
    telegram_backfill_limit_per_channel: int = 400
    telegram_backfill_process_budget_per_channel: int = 40
    telegram_channel_workers: int = 3
    telegram_poll_interval_seconds: int = 30
    telegram_ignore_mercadolibre_links: bool = True
    telegram_link_resolver_timeout_seconds: float = 6.0
    telegram_link_resolver_max_redirects: int = 5
    # Modo automático: no ingerir historial al arrancar; marcar cursor en el
    # mensaje más reciente y procesar sólo mensajes posteriores.
    telegram_start_from_now: bool = True

    # WhatsApp / Evolution
    whatsapp_enabled: bool = False
    evolution_base_url: Optional[str] = None
    evolution_api_key: Optional[str] = None
    evolution_api_key_header: str = "apikey"
    evolution_instance: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("EVOLUTION_INSTANCE", "EVOLUTION_INSTANCE_NAME"),
    )
    evolution_instance_id: Optional[str] = None
    evolution_instance_token: Optional[str] = None
    evolution_version: Optional[str] = None
    evolution_manager_ui: Optional[str] = None
    evolution_docs_url: Optional[str] = None
    evolution_default_target_type: Optional[str] = None
    evolution_default_target: Optional[str] = None
    whatsapp_target_group_id: Optional[str] = None
    # Alias requerido por la spec §3 (acepta ambos nombres)
    ofertas_whatsapp_group_id: Optional[str] = None
    whatsapp_channel_name: Optional[str] = None
    whatsapp_channel_jid: Optional[str] = None
    whatsapp_channel_link: Optional[str] = None
    whatsapp_cooldown_seconds: int = 300
    whatsapp_outbox_revalidate_age_seconds: int = 3600

    # Dispatcher
    publishing_enabled: bool = False
    publishing_dry_run: bool = True
    dispatcher_idle_sleep_seconds: float = 5.0
    # Throttle incondicional del loop de Mercado Libre en el orquestador
    # (anti-runaway). Sin esto el loop gira a ~20 ciclos/s cuando el
    # frontier está lleno. 5s ≈ 720 ciclos/h máx; 10s ≈ 360 ciclos/h.
    ml_loop_sleep_seconds: float = 5.0

    # Mantenimiento nocturno (purga runtime_events + VACUUM en hibernación).
    # Ver `src/ofertas_hunter/maintenance/nightly.py`. La ventana cae dentro
    # de la hibernación del scheduler (no se publica nada).
    nightly_maintenance_enabled: bool = True
    nightly_maintenance_start: str = "03:00"
    nightly_maintenance_end: str = "04:30"
    nightly_maintenance_timezone: str = "America/Mexico_City"
    runtime_events_keep_mcp_tool_called_hours: int = 48
    runtime_events_keep_verbose_days: int = 7
    nightly_vacuum_enabled: bool = True
    nightly_vacuum_min_free_gb: float = 10.0
    nightly_maintenance_batch_size: int = 100000
    nightly_maintenance_restart_after: bool = True
    nightly_maintenance_max_seconds: int = 3600
    nightly_maintenance_create_backup: bool = False
    nightly_maintenance_backup_retention: int = 1
    # Nombre del servicio systemd principal que el mantenimiento detiene/
    # reinicia. Si no existe en systemd, el modo automático no se activa.
    nightly_maintenance_service_name: str = "ofertas-hunter.service"
    # Si el mantenimiento corre como usuario sin privilegios (no root),
    # `systemctl stop/start` necesita `sudo -n` (sudo sin password).
    nightly_maintenance_use_sudo: bool = True

    # LLM
    llm_heal_enabled: bool = False
    llm_backend: str = "none"  # kiro_cli | anthropic | none
    anthropic_api_key: Optional[str] = None
    kiro_cli_path: Optional[str] = None

    # Diversity Curator (selección IA del próximo item del outbox).
    # Cuando `diversity_curator_enabled=False` el dispatcher mantiene el
    # comportamiento legacy (`pick_random_eligible`). Cuando `True`, el
    # curator se inyecta como `item_selector` en el OutboxDispatcher.
    # Si `diversity_curator_use_llm=False`, el curator funciona en modo
    # determinístico (top1 del scorer) — no requiere kiro-cli.
    diversity_curator_enabled: bool = False
    diversity_curator_use_llm: bool = True
    diversity_curator_history_size: int = 10
    diversity_curator_candidate_limit: int = 10
    diversity_curator_llm_timeout_seconds: float = 30.0
    # Override explícito al binario kiro-cli del curator (separado del
    # `kiro_cli_path` global del LLM healer, por si el operador quiere
    # un binario distinto). Si está vacío usa la auto-detección estándar.
    diversity_curator_kiro_cli_path: Optional[str] = None

    # Scoring thresholds
    price_error_threshold_confirmed: int = 80
    price_error_threshold_possible: int = 60
    price_error_threshold_suspicious: int = 40
    normal_offer_min_discount: float = 50.0

    # Amazon publish guardrails
    # Si True, ningún item Amazon SIN affiliate_url válido (amzn.to/ o tag=)
    # puede publicarse. Y las ofertas normales Amazon exigen precio anterior
    # verificado (old_price_verified) — nunca se inventa "Antes".
    amazon_affiliate_required_for_publish: bool = True
    amazon_min_discount_percent: float = 50.0
    # Guardrail anti falsos-positivos de precio (precio por unidad, descuentos
    # extremos sin verificar). Ver whatsapp_publisher._amazon_gate.
    amazon_extreme_discount_threshold: float = 90.0
    amazon_min_absolute_price: float = 10.0
    # Enriquecimiento automático de afiliados Amazon en el loop.
    amazon_affiliate_enrich_limit: int = 5
    amazon_affiliate_enrich_timeout_seconds: float = 90.0

    # Helpers
    @property
    def db_path_resolved(self) -> Path:
        path = Path(self.db_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    @property
    def telegram_channel_list(self) -> list[str]:
        raw = self.telegram_target_channels or self.telegram_channels
        return [c.strip() for c in raw.split(",") if c.strip()]

    @property
    def resolved_telegram_api_id(self) -> Optional[int]:
        return self.telegram_api_id or self.ofertas_telegram_api_id

    @property
    def resolved_telegram_api_hash(self) -> Optional[str]:
        return self.telegram_api_hash or self.ofertas_telegram_api_hash

    @property
    def telegram_session_path_resolved(self) -> Path:
        path = Path(self.telegram_session_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    @property
    def whatsapp_group(self) -> Optional[str]:
        """Devuelve el destino activo de WhatsApp.

        Prioridad:
        1. `EVOLUTION_DEFAULT_TARGET`
        2. `OFERTAS_WHATSAPP_GROUP_ID`
        3. `WHATSAPP_TARGET_GROUP_ID`

        Los campos `WHATSAPP_CHANNEL_*` son sólo referencia administrativa y
        no se usan como destino activo de publicación.
        """
        return (
            self.evolution_default_target
            or self.ofertas_whatsapp_group_id
            or self.whatsapp_target_group_id
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve la instancia única de Settings (cacheada)."""
    return Settings()
