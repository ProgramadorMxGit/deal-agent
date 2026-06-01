from __future__ import annotations

from ofertas_hunter.config import Settings


def test_settings_accepts_evolution_instance_name_alias():
    settings = Settings(
        EVOLUTION_INSTANCE_NAME="mi-numero",
        EVOLUTION_API_KEY_HEADER="apikey",
    )

    assert settings.evolution_instance == "mi-numero"
    assert settings.evolution_api_key_header == "apikey"


def test_settings_whatsapp_group_prefers_default_target_over_legacy_group_ids():
    settings = Settings(
        evolution_default_target="5215512345678",
        ofertas_whatsapp_group_id="120363@g.us",
        whatsapp_target_group_id="120999@g.us",
        whatsapp_channel_jid="0029example@newsletter",
    )

    assert settings.whatsapp_group == "5215512345678"


def test_settings_exposes_amazon_cookies_path():
    settings = Settings(AMAZON_COOKIES_PATH="secrets/amazon_cookies.json")

    assert settings.amazon_cookies_path == "secrets/amazon_cookies.json"
