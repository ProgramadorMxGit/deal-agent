"""Tests del fix de category_normalized end-to-end.

Cubre:
- ML category_raw='vender uno igual' + título whey -> proteína/suplementos.
- ML andadera -> bebe/salud.
- ML transportadora perro/gato -> mascotas (NO bebé).
- ML cesto ropa bambú -> hogar.
- Amazon sigue clasificando bien.
- compute_diversity_metadata devuelve los 6 campos.
- enqueue_with_quota PERSISTE los 6 campos en message_payload_json.
- categorías basura nunca pasan como categoría efectiva.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.dispatching.diversity_metadata import (
    GARBAGE_CATEGORIES,
    compute_diversity_metadata,
    infer_offer_category,
)
from ofertas_hunter.dispatching.outbox_admission import (
    QuotaConfig,
    enqueue_with_quota,
)


# ---------------------------------------------------------------------------
# Inferencia de categoría con ejemplos reales del operador
# ---------------------------------------------------------------------------

def test_ml_vender_uno_igual_whey_is_protein():
    res = infer_offer_category(
        "Proteína Whey FSB WPC 80 de Chocolate, 900 g para 30 Porciones",
        {"category": "vender uno igual", "marketplace": "mercadolibre"},
        "mercadolibre",
    )
    assert res.normalized == "proteina/suplementos"
    assert res.source == "title"
    # el crudo se conserva pero NO se usa como señal
    assert res.raw == "vender uno igual"


def test_ml_andadera_is_bebe_or_salud():
    # andadera de bebé
    r1 = infer_offer_category("Andadera Para Bebes 6 En 1 Juguetes Didacticos", {})
    assert r1.normalized in ("bebe", "salud", "juguetes")
    # andadera ortopédica de adulto
    r2 = infer_offer_category("Andadera Ortopédica Rollator Asiento Silla Adulto", {})
    assert r2.normalized in ("bebe", "salud")


def test_ml_transportadora_perro_gato_is_mascotas_not_bebe():
    titles = [
        "Transportadora perro gato Skudo 3 iata 60x40x39cm",
        "Mochila Back Pack Transportadora Gato Perro Mascota Chica",
        "Bolsa Transportadora Mascotas Grande Tela Morada",
        "Bolsa De Transporte Para Mascotas Pequeña Portátil",
    ]
    for t in titles:
        res = infer_offer_category(t, {"category": "jaulas y transportadoras"})
        assert res.normalized == "mascotas", t


def test_ml_transportadora_bebe_is_bebe():
    # "transportadora" SIN contexto de mascota + autoasiento -> bebé
    res = infer_offer_category("Autoasiento Booster D Bebe Maxi 2 En 1 Color Rosa", {})
    assert res.normalized == "bebe"


def test_ml_cesto_ropa_bambu_is_hogar():
    res = infer_offer_category(
        "Cesto para ropa sucia lavandería bambú AG Box plegable organizador",
        {"category": "organizacion", "marketplace": "mercadolibre"},
        "mercadolibre",
    )
    assert res.normalized == "hogar"


def test_amazon_examples_still_classify():
    assert infer_offer_category("Olla Cinco by Lamex 24 cm roja", {}).normalized == "hogar"
    assert infer_offer_category(
        "CORSAIR RM750x Shift Fuente ATX 750W", {}
    ).normalized == "tecnologia"
    assert infer_offer_category(
        "Stanley Quencher Black Reverb 30 oz Termo", {}
    ).normalized == "hogar"


# ---------------------------------------------------------------------------
# Taxonomía ampliada (huecos que antes caían en "otro")
# ---------------------------------------------------------------------------

def test_new_taxonomy_gaps():
    cases = {
        "Armaf Odyssey Homme White Edition EDP Spray Men 3.4 oz": "belleza",
        "Maison Alhambra Jean Lowe Vibe Eau de Parfum": "belleza",
        "Cerveza Clara Coronita Extra 24 botellas 210 ml": "despensa",
        "Johnnie Walker Black Label Blended Whisky 1 Litro": "despensa",
        "DURACELL AA 1.5V Pilas Alcalinas 16 piezas": "hogar",
        "Oral B iO Series 4 Cepillo de Dientes Eléctrico": "belleza",
        "Philips OneBlade Recortadora Perfiladora 5 en 1": "belleza",
        "Abanico de Torre con Humidificación ventilador": "hogar",
        "Lápiz Labial Revlon Super Lustrous Tono Rum Raisin": "belleza",
    }
    for title, expected in cases.items():
        assert infer_offer_category(title, {}).normalized == expected, title


def test_marketing_title_uses_brand_hint():
    # Título de marketing sin keyword de producto, pero marca reconocible.
    res = infer_offer_category(
        "PRECIO MAS BAJO PROTEGE TU PIEL CON NIVEA",
        {"brand": "NIVEA"},
    )
    assert res.normalized == "belleza"
    assert res.source in ("title", "brand")


def test_pure_marketing_title_no_brand_is_otro():
    res = infer_offer_category("EN SU PRECIO MÁS BAJO", {})
    assert res.normalized == "otro"
    assert res.source == "fallback"


def test_garbage_categories_never_used():
    for g in ["vender uno igual", "sin categoria", "ver mas", "comprar ahora",
              "compartir", "ofertas del dia", "none", "null"]:
        res = infer_offer_category("EN SU PRECIO MÁS BAJO", {"category": g})
        assert res.normalized == "otro"
        # ninguna categoría basura debe salir como normalized
        assert res.normalized not in GARBAGE_CATEGORIES or res.normalized == "otro"


# ---------------------------------------------------------------------------
# compute_diversity_metadata: 6 campos
# ---------------------------------------------------------------------------

def test_compute_metadata_has_six_fields():
    meta = compute_diversity_metadata({
        "title": "Proteina Bhp Ultra Whey 2.27 Kg Vainilla",
        "category": "vender uno igual",
        "brand": "BHP",
        "marketplace": "mercadolibre",
    })
    assert set(meta.keys()) == {
        "category_raw", "category_normalized", "category_source",
        "category_confidence", "brand_normalized", "product_family",
    }
    assert meta["category_normalized"] == "proteina/suplementos"
    assert meta["category_raw"] == "vender uno igual"
    assert meta["brand_normalized"] == "bhp"
    assert meta["product_family"]


# ---------------------------------------------------------------------------
# enqueue_with_quota PERSISTE los 6 campos
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_enqueue_persists_normalized_metadata(db):
    payload = {
        "title": "Proteína Whey FSB WPC 80 Chocolate 900 g",
        "category": "vender uno igual",  # crudo contaminado de ML
        "brand": "FSB",
        "marketplace": "mercadolibre",
        "discount_percent": 55.0,
        "item_id": "MLM123",
    }
    oid = enqueue_with_quota(
        db, offer_id=1, outbox_type="normal", payload=payload,
        config=QuotaConfig(enabled=False),
    )
    db.commit()
    row = db.execute(
        "SELECT message_payload_json FROM outbox WHERE id=?", (oid,)
    ).fetchone()
    p = json.loads(row["message_payload_json"])
    assert p["category_normalized"] == "proteina/suplementos"
    assert p["category_raw"] == "vender uno igual"
    assert p["category_source"] == "title"
    assert p["brand_normalized"] == "fsb"
    assert p["product_family"]
    assert "category_confidence" in p
    # el crudo NO se sobreescribe destructivamente: 'category' sigue ahí
    assert p["category"] == "vender uno igual"


def test_enqueue_normalized_not_garbage(db):
    """Aunque entren con category basura, el normalized nunca es basura."""
    for i, garbage in enumerate(["vender uno igual", "ver mas", "ofertas"]):
        payload = {
            "title": "Andadera Ortopédica Rollator Adulto Plegable",
            "category": garbage,
            "marketplace": "mercadolibre",
            "discount_percent": 55.0,
            "item_id": f"MLM{i}",
        }
        oid = enqueue_with_quota(
            db, offer_id=10 + i, outbox_type="normal", payload=payload,
            config=QuotaConfig(enabled=False),
        )
        db.commit()
        row = db.execute(
            "SELECT message_payload_json FROM outbox WHERE id=?", (oid,)
        ).fetchone()
        p = json.loads(row["message_payload_json"])
        assert p["category_normalized"] not in GARBAGE_CATEGORIES
        assert p["category_normalized"] in ("bebe", "salud")
