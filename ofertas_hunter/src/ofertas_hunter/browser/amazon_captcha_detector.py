"""Detector estructural de CAPTCHA de Amazon (Robot Check).

Reemplaza el matching ingenuo por substring `validateCaptcha` /
`amzn-captcha` que el detector anterior usaba. La nueva lógica busca
**evidencia estructural** y **visible** simultáneamente, alineada con la
estrategia de `AmazonScrapperIA/src/browser_worker.py`.

## Señales fuertes (estructurales)

Cualquiera de estas, por sí sola, es **alta evidencia** de captcha real:

* `<form action="/errors/validateCaptcha"...>` — el form gating de Amazon.
* `<input id="captchacharacters">` o `<input name="captchacharacters">` —
  el campo donde el humano escribe el captcha.
* `<input name="amzn-captcha"...>` — usado en variantes localizadas.
* URL final contiene `/errors/validateCaptcha` (Playwright sigue redirects).

## Señales visibles (texto / título)

Confirman que el usuario realmente vería el bloqueo:

* `document.title` contiene `Robot Check` o `Amazon.com.mx` con body chico
  y "Continuar a Compras".
* texto visible: "Enter the characters you see below"
* texto visible: "Type the characters you see"
* texto visible: "Escribe los caracteres que ves"
* texto visible: "Continuar comprando" / "Continuar a Compras" (apparece en
  el botón de la página de error de Amazon MX).
* texto visible: "Lo sentimos, parece que está utilizando un programa
  automatizado".
* texto visible: "Sorry, we just need to make sure you're not a robot".

## Reglas

* `is_captcha=True` y `confidence="high"` → **al menos una señal fuerte**
  combinada con **al menos una señal visible** (o body muy chico < 10KB
  con `Amazon.com.mx` como title y form de validateCaptcha).
* `is_captcha=True` y `confidence="medium"` → señal fuerte pero NO visible
  (el HTML pudo cargar parcial). El caller emite warning, NO pausa
  Amazon. La revalidación se reprograma.
* `is_captcha=True` y `confidence="low"` → SOLO señal débil (e.g.
  substring `validateCaptcha` dentro de un script bundle, sin estructura
  ni visible). No se trata como captcha real. Se clasifica como
  `amazon_suspected_false_captcha`.
* `is_captcha=False` → ninguna señal estructural ni visible.

`should_pause_marketplace=True` requiere `confidence="high"` y al menos
una señal estructural fuerte. El caller (orchestrator/agent) acumula
estas detecciones para decidir cuándo pausar.

## NO marcar captcha por

* substring `captcha` dentro de scripts JS / JSON / telemetry / comentarios.
* status HTTP 503 sin contenido visible de robot check.
* falta temporal de precio.
* página incompleta.
* timeout de Playwright.
* selector roto.

Para esos casos, el caller debe usar reasons como `amazon_extraction_failed`,
`amazon_incomplete_dom`, `amazon_selector_miss`, `amazon_possible_block_low_confidence`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Patrones estructurales fuertes
# ---------------------------------------------------------------------------

_FORM_ACTION_RE = re.compile(
    r'<form[^>]+action\s*=\s*"[^"]*\/errors\/validateCaptcha[^"]*"',
    re.IGNORECASE,
)
_CAPTCHACHARS_INPUT_RE = re.compile(
    r'<input[^>]+(?:id|name)\s*=\s*"captchacharacters"',
    re.IGNORECASE,
)
_AMZN_CAPTCHA_INPUT_RE = re.compile(
    r'<input[^>]+name\s*=\s*"amzn-captcha[^"]*"',
    re.IGNORECASE,
)
_CAPTCHA_IMG_SRC_RE = re.compile(
    r'<img[^>]+src\s*=\s*"[^"]*\/captcha\/',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Patrones visibles (texto humano-leíble)
# ---------------------------------------------------------------------------

_VISIBLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("title_robot_check", re.compile(r"<title[^>]*>\s*Robot Check\s*<", re.IGNORECASE)),
    ("text_enter_characters_en", re.compile(r"Enter the characters you see below", re.IGNORECASE)),
    ("text_type_characters_en", re.compile(r"Type the characters you see", re.IGNORECASE)),
    ("text_enter_characters_es", re.compile(r"Escribe los caracteres que ves", re.IGNORECASE)),
    (
        "text_sorry_robot_en",
        re.compile(
            r"Sorry,\s*we just need to make sure you'?re not a robot",
            re.IGNORECASE,
        ),
    ),
    (
        "text_apology_es",
        re.compile(
            r"Lo sentimos,\s*parece que está utilizando un programa automatizado",
            re.IGNORECASE,
        ),
    ),
    (
        "text_continuar_comprando",
        re.compile(r"continuar comprando|continuar a compras", re.IGNORECASE),
    ),
    ("text_continue_shopping", re.compile(r"continue shopping", re.IGNORECASE)),
)


# ---------------------------------------------------------------------------
# Patrón débil (substring sin contexto): NO declara captcha por sí solo
# ---------------------------------------------------------------------------

_WEAK_TOKENS: tuple[str, ...] = (
    "validateCaptcha",
    "amzn-captcha",
    "captcha-instrumentation",
)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass
class CaptchaAssessment:
    """Resultado del análisis de una respuesta Amazon.

    Campos:
        is_captcha: True si el HTML/URL contiene evidencia (cualquier
            confidence). El caller decide si actuar según `confidence`.
        confidence: "high" | "medium" | "low" | "none".
        strong_signals: señales estructurales detectadas.
        weak_signals: señales débiles detectadas (substrings sin contexto).
        visible_signals: señales visibles detectadas.
        should_pause_marketplace: True sólo si confidence=high y se reúnen
            condiciones para sumar al contador de captchas reales.
        reasons: textos cortos y estables, para auditoría.
    """

    is_captcha: bool
    confidence: str
    strong_signals: tuple[str, ...] = field(default_factory=tuple)
    weak_signals: tuple[str, ...] = field(default_factory=tuple)
    visible_signals: tuple[str, ...] = field(default_factory=tuple)
    should_pause_marketplace: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_high_confidence(self) -> bool:
        return self.is_captcha and self.confidence == "high"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class AmazonCaptchaDetector:
    """Detector estructural y visible para captcha de Amazon."""

    BODY_TINY_THRESHOLD: int = 10_000  # bytes; el captcha real ronda 5–6KB.

    def assess(
        self,
        *,
        html: Optional[str],
        final_url: Optional[str] = None,
        status: Optional[int] = None,
    ) -> CaptchaAssessment:
        """Analiza una respuesta Amazon y devuelve un `CaptchaAssessment`."""
        html = html or ""
        final_url = final_url or ""

        strong: list[str] = []
        weak: list[str] = []
        visible: list[str] = []
        reasons: list[str] = []

        # ---- Señales fuertes estructurales ----
        if "/errors/validateCaptcha" in final_url:
            strong.append("url_validate_captcha")
            reasons.append("final_url_contains_validateCaptcha")

        if _FORM_ACTION_RE.search(html):
            strong.append("form_action_validate_captcha")
            reasons.append("form_action_validate_captcha")
        if _CAPTCHACHARS_INPUT_RE.search(html):
            strong.append("input_captchacharacters")
            reasons.append("input_captchacharacters")
        if _AMZN_CAPTCHA_INPUT_RE.search(html):
            strong.append("input_amzn_captcha")
            reasons.append("input_amzn_captcha")
        if _CAPTCHA_IMG_SRC_RE.search(html):
            strong.append("img_src_captcha")
            reasons.append("img_src_captcha")

        # ---- Señales visibles (texto humano) ----
        for name, pat in _VISIBLE_PATTERNS:
            if pat.search(html):
                visible.append(name)

        # ---- Señales débiles (substrings sin contexto) ----
        for tok in _WEAK_TOKENS:
            if tok in html:
                weak.append(tok)

        # ---- Heurística de body diminuto ----
        # Sólo cuenta como confirmación cuando además hay un `<title>` con
        # `Amazon.com.mx` o `Robot Check`. Un body chico sin título no es
        # señal suficiente (puede ser un timeout / error de red).
        body_tiny = 0 < len(html) < self.BODY_TINY_THRESHOLD
        title_match = re.search(
            r"<title[^>]*>\s*(Amazon\.com\.mx|Robot Check)\s*<",
            html,
            re.IGNORECASE,
        )
        body_tiny_with_title = bool(body_tiny and title_match)
        if body_tiny_with_title:
            reasons.append("body_tiny_with_amazon_title")

        # ---- Decisión ----
        # high: estructura fuerte + visible (texto humano leíble).
        #   `body_tiny_with_title` por sí solo NO sube a high — antes
        #   subía pero un producto real con title "Amazon.com.mx" y body
        #   chico (timeout parcial) podía disparar falso pausa.
        # medium: estructura fuerte sin visible.
        # low: sólo weak (sin estructura ni visible).
        # none: nada.
        confidence = "none"
        is_captcha = False

        if strong and visible:
            confidence = "high"
            is_captcha = True
        elif strong:
            # Estructura fuerte sin texto visible humano: medium. NO pausa.
            # Esto cubre el caso reportado por el operador donde un
            # `<form action="/errors/validateCaptcha">` aislado en una
            # página de producto disparaba pausa de marketplace.
            confidence = "medium"
            is_captcha = True
        elif weak and visible:
            confidence = "medium"
            is_captcha = True
        elif weak and visible:
            # Defensa: si hay señal débil (substring) PERO visible texto,
            # tratamos como medium (no high) para no pausar marketplace
            # sólo por substrings.
            confidence = "medium"
            is_captcha = True
        elif weak:
            confidence = "low"
            is_captcha = True
            reasons.append("weak_token_only_no_structure")
        elif visible:
            # Visible sin estructura → casi nunca real. Reportamos como
            # baja confianza para el caller.
            confidence = "low"
            is_captcha = True
            reasons.append("visible_text_only_no_structure")

        # ---- HTTP 503 sin captcha real → NO captcha ----
        if status == 503 and confidence in ("low",):
            # 503 + texto suelto NO es suficiente.
            reasons.append("status_503_without_strong_signals")
            # Mantenemos is_captcha=True con confidence=low para que el
            # caller emita amazon_suspected_false_captcha sin pausar.

        # `should_pause_marketplace`: SOLO con structural fuerte + visible.
        # Caso especial: URL `/errors/validateCaptcha` + form_action es ya
        # suficiente (REGLA del usuario).
        should_pause = (
            confidence == "high"
            and (
                "url_validate_captcha" in strong
                and "form_action_validate_captcha" in strong
            )
            or (confidence == "high" and len(strong) >= 1 and len(visible) >= 1)
        )

        return CaptchaAssessment(
            is_captcha=is_captcha,
            confidence=confidence,
            strong_signals=tuple(strong),
            weak_signals=tuple(weak),
            visible_signals=tuple(visible),
            should_pause_marketplace=bool(should_pause),
            reasons=tuple(reasons),
        )


__all__ = ["AmazonCaptchaDetector", "CaptchaAssessment"]
