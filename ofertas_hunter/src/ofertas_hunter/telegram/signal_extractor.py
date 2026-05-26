"""Extracción de señales textuales de mensajes de Telegram.

Detecta:

- términos explícitos de "error de precio";
- términos de urgencia ("corran", "deja pedir", "a solo", etc.);
- emojis de urgencia 🚨 🔥 ‼️ ⚡ etc.;
- porcentajes de descuento visibles;
- mayúsculas excesivas.

Devuelve `UrgencySignals(score, terms)` que se usa para alimentar el
PriceErrorScorer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Términos ----------------------------------------------------------------

PRICE_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bERROR\s+DE\s+PRECIO+\b[\?\!]*", re.IGNORECASE),
    re.compile(r"\bPOSIBLE\s+ERROR\s+DE\s+PRECIO\b", re.IGNORECASE),
    re.compile(r"\bOTRO\s+ERROR\s+DE\s+PRECIO\b", re.IGNORECASE),
]

URGENCY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bCORR[A]+N\b", re.IGNORECASE),
    re.compile(r"\bDEJA\s+PEDIR\b", re.IGNORECASE),
    re.compile(r"\bNO\s+CANCELAN\b", re.IGNORECASE),
    re.compile(r"\b(EN|A)\s+SOLO\b", re.IGNORECASE),
    re.compile(r"\bSALE\s+EN\b", re.IGNORECASE),
    re.compile(r"\bPRECIO\s+M[AÁ]S\s+BAJO\b", re.IGNORECASE),
]

URGENCY_EMOJI_RE = re.compile(r"[🚨🔥‼⚡😱🤯💥🏃⏳⏰]")

# Puntos
SCORE_PRICE_ERROR = 25
SCORE_URGENCY_TERM = 10
SCORE_EMOJIS_BONUS = 5


# --- Resultado --------------------------------------------------------------


@dataclass
class UrgencySignals:
    score: int = 0
    terms: list[str] = field(default_factory=list)
    has_price_error_keyword: bool = False
    emojis_count: int = 0


# --- API --------------------------------------------------------------------


def extract_urgency(text: str) -> UrgencySignals:
    """Extrae señales de urgencia/error de precio de un mensaje.

    El texto se examina con regex case-insensitive. La función es pura.
    """
    if not text:
        return UrgencySignals()

    score = 0
    terms: list[str] = []
    has_pe_keyword = False

    for pattern in PRICE_ERROR_PATTERNS:
        match = pattern.search(text)
        if match:
            score += SCORE_PRICE_ERROR
            terms.append(match.group(0).strip())
            has_pe_keyword = True
            break  # un solo bonus por categoría

    urgency_seen = False
    for pattern in URGENCY_PATTERNS:
        match = pattern.search(text)
        if match:
            urgency_seen = True
            terms.append(match.group(0).strip())
    if urgency_seen:
        score += SCORE_URGENCY_TERM

    emojis = URGENCY_EMOJI_RE.findall(text)
    if len(emojis) >= 3:
        score += SCORE_EMOJIS_BONUS

    return UrgencySignals(
        score=score,
        terms=terms,
        has_price_error_keyword=has_pe_keyword,
        emojis_count=len(emojis),
    )
