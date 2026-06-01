"""Tests del parser de Telegram.

Cubre los criterios obligatorios de la spec §16:
- test_telegram_mercadolibre_links_are_ignored
- test_telegram_urgency_terms_increase_score (en signal_extractor)
"""

from __future__ import annotations

from datetime import datetime, timezone

from ofertas_hunter.telegram.message_parser import parse_message
from ofertas_hunter.telegram.signal_extractor import extract_urgency


def _now():
    return datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc)


class TestMessageParser:
    def test_telegram_mercadolibre_links_are_ignored(self):
        text = (
            "Tablet Samsung Galaxy Tab A9\n"
            "Mercado Libre - $2,499 con cupón\n"
            "https://meli.la/abc123"
        )
        parsed = parse_message(text, channel="ofertonesmexico", message_id=1, captured_at=_now())
        assert parsed.skip_reason == "mercadolibre_link"
        assert parsed.marketplace == "mercadolibre"

    def test_mercadolibre_link_without_keyword_still_ignored(self):
        text = "Producto random\nhttps://www.mercadolibre.com.mx/p/MLM12345"
        parsed = parse_message(text, channel="x", message_id=2, captured_at=_now())
        assert parsed.skip_reason == "mercadolibre_link"

    def test_walmart_link_processed_with_urgency_terms(self):
        text = (
            "ERROR DE PRECIO???\n"
            "Laptop HP EliteBook 840 G6 Intel Core i7 32GB RAM 512GB SSD\n"
            "Walmart - $2,349\n"
            "🚨🔥 CORRAN!!!\n"
            "https://bit.ly/abc"
        )
        parsed = parse_message(text, channel="ofertonesmexico", message_id=3, captured_at=_now())
        assert parsed.skip_reason is None
        assert parsed.marketplace == "walmart"
        assert parsed.brand == "hp"
        assert parsed.category == "laptop"
        assert parsed.is_price_error_keyword is True
        assert parsed.urgency_score >= 35
        assert parsed.written_price == 2349.0
        assert any("CORRAN" in t.upper() for t in parsed.urgency_terms)

    def test_no_link_marked_as_skip(self):
        text = "Solo texto sin link"
        parsed = parse_message(text, channel="x", message_id=4, captured_at=_now())
        assert parsed.skip_reason == "no_link"

    def test_amazon_de_x_a_y_uses_final_price(self):
        text = (
            "Samsung Galaxy S25 Ultra Azul 256GB con S-Pen\n"
            "‼️50% DE DESCUENTO‼️\n"
            "DE $32,999.00 A $11,499.00 🔥🔥🔥🔥\n"
            "✅ Cupón: BNMXHOT30\n"
            "https://amzn.to/43HYadf\n"
            "👉Compra mínima $12,500 mxn\n"
            "▶️Max descuento $5,000 mxn"
        )
        parsed = parse_message(text, channel="ofertas", message_id=5, captured_at=_now())
        assert parsed.marketplace == "amazon"
        assert parsed.discount_visible == 50.0
        assert parsed.written_price == 11499.0

    def test_amazon_precio_oferta_mas_cupon_prefers_coupon_price(self):
        text = (
            "Amazon: Samsung Galaxy Tab S10+ Plata 12GB RAM 256GB, AI, S Pen, 1 año con Garantía\n"
            "👉Enlace:\n"
            "https://www.amazon.com.mx/dp/B0DF1FBWMF\n"
            "🔥Precio Oferta + ✅Cupón Bancario: $12,499.00\n"
            "🔥Precio Oferta: $12,999.00\n"
            "▶️Cupones de 10% ó 15% ó 20%\n"
            "✅AMAZON ACCESS INVEX:\n"
            "Cupón de $200: ACCESSHOT150"
        )
        parsed = parse_message(text, channel="ofertas", message_id=6, captured_at=_now())
        assert parsed.marketplace == "amazon"
        assert parsed.written_price == 12499.0

    def test_amazon_ignores_min_purchase_and_cap_amounts_as_main_price(self):
        text = (
            "Amazon:Midea Refrigerador Side by Side, 20 pies, Glass Door Black, MDRS710FGM22\n"
            "👉Enlace:\n"
            "https://www.amazon.com.mx/dp/B0GSBXM7TW\n"
            "✅Cupón de 30% pagando de contado o a Meses sin intereses con Tarjeta de crédito BANAMEX:\n"
            "BNMXHOT30\n"
            "➡️Compra mínima $12,500, ▶️Tope de descuento $5,000\n"
            "🔥Precio Oferta + ✅Cupón 30% \"BNMXHOT30\": $10,188.66"
        )
        parsed = parse_message(text, channel="ofertas", message_id=7, captured_at=_now())
        assert parsed.marketplace == "amazon"
        assert parsed.written_price == 10188.66


class TestUrgencyExtractor:
    def test_telegram_urgency_terms_increase_score(self):
        """spec §16: las urgencias incrementan el score frente a un texto neutro."""
        neutral = extract_urgency("Producto random a buen precio")
        urgent = extract_urgency("🚨🔥 ERROR DE PRECIO 🚨 CORRAN A SOLO 999 ‼️")
        assert neutral.score == 0
        assert urgent.score >= 35
        assert urgent.has_price_error_keyword is True

    def test_price_error_phrasing_variants(self):
        for variant in [
            "ERROR DE PRECIO",
            "ERROR DE PRECIOOO",
            "ERROR DE PRECIO???",
            "POSIBLE ERROR DE PRECIO",
            "OTRO ERROR DE PRECIO",
        ]:
            sig = extract_urgency(variant)
            assert sig.has_price_error_keyword is True, variant

    def test_emojis_only_bonus_when_three_plus(self):
        few = extract_urgency("Algo barato 🔥")
        many = extract_urgency("Algo barato 🔥🚨⚡")
        assert few.score == 0
        assert many.score == 5


class TestParserFixtures:
    """Cobertura sobre los 14 fixtures de mensajes reales."""

    fixtures_dir = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "fixtures" / "telegram"
    )

    @staticmethod
    def _load(name: str) -> str:
        from pathlib import Path

        return Path(
            TestParserFixtures.fixtures_dir / name
        ).read_text(encoding="utf-8")

    def test_telegram_detects_price_error_terms(self):
        for name in [
            "iphone_16_pro_max_liverpool_3899.txt",
            "msi_coppel_2719.txt",
            "dell_pro_16_1544.txt",
            "asus_vivobook_sams_305.txt",
            "galaxy_a32_walmart_197.txt",
            "laptop_hp_elitebook_walmart_2349.txt",
        ]:
            text = self._load(name)
            parsed = parse_message(text, "x", 1, _now())
            assert parsed.is_price_error_keyword is True, name

    def test_telegram_detects_urgency_terms(self):
        text = self._load("iphone_16_pro_max_liverpool_3899.txt")
        parsed = parse_message(text, "x", 1, _now())
        joined = " ".join(parsed.urgency_terms).upper()
        assert "CORRAN" in joined or "A SOLO" in joined
        assert parsed.urgency_score >= 25

    def test_telegram_extracts_price_from_message(self):
        cases = {
            "asus_vivobook_sams_305.txt": 305.88,
            "galaxy_s24_sears_1399.txt": 1399.0,
            "iphone_16_pro_max_liverpool_3899.txt": 3899.0,
            "galaxy_a32_walmart_197.txt": 197.0,
            "ipad_mini_sams_4999.txt": 4999.0,
        }
        for name, expected in cases.items():
            text = self._load(name)
            parsed = parse_message(text, "x", 1, _now())
            assert parsed.written_price == expected, (name, parsed.written_price)

    def test_telegram_extracts_discount_percent(self):
        text = self._load("coofandy_amazon_91_percent.txt")
        parsed = parse_message(text, "x", 1, _now())
        assert parsed.discount_visible == 91.0

    def test_telegram_extracts_store_guess(self):
        cases = {
            "iphone_16_pro_max_liverpool_3899.txt": "liverpool",
            "msi_coppel_2719.txt": "coppel",
            "asus_vivobook_sams_305.txt": "sams_club",
            "airpods_officedepot_599.txt": "office_depot",
            "galaxy_s24_sears_1399.txt": "sears",
            "dell_pro_16_1544.txt": "dell",
            "sony_wf1000xm5.txt": "sony_store",
            "laptop_hp_elitebook_walmart_2349.txt": "walmart",
            "galaxy_a32_walmart_197.txt": "walmart",
            "ipad_coppel_2611.txt": "coppel",
            "ipad_mini_sams_4999.txt": "sams_club",
            "gigabyte_b450_amazon_611.txt": "amazon",
            "coofandy_amazon_91_percent.txt": "amazon",
        }
        for name, expected_store in cases.items():
            text = self._load(name)
            parsed = parse_message(text, "x", 1, _now())
            assert parsed.marketplace == expected_store, (name, parsed.marketplace)

    def test_telegram_extracts_hashtags(self):
        text = self._load("gigabyte_b450_amazon_611.txt")
        parsed = parse_message(text, "x", 1, _now())
        assert "amazon" in parsed.hashtags
        assert "pc" in parsed.hashtags

    def test_telegram_mercadolibre_fixture_marked_as_skip(self):
        text = self._load("mercadolibre_link_should_be_ignored.txt")
        parsed = parse_message(text, "x", 1, _now())
        assert parsed.skip_reason == "mercadolibre_link"
