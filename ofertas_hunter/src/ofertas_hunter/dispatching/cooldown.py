"""Cooldown global para publicaciones de oferta normal.

- Ofertas normales: respetan `cooldown_seconds` (5 min por defecto).
- Errores de precio (`price_error_confirmed` o `possible_price_error` ya
  revalidados): bypass total, se publican inmediatamente.

La política de cooldown es **estado en memoria** del dispatcher; no requiere
persistencia (si el dispatcher se reinicia, lo más conservador es tomar la
hora del último `published_messages.sent_at` con `type='normal'` como base).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models import OutboxType


@dataclass
class CooldownPolicy:
    """Política de cooldown configurable.

    Atributos:
        cooldown_seconds: tiempo mínimo entre dos ofertas normales.
        bypass_types: tipos de outbox que ignoran el cooldown.
    """

    cooldown_seconds: int = 300
    bypass_types: frozenset[str] = frozenset(
        {OutboxType.PRICE_ERROR.value, OutboxType.POSSIBLE_PE.value}
    )

    def is_publishable(
        self,
        item_type: str,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> bool:
        """True si una oferta de `item_type` puede publicarse ahora.

        Args:
            item_type: `OutboxType.value` (`normal` | `price_error` | `possible_pe`).
            last_normal_publication_at: timestamp de la última publicación
                **normal**. Si es `None` (o tipo bypass), no aplica cooldown.
            now: timestamp actual (inyectable para tests).
        """
        if item_type in self.bypass_types:
            return True
        if last_normal_publication_at is None:
            return True
        now = now or datetime.now(timezone.utc)
        elapsed = now - last_normal_publication_at
        return elapsed >= timedelta(seconds=self.cooldown_seconds)

    def time_until_next(
        self,
        last_normal_publication_at: Optional[datetime],
        now: Optional[datetime] = None,
    ) -> timedelta:
        """Tiempo restante hasta poder publicar la siguiente oferta normal.

        Devuelve `timedelta(0)` si ya se puede publicar.
        """
        if last_normal_publication_at is None:
            return timedelta(0)
        now = now or datetime.now(timezone.utc)
        elapsed = now - last_normal_publication_at
        remaining = timedelta(seconds=self.cooldown_seconds) - elapsed
        return max(timedelta(0), remaining)
