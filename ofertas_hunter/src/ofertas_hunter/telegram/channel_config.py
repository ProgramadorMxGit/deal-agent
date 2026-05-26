"""Resolución de canales Telegram a partir de la config.

Cada entrada de `TELEGRAM_TARGET_CHANNELS` puede ser:

- un username (`ofertonesmexico`) — sin `@`.
- un título exacto (`OFERTAS PREMIUM MX`).
- un id numérico (negativo, ej. `-1001234567890`).

El listener intentará resolver username primero, luego título exacto, luego
id directo.

`ChannelConfig.parse(s)` devuelve la lista normalizada de strings.
`ChannelConfig.classify(entry)` devuelve `"username" | "title" | "id"`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,}$")
_ID_RE = re.compile(r"^-?\d+$")


@dataclass(frozen=True)
class ChannelEntry:
    raw: str
    kind: str  # "username" | "title" | "id"

    def normalized(self) -> str:
        return self.raw.lstrip("@").strip()


def parse_channels(value: str) -> list[ChannelEntry]:
    if not value:
        return []
    entries: list[ChannelEntry] = []
    seen: set[str] = set()
    for chunk in value.split(","):
        candidate = chunk.strip()
        if not candidate:
            continue
        if candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        entries.append(ChannelEntry(raw=candidate, kind=classify(candidate)))
    return entries


def classify(entry: str) -> str:
    cleaned = entry.lstrip("@").strip()
    if _ID_RE.match(cleaned):
        return "id"
    if _USERNAME_RE.match(cleaned):
        return "username"
    return "title"
