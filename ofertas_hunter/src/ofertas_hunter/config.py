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

from pydantic import Field
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
    amazon_warmup_homepage: bool = False

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

    # WhatsApp / Evolution
    whatsapp_enabled: bool = False
    evolution_base_url: Optional[str] = None
    evolution_api_key: Optional[str] = None
    evolution_instance: Optional[str] = None
    whatsapp_target_group_id: Optional[str] = None
    # Alias requerido por la spec §3 (acepta ambos nombres)
    ofertas_whatsapp_group_id: Optional[str] = None
    whatsapp_cooldown_seconds: int = 300
    whatsapp_outbox_revalidate_age_seconds: int = 3600

    # Dispatcher
    publishing_enabled: bool = False
    publishing_dry_run: bool = True
    dispatcher_idle_sleep_seconds: float = 5.0

    # LLM
    llm_heal_enabled: bool = False
    llm_backend: str = "none"  # kiro_cli | anthropic | none
    anthropic_api_key: Optional[str] = None
    kiro_cli_path: Optional[str] = None

    # Scoring thresholds
    price_error_threshold_confirmed: int = 80
    price_error_threshold_possible: int = 60
    price_error_threshold_suspicious: int = 40
    normal_offer_min_discount: float = 50.0

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
        """Devuelve el JID del grupo, prefiriendo OFERTAS_WHATSAPP_GROUP_ID
        (nombre exigido en la spec §3) y cayendo en WHATSAPP_TARGET_GROUP_ID
        para compatibilidad."""
        return self.ofertas_whatsapp_group_id or self.whatsapp_target_group_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve la instancia única de Settings (cacheada)."""
    return Settings()
