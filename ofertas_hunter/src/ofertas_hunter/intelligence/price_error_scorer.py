"""Scorer determinista de errores de precio.

Implementa la lógica completa documentada en `docs/PRICE_ERROR_DETECTION.md`.
Es **determinista** y **sin dependencias externas**: dado un `PriceErrorSignal`
devuelve siempre el mismo `ScoringResult`. Eso permite tests sólidos contra
los ejemplos A-M de la spec.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable, Optional

from ..models import (
    Classification,
    Condition,
    PriceErrorSignal,
    ScoringResult,
)
from .accessory_detector import AccessoryAssessment, assess_title
from .category_ranges import HIGH_VALUE_CATEGORIES, PREMIUM_BRANDS, get_range


# --- Constantes de scoring (en línea con la spec §2.3) -----------------------

PREMIUM_BELOW_20PCT_RANGE = 35
HISTORICAL_DROP_70PCT = 35
HISTORICAL_DROP_60_70 = 20
DISCOUNT_VISIBLE_80 = 30
DISCOUNT_VISIBLE_90 = 10  # bonus extra cuando el descuento >= 90%
TELEGRAM_ERROR_KEYWORD = 25
TELEGRAM_URGENCY_KEYWORD = 10
TELEGRAM_EMOJIS_BONUS = 5
PREMIUM_BRAND_ANOMALY = 20
PREMIUM_BRAND_STRONG_ANOMALY_VS_HISTORY = 25
LAPTOP_BELOW_4000 = 30
SMARTPHONE_FLAGSHIP_BELOW_5000 = 35
AUDIO_FLAGSHIP_BELOW_1000 = 25
TABLET_APPLE_BELOW_5000 = 25
COMBO_LAPTOP_BELOW_5000 = 30
DIGIT_MISSING = 20
PREVIOUS_VS_CURRENT_70PCT = 20
PREVIOUS_VS_CURRENT_60_70 = 15
PC_COMPONENT_HISTORICAL_DROP_60 = 10

# Reducciones
USED_PRODUCT = -25
NO_STOCK = -50
LINK_NOT_VALIDATED = -30
COUPON_DEPENDENT = -15
GENERIC_BRAND = -10
EXTERNAL_SELLER_SUSPECT = -15
ACCESSORY_PRICE = -40
VARIANT_MISMATCH = -35

# Penalizaciones nuevas (REGLA 5): accesorios genéricos publicados como
# "ERROR DE PRECIO" en ML.
GENERIC_ACCESSORY_PENALTY = -45
COMPATIBLE_WITH_PREMIUM_PENALTY = -30

# Cap superior cuando se detecta accesorio genérico (REGLA 5). Se aplica
# después de calcular el score bruto. Si hay caída histórica >= 85% sí se
# permite superar este cap.
GENERIC_ACCESSORY_SCORE_CAP = 55  # → como mucho `suspicious_deal`

# Termonología
PRICE_ERROR_TERMS = [
    re.compile(r"\bERROR\s+DE\s+PRECIO+\b[\?\!]*", re.IGNORECASE),
    re.compile(r"\bPOSIBLE\s+ERROR\s+DE\s+PRECIO\b", re.IGNORECASE),
    re.compile(r"\bOTRO\s+ERROR\s+DE\s+PRECIO\b", re.IGNORECASE),
]
URGENCY_TERMS = [
    re.compile(r"\bCORR[A]+N\b", re.IGNORECASE),
    re.compile(r"\bDEJA\s+PEDIR\b", re.IGNORECASE),
    re.compile(r"\bNO\s+CANCELAN\b", re.IGNORECASE),
    re.compile(r"\b(EN|A)\s+SOLO\b", re.IGNORECASE),
    re.compile(r"\bSALE\s+EN\b", re.IGNORECASE),
    re.compile(r"\bPRECIO\s+M[AÁ]S\s+BAJO\b", re.IGNORECASE),
]
URGENCY_EMOJIS = re.compile(r"[🚨🔥‼⚡😱🤯💥🏃⏳⏰]")


# --- Helpers ----------------------------------------------------------------


def _fingerprint_laptop(title: Optional[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    return "laptop" in t or "notebook" in t or "macbook" in t


def _fingerprint_combo(title: Optional[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    keywords = ("combo", "+ backpack", "+ mouse", "+ mochila", "+ bocina")
    return any(k in t for k in keywords)


def _fingerprint_high_end_laptop(title: Optional[str]) -> bool:
    """Detecta laptop con i7/Ryzen 7 o GPU dedicada."""
    if not title:
        return False
    t = title.lower()
    cpu_high = any(k in t for k in ("core i7", " i7 ", " i7,", "ryzen 7"))
    gpu_dedicated = any(k in t for k in ("rtx", "gtx", "radeon rx"))
    return cpu_high or gpu_dedicated


def _fingerprint_smartphone(title: Optional[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(
        k in t
        for k in (
            "iphone",
            "galaxy",
            "smartphone",
            "celular",
            "redmi",
            "motorola",
            "pixel",
        )
    )


def _fingerprint_smartphone_flagship(title: Optional[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(
        k in t
        for k in (
            "iphone 15",
            "iphone 16",
            "iphone pro",
            "iphone pro max",
            "galaxy s24",
            "galaxy s25",
            "galaxy s ultra",
            "galaxy z",
            "pixel 8",
            "pixel 9",
        )
    )


def _fingerprint_tablet_apple(title: Optional[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    return ("ipad" in t) and ("apple" in t or True)  # iPad ya implica Apple


def _fingerprint_audio_flagship(title: Optional[str]) -> bool:
    if not title:
        return False
    t = title.lower()
    return any(
        k in t
        for k in (
            "airpods pro",
            "wf-1000xm",
            "wf 1000xm",
            "qc ultra",
            "qc 45",
            "quietcomfort",
            "sennheiser momentum",
        )
    )


def _has_premium_brand(brand: Optional[str]) -> bool:
    if not brand:
        return False
    return brand.lower() in PREMIUM_BRANDS


def _digit_missing_signal(
    current_price: Optional[float], category: Optional[str], title: Optional[str]
) -> bool:
    """Heurística: si el precio actual *10 cae dentro de un rango razonable de
    la categoría, sospechamos un dígito faltante.

    Ejemplos: laptop $305 → $3,050 sigue sospechoso, *10 = $3,050 pero *100 = $30,500
    cae dentro del rango. iPhone Pro Max $3,899 → *10 = $38,990 sí entra en rango.
    """
    if current_price is None or current_price <= 0:
        return False
    if not category and not title:
        return False
    crange = get_range(category)
    if crange.suspicious_below is None:
        return False
    # Si current * 10 cabe en el rango "no sospechoso" (>= suspicious_below) y
    # current * 100 también es razonable, sospecha alta.
    if current_price * 10 >= crange.suspicious_below and current_price < crange.suspicious_below:
        # Adicionalmente: el precio absoluto debe ser muy bajo para la categoría
        if current_price < (crange.suspicious_below * 0.5):
            return True
    return False


def _confidence_label(score: int) -> str:
    if score >= 90:
        return "very high"
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def _classify(score: int) -> str:
    if score >= 80:
        return Classification.PRICE_ERROR_CONFIRMED.value
    if score >= 60:
        return Classification.POSSIBLE_PRICE_ERROR.value
    if score >= 40:
        return Classification.SUSPICIOUS_DEAL.value
    return Classification.NO_PRICE_ERROR.value


def _count_urgency_emojis(text: Optional[str]) -> int:
    if not text:
        return 0
    return len(URGENCY_EMOJIS.findall(text))


def _has_terms(text: Optional[str], patterns: Iterable[re.Pattern[str]]) -> list[str]:
    if not text:
        return []
    matched: list[str] = []
    for pat in patterns:
        m = pat.search(text)
        if m:
            matched.append(m.group(0).strip())
    return matched


# --- Scorer principal --------------------------------------------------------


class PriceErrorScorer:
    """Calcula score 0-100 para un PriceErrorSignal.

    El score se acota a [0, 100]. Las clasificaciones se determinan por
    umbrales (ver `_classify`).

    El scorer **no muta** el signal de entrada. Devuelve un `ScoringResult`
    y opcionalmente un signal nuevo enriquecido (`score_into`).
    """

    def score(self, signal: PriceErrorSignal) -> ScoringResult:
        reasons: list[str] = []
        not_publishable: list[str] = []

        # ---- Hard gates: no publicable ----
        if not signal.has_image:
            not_publishable.append("no_image")
        if signal.current_price is None or signal.current_price <= 0:
            not_publishable.append("no_price")

        # ---- Accessory / "compatible con" assessment (REGLA 2 + 3) -----
        # Se calcula UNA vez para gobernar todos los bonus y penalizaciones
        # subsecuentes. Vivimos del `signal.product_title`; si el extractor
        # ya marcó `accessory_price_suspected`, también lo respetamos.
        assessment: AccessoryAssessment = assess_title(
            signal.product_title,
            category=signal.category,
            brand=signal.brand,
        )
        is_generic_accessory = (
            assessment.is_generic_accessory
            or signal.accessory_price_suspected
        )
        compatible_only = (
            assessment.mentions_compatible_with_premium
            and not assessment.is_real_premium_product
        )
        # La marca premium sólo cuenta si el producto es realmente premium.
        # Si el título dice "compatible con iPhone", la marca declarada
        # "apple" no debe sumar bonus.
        premium_brand_counts = (
            _has_premium_brand(signal.brand)
            and assessment.is_real_premium_product
            and not is_generic_accessory
            and not compatible_only
        )

        # ---- Score positivo ----
        score = 0
        category = signal.category or self._infer_category(signal.product_title)

        # 1) Premium brand con precio < 20% del rango normal de categoría
        crange = get_range(category)
        if (
            premium_brand_counts
            and signal.current_price is not None
            and crange.suspicious_below is not None
            and signal.current_price < (crange.suspicious_below * 0.2)
        ):
            score += PREMIUM_BELOW_20PCT_RANGE
            reasons.append(f"premium_below_20pct_of_category[{PREMIUM_BELOW_20PCT_RANGE}]")

        # 2) Histórico propio: precio < histórico * 0.30 (caída >= 70%)
        if (
            signal.historical_median_price
            and signal.current_price is not None
            and signal.current_price <= signal.historical_median_price * 0.30
        ):
            score += HISTORICAL_DROP_70PCT
            reasons.append(f"historical_drop_70pct[{HISTORICAL_DROP_70PCT}]")
        # 2b) Caída 60-70% vs histórico: señal media
        elif (
            signal.historical_median_price
            and signal.current_price is not None
            and signal.current_price <= signal.historical_median_price * 0.40
        ):
            score += HISTORICAL_DROP_60_70
            reasons.append(f"historical_drop_60_70[{HISTORICAL_DROP_60_70}]")
        # 2c) Componentes PC con caída >= 50% vs histórico
        elif (
            category == "pc_components"
            and signal.historical_median_price
            and signal.current_price is not None
            and signal.current_price <= signal.historical_median_price * 0.50
        ):
            score += PC_COMPONENT_HISTORICAL_DROP_60
            reasons.append(f"pc_component_historical_drop[{PC_COMPONENT_HISTORICAL_DROP_60}]")

        # 3) Descuento visible >= 80%
        discount = signal.discount_percent
        if discount is None and signal.previous_price and signal.current_price:
            if signal.previous_price > 0 and signal.current_price < signal.previous_price:
                discount = round(
                    (1 - signal.current_price / signal.previous_price) * 100, 2
                )
        if discount is not None and discount >= 80:
            score += DISCOUNT_VISIBLE_80
            reasons.append(f"discount_visible_>=80[{DISCOUNT_VISIBLE_80}]")
            if discount >= 90:
                score += DISCOUNT_VISIBLE_90
                reasons.append(f"discount_visible_>=90[{DISCOUNT_VISIBLE_90}]")

        # 4) Telegram: "error de precio"
        text = signal.original_text or ""
        explicit_terms = _has_terms(text, PRICE_ERROR_TERMS)
        if explicit_terms or any(
            "ERROR DE PRECIO" in (t or "").upper() for t in signal.urgency_terms
        ):
            score += TELEGRAM_ERROR_KEYWORD
            reasons.append(f"telegram_error_keyword[{TELEGRAM_ERROR_KEYWORD}]")

        # 5) Urgencia ("corran", "a solo", "deja pedir", etc.)
        urgent_terms = _has_terms(text, URGENCY_TERMS)
        urgent_signal = list(urgent_terms) + [
            t
            for t in signal.urgency_terms
            if any(
                key in (t or "").upper()
                for key in ("CORRAN", "DEJA PEDIR", "EN SOLO", "A SOLO", "SALE EN", "NO CANCELAN")
            )
        ]
        if urgent_signal:
            score += TELEGRAM_URGENCY_KEYWORD
            reasons.append(f"telegram_urgency_keyword[{TELEGRAM_URGENCY_KEYWORD}]")

        # 5b) Emojis de urgencia
        if _count_urgency_emojis(text) >= 3:
            score += TELEGRAM_EMOJIS_BONUS
            reasons.append(f"telegram_urgency_emojis[{TELEGRAM_EMOJIS_BONUS}]")

        # 6) Marca premium con precio anormal (current < suspicious_below)
        if (
            premium_brand_counts
            and signal.current_price is not None
            and crange.suspicious_below is not None
            and signal.current_price < crange.suspicious_below
        ):
            score += PREMIUM_BRAND_ANOMALY
            reasons.append(f"premium_brand_anomaly[{PREMIUM_BRAND_ANOMALY}]")

        # 7) Laptop nueva por debajo de $4,000 MXN
        title_is_laptop = _fingerprint_laptop(signal.product_title) or category in (
            "laptop",
            "laptop_gaming",
            "laptop_business",
        )
        if (
            title_is_laptop
            and not is_generic_accessory
            and signal.current_price is not None
            and signal.current_price < 4000.0
            and signal.condition == Condition.NEW.value
        ):
            score += LAPTOP_BELOW_4000
            reasons.append(f"laptop_below_4000[{LAPTOP_BELOW_4000}]")
            # Bonus extra si tiene CPU/GPU de gama alta
            if _fingerprint_high_end_laptop(signal.product_title):
                score += 10
                reasons.append("laptop_high_end_cpu_gpu[10]")

        # 7b) Combo laptop + accesorios bajo $5,000
        if (
            _fingerprint_combo(signal.product_title)
            and title_is_laptop
            and signal.current_price is not None
            and signal.current_price < 5000.0
        ):
            score += COMBO_LAPTOP_BELOW_5000
            reasons.append(f"combo_laptop_accessories_below_5000[{COMBO_LAPTOP_BELOW_5000}]")

        # 8) Smartphone flagship < $5,000
        if (
            (_fingerprint_smartphone_flagship(signal.product_title) or category == "smartphone_flagship")
            and not is_generic_accessory
            and not compatible_only
            and signal.current_price is not None
            and signal.current_price < 5000.0
        ):
            score += SMARTPHONE_FLAGSHIP_BELOW_5000
            reasons.append(f"smartphone_flagship_below_5000[{SMARTPHONE_FLAGSHIP_BELOW_5000}]")
        elif (
            _fingerprint_smartphone(signal.product_title)
            and not is_generic_accessory
            and not compatible_only
            and signal.current_price is not None
            and signal.current_price < 500.0
        ):
            # Smartphone funcional <$500 = error extremo
            score += SMARTPHONE_FLAGSHIP_BELOW_5000
            reasons.append(f"smartphone_below_500_extreme[{SMARTPHONE_FLAGSHIP_BELOW_5000}]")

        # 9) Audio flagship < $1,000
        if (
            (_fingerprint_audio_flagship(signal.product_title) or category == "audio_premium")
            and not is_generic_accessory
            and signal.current_price is not None
            and signal.current_price < 1000.0
        ):
            score += AUDIO_FLAGSHIP_BELOW_1000
            reasons.append(f"audio_flagship_below_1000[{AUDIO_FLAGSHIP_BELOW_1000}]")

        # 10) iPad / tablet Apple < $5,000
        if (
            _fingerprint_tablet_apple(signal.product_title)
            and not is_generic_accessory
            and not compatible_only
            and signal.current_price is not None
            and signal.current_price < 5000.0
        ):
            score += TABLET_APPLE_BELOW_5000
            reasons.append(f"tablet_apple_below_5000[{TABLET_APPLE_BELOW_5000}]")
        elif (
            category == "tablet"
            and not is_generic_accessory
            and signal.current_price is not None
            and signal.current_price < 5000.0
        ):
            score += TABLET_APPLE_BELOW_5000
            reasons.append(f"tablet_below_5000[{TABLET_APPLE_BELOW_5000}]")

        # 11) Falta de un dígito en el precio
        if _digit_missing_signal(signal.current_price, category, signal.product_title):
            score += DIGIT_MISSING
            reasons.append(f"digit_missing_suspected[{DIGIT_MISSING}]")

        # 12) Precio actual contradice precio anterior (descuento real >= 70%)
        if (
            signal.previous_price
            and signal.current_price is not None
            and signal.previous_price > 0
            and signal.current_price <= signal.previous_price * 0.30
        ):
            score += PREVIOUS_VS_CURRENT_70PCT
            reasons.append(f"previous_vs_current_drop_70[{PREVIOUS_VS_CURRENT_70PCT}]")
        elif (
            signal.previous_price
            and signal.current_price is not None
            and signal.previous_price > 0
            and signal.current_price <= signal.previous_price * 0.40
        ):
            score += PREVIOUS_VS_CURRENT_60_70
            reasons.append(f"previous_vs_current_drop_60_70[{PREVIOUS_VS_CURRENT_60_70}]")

        # 12b) Marca premium con caída >= 60% vs histórico (refuerzo de pc_components)
        if (
            premium_brand_counts
            and signal.historical_median_price
            and signal.current_price is not None
            and signal.current_price <= signal.historical_median_price * 0.40
        ):
            score += PREMIUM_BRAND_STRONG_ANOMALY_VS_HISTORY
            reasons.append(
                f"premium_brand_strong_anomaly_vs_history[{PREMIUM_BRAND_STRONG_ANOMALY_VS_HISTORY}]"
            )

        # ---- Score negativo (si pasa los gates) ----

        if signal.condition in (Condition.USED.value, Condition.REFURBISHED.value):
            score += USED_PRODUCT
            reasons.append(f"used_or_refurbished[{USED_PRODUCT}]")

        if signal.has_stock is False:
            score += NO_STOCK
            reasons.append(f"no_stock[{NO_STOCK}]")
            not_publishable.append("no_stock")

        if signal.resolved_url is None and signal.original_url and not signal.is_publishable:
            # No siempre se rebaja: sólo si explícitamente no se pudo resolver.
            score += LINK_NOT_VALIDATED
            reasons.append(f"link_not_validated[{LINK_NOT_VALIDATED}]")

        if signal.coupon_dependent:
            score += COUPON_DEPENDENT
            reasons.append(f"coupon_dependent[{COUPON_DEPENDENT}]")

        if signal.generic_brand:
            score += GENERIC_BRAND
            reasons.append(f"generic_brand[{GENERIC_BRAND}]")

        if signal.external_seller_suspect:
            score += EXTERNAL_SELLER_SUSPECT
            reasons.append(f"external_seller_suspect[{EXTERNAL_SELLER_SUSPECT}]")

        if signal.accessory_price_suspected:
            score += ACCESSORY_PRICE
            reasons.append(f"accessory_price[{ACCESSORY_PRICE}]")

        if signal.variant_mismatch:
            score += VARIANT_MISMATCH
            reasons.append(f"variant_mismatch[{VARIANT_MISMATCH}]")

        # Mensualidad falsa: descarta directamente
        if signal.monthly_payment_suspected:
            not_publishable.append("monthly_payment")
            score = max(0, score - 30)
            reasons.append("monthly_payment_suspected[-30]")

        # ---- Penalizaciones nuevas (REGLA 5) -----------------------------
        # Accesorio genérico: -45 y bloqueo de price_error_confirmed.
        if is_generic_accessory:
            score += GENERIC_ACCESSORY_PENALTY
            reasons.append(
                f"generic_accessory_not_price_error[{GENERIC_ACCESSORY_PENALTY}]"
            )
            # Auditoría: dejamos huella de los tokens detectados.
            if assessment.matched_tokens:
                reasons.append(
                    "accessory_tokens=" + ",".join(assessment.matched_tokens[:6])
                )

        # "compatible con iPhone/Samsung/iPad" SIN producto premium real: -30.
        if compatible_only:
            score += COMPATIBLE_WITH_PREMIUM_PENALTY
            reasons.append(
                "compatible_with_premium_brand_not_premium_product"
                f"[{COMPATIBLE_WITH_PREMIUM_PENALTY}]"
            )

        # Cap superior: si es accesorio genérico, no se permite
        # `price_error_confirmed` salvo que haya histórico propio que
        # demuestre caída >= 85%. Ningún accesorio "compatible con" puede
        # superarlo nunca.
        extreme_historical_drop = bool(
            signal.historical_median_price
            and signal.current_price is not None
            and signal.current_price <= signal.historical_median_price * 0.15
        )
        if is_generic_accessory and not (
            extreme_historical_drop and not compatible_only
        ):
            if score > GENERIC_ACCESSORY_SCORE_CAP:
                reasons.append(
                    f"generic_accessory_capped_to_{GENERIC_ACCESSORY_SCORE_CAP}"
                )
                score = GENERIC_ACCESSORY_SCORE_CAP

        # ---- Acotar y clasificar ----
        score = max(0, min(100, score))
        classification = _classify(score)
        confidence = _confidence_label(score)

        # REGLA 1 (defensiva): nunca dejar `price_error_confirmed` con
        # confianza `medium`. Si los thresholds quedaran fuera de sync, se
        # degrada a `possible_price_error`.
        if (
            classification == Classification.PRICE_ERROR_CONFIRMED.value
            and confidence not in ("very high", "high")
        ):
            classification = Classification.POSSIBLE_PRICE_ERROR.value
            reasons.append(
                "degraded_confirmed_to_possible_due_to_medium_confidence"
            )

        # REGLA 5 (cap): si es accesorio genérico, la confianza máxima
        # permitida es "medium" (no "high"/"very high") salvo histórico
        # extremo y producto premium real.
        if (
            is_generic_accessory
            and not (extreme_historical_drop and not compatible_only)
            and confidence in ("very high", "high")
        ):
            confidence = "medium"
            reasons.append("generic_accessory_capped_confidence_to_medium")
            # Si la clasificación seguía siendo confirmed por azar, también
            # se degrada (consistencia con confianza).
            if classification == Classification.PRICE_ERROR_CONFIRMED.value:
                classification = Classification.POSSIBLE_PRICE_ERROR.value

        is_publishable = (
            len(not_publishable) == 0
            and signal.has_image
            and signal.current_price is not None
            and signal.current_price > 0
        )

        return ScoringResult(
            score=score,
            classification=classification,
            confidence_label=confidence,
            reasons=reasons,
            is_publishable=is_publishable,
            not_publishable_reasons=not_publishable,
        )

    def score_into(self, signal: PriceErrorSignal) -> PriceErrorSignal:
        """Devuelve un nuevo `PriceErrorSignal` con los campos del scorer
        rellenados (para persistir o auditar). No muta el original.
        """
        result = self.score(signal)
        return replace(
            signal,
            final_score=result.score,
            classification=result.classification,
            reasons=list(signal.reasons) + result.reasons,
            is_publishable=result.is_publishable,
            not_publishable_reasons=list(signal.not_publishable_reasons)
            + result.not_publishable_reasons,
        )

    # ------------------------------------------------------------------
    # Inferencia simple de categoría (fallback cuando signal.category es None).
    # ------------------------------------------------------------------

    def _infer_category(self, title: Optional[str]) -> Optional[str]:
        if not title:
            return None
        t = title.lower()
        if any(k in t for k in ("laptop", "notebook", "macbook")):
            if any(k in t for k in ("rtx", "gtx", "radeon rx", "gamer", "gaming")):
                return "laptop_gaming"
            if any(k in t for k in ("elitebook", "thinkpad", "latitude", "probook")):
                return "laptop_business"
            return "laptop"
        if any(k in t for k in ("ipad", "tablet")):
            return "tablet"
        if any(
            k in t
            for k in (
                "iphone 15",
                "iphone 16",
                "galaxy s24",
                "galaxy s25",
                "galaxy s ultra",
                "iphone pro",
            )
        ):
            return "smartphone_flagship"
        if any(k in t for k in ("iphone", "galaxy", "smartphone", "celular", "redmi")):
            return "smartphone"
        if any(
            k in t for k in ("airpods", "wf-1000xm", "wf 1000xm", "audífonos", "headphones", "qc ultra")
        ):
            return "audio_premium"
        if any(k in t for k in ("playstation", "xbox", "nintendo", "ps5", "consola")):
            return "console"
        if any(k in t for k in ("monitor", "smart tv", "pantalla")):
            return "display"
        if any(k in t for k in ("motherboard", "gpu", "cpu", "ram", "ssd", "rtx", "ryzen")):
            return "pc_components"
        return "uncategorized"
