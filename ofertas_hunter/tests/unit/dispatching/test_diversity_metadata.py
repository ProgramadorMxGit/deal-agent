"""Tests de inferencia/normalización de metadata de diversidad.

Cubre criterios del spec:
A. payload.category="vender uno igual" se ignora.
B. "BHP Ultra Whey..." infiere proteína/suplementos.
C. "Robot Aspirador..." infiere tecnología.
D. marca BHP se normaliza.
E. sabores distintos del mismo whey → misma product_family.
H (parte): fuzzy title >= 0.85 entre variantes.
"""
from __future__ import annotations

from ofertas_hunter.dispatching.diversity_metadata import (
    infer_offer_category,
    normalize_brand,
    normalize_title,
    product_family,
    title_fingerprint,
    title_similarity,
)


# A — categoría basura ignorada
def test_A_garbage_category_ignored_uses_title():
    res = infer_offer_category(
        "Proteina Bhp Ultra Whey 2.27 Kg",
        {"category": "vender uno igual"},
        "mercadolibre",
        "mercadolibre_hunter",
    )
    assert res.normalized == "proteina/suplementos"
    assert res.source == "title"
    assert res.raw == "vender uno igual"


def test_A_other_garbage_values():
    for garbage in ["ver más", "otros", "", "Ver Todos", "Mas Vendidos"]:
        res = infer_offer_category("Cosa rara sin palabra clave", {"category": garbage})
        # sin keyword en título y categoría basura → fallback "otro"
        assert res.normalized == "otro"
        assert res.source == "fallback"


# B — whey infiere proteína
def test_B_whey_infers_protein():
    titles = [
        "BHP Ultra Whey 2.27Kg Bote Vainilla",
        "Proteina 100% Whey 2.2 Kg Adicionada Con Creatina",
        "43 Whey Protein 3 Kg Cookies And Cream",
        "Bhp Iso Protein Fresh 20 Serv Alta Pureza",
        "Suplemento En Polvo Bhp Nutrition Proteína",
    ]
    for t in titles:
        assert infer_offer_category(t, {}).normalized == "proteina/suplementos", t


# C — robot aspirador infiere tecnología
def test_C_robot_infers_tech():
    assert infer_offer_category("Bluelander Robot Aspirador 2 en 1", {}).normalized == "tecnologia"
    assert infer_offer_category("RCA Aspiradora Canister Echo RC-A2", {}).normalized in (
        "tecnologia", "hogar",
    )
    assert infer_offer_category("Laptop HP Core i5 16GB SSD", {}).normalized == "tecnologia"


def test_C_more_categories():
    assert infer_offer_category("Andadera Ortopedica Rollator", {}).normalized in ("bebe", "salud")
    assert infer_offer_category("Gorra Puma Liga Cap", {}).normalized in ("ropa", "calzado")
    assert infer_offer_category("Serum Facial Nivea Anti Manchas", {}).normalized == "belleza"
    assert infer_offer_category("Sartén de Teflón 28cm", {}).normalized == "hogar"
    assert infer_offer_category("Tenis Nike Running", {}).normalized == "calzado"


def test_sunscreen_with_vitamin_c_is_belleza_not_protein():
    """Regresión: un protector solar con 'Vitamina C' NO debe inferirse como
    suplemento (la regla belleza tiene prioridad sobre la de suplementos)."""
    title = "Protector Solar Corporal NIVEA SUN Protección Fps50+ 200Ml Con Vitamina C Y Ácido Hialurónico"
    assert infer_offer_category(title, {"category": "Filtro Solar Corporal"}).normalized == "belleza"
    assert infer_offer_category("Avene Cleanance Protector Solar FPS 50+ con Color", {}).normalized == "belleza"


def test_real_supplement_still_protein():
    """Un suplemento real de vitaminas sí debe seguir siendo proteína/suplementos."""
    assert infer_offer_category("Multivitaminico Centrum 100 Tabletas", {}).normalized == "proteina/suplementos"
    assert infer_offer_category("Creatina Monohidratada 300g", {}).normalized == "proteina/suplementos"


# D — marca BHP normaliza
def test_D_brand_bhp_normalizes():
    assert normalize_brand("bhp", "Proteina Bhp Ultra Whey") == "bhp"
    assert normalize_brand("BHP Nutrition", "x") == "bhp"
    assert normalize_brand(None, "Proteina Bhp Ultra Whey 2.27 Kg") == "bhp"


def test_D_brand_43_supplements():
    assert normalize_brand("43", "43 Whey Protein 3 Kg") == "43 supplements"
    assert normalize_brand(None, "43 Proteina Zero Hidrolizada 6 Kg") == "43 supplements"


def test_D_brand_none_for_generic():
    assert normalize_brand(None, "Proteina Generica Sin Marca") is None


def test_D_brand_strips_de_prefix():
    """Regresión: 'de ISDIN' debe normalizar a 'isdin', no 'de isdin'."""
    assert normalize_brand("de ISDIN", "Protector Solar de ISDIN Fusion") == "isdin"
    assert normalize_brand("ISDIN", "ISDIN Fotoprotector") == "isdin"
    assert normalize_brand(None, "ISDIN Fotoprotector Fusion Water") == "isdin"
    assert normalize_brand("Marca Vichy", "Vichy Protector Solar") == "vichy"


# E — variantes de sabor → misma familia
def test_E_flavor_variants_same_family():
    base = [
        "Proteina Bhp Ultra Whey Ultra 2.27 Kg 5 Lbs Bote Sabor Vainilla",
        "Proteina Bhp Ultra Whey Ultra 2.27 Kg 5 Lbs Bote Sabor Cookies And Cream",
        "Proteina Bhp Ultra Whey Ultra 2.27 Kg 5 Lbs Bote Chocolate",
    ]
    fams = {product_family(t, "bhp") for t in base}
    assert len(fams) == 1, fams


def test_E_different_products_different_family():
    f1 = product_family("BHP Ultra Whey 2.27 Kg", "bhp")
    f2 = product_family("Laptop HP Core i5 16GB", "hp")
    assert f1 != f2


def test_E_43_zero_variants_same_family():
    f = {
        product_family("43 Proteina Zero Hidrolizada 6 Kg Vainilla 43 Supplements", "43"),
        product_family("43 Proteina Zero Hidrolizada 6 Kg Galleta 43 Supplements", "43"),
        product_family("43 Proteina Zero Hidrolizada 6 Kg Platano 43 Supplements", "43"),
    }
    assert len(f) == 1, f


# H (parte) — similitud fuzzy de variantes
def test_H_variant_titles_similar_above_threshold():
    a = "Proteina Bhp Ultra Whey Ultra 2.27 Kg 5 Lbs Bote Sabor Vainilla"
    b = "Proteina Bhp Ultra Whey Ultra 2.27 Kg 5 Lbs Bote Chocolate"
    assert title_similarity(a, b) >= 0.85


def test_H_distinct_products_low_similarity():
    a = "BHP Ultra Whey 2.27 Kg Vainilla"
    b = "Laptop HP Core i5 16GB SSD 512"
    assert title_similarity(a, b) < 0.85


def test_normalize_title_strips_accents_and_punct():
    assert normalize_title("Proteína 100% Whey, ¡Súper!") == "proteina 100 whey super"


def test_title_fingerprint_drops_flavor():
    fp1 = title_fingerprint("BHP Whey Bote Vainilla")
    fp2 = title_fingerprint("BHP Whey Bote Chocolate")
    assert fp1 == fp2
