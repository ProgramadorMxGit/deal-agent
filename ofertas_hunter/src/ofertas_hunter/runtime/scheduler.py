"""Scheduler de modo de operación según hora del día.

Tres modos:

- `hibernating` (23:30 – 06:30 por default): los hunters/discovery duermen,
  el dispatcher **no publica** (la gente está dormida y no quiero gastar
  recursos ni quemar cookies). Watchdog, maintenance y telegram_listener
  siguen activos.
- `warmup` (06:30 – 07:00): hunters reactivados con prioridad alta para
  acumular outbox antes de la hora pico. **Dispatcher sigue pausado** —
  publicaremos a partir de las 7:00 con inventario fresco listo.
- `active` (07:00 – 23:30): operación normal.

Las ventanas son configurables vía env y la zona horaria es
`SCHEDULE_TIMEZONE` (default `America/Mexico_City`).

Ofertas que se descubren durante hibernación / warmup quedan en el outbox
**con su flag de tipo intacto** (price_error sigue siendo price_error).
Cuando el modo cambia a `active`, el dispatcher las publica normalmente
respetando el cooldown global de 5 minutos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo  # type: ignore
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class ScheduleMode(str, Enum):
    HIBERNATING = "hibernating"
    WARMUP = "warmup"
    ACTIVE = "active"


# ---------------------------------------------------------------------------
# Config + parsing
# ---------------------------------------------------------------------------


def _parse_hhmm(value: str, *, default: tuple[int, int]) -> time:
    """Parsea 'HH:MM' a `time`. Cae a default en error."""
    if not value:
        return time(*default)
    try:
        hh_str, mm_str = value.strip().split(":", 1)
        return time(int(hh_str), int(mm_str))
    except (ValueError, AttributeError):
        logger.warning("hora inválida %r, uso default %s", value, default)
        return time(*default)


@dataclass(frozen=True)
class ScheduleConfig:
    """Ventanas de operación, todas en la zona horaria del bot."""

    timezone_name: str = "America/Mexico_City"
    # Hibernación nocturna
    hibernate_start: time = time(23, 30)
    hibernate_end: time = time(6, 30)
    # Calentamiento (entre `hibernate_end` y `active_start`)
    warmup_start: time = time(6, 30)
    active_start: time = time(7, 0)
    # Si False, el scheduler siempre devuelve ACTIVE (modo legacy).
    enabled: bool = True

    @classmethod
    def from_env(
        cls,
        *,
        enabled: bool = True,
        timezone_name: str = "America/Mexico_City",
        hibernate_start: str = "23:30",
        hibernate_end: str = "06:30",
        warmup_start: str = "06:30",
        active_start: str = "07:00",
    ) -> "ScheduleConfig":
        return cls(
            timezone_name=timezone_name,
            hibernate_start=_parse_hhmm(hibernate_start, default=(23, 30)),
            hibernate_end=_parse_hhmm(hibernate_end, default=(6, 30)),
            warmup_start=_parse_hhmm(warmup_start, default=(6, 30)),
            active_start=_parse_hhmm(active_start, default=(7, 0)),
            enabled=enabled,
        )


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass
class ModeDecision:
    mode: ScheduleMode
    local_time: time
    next_change_in: timedelta  # cuánto falta para el próximo cambio
    next_mode: ScheduleMode

    @property
    def is_active(self) -> bool:
        return self.mode == ScheduleMode.ACTIVE

    @property
    def is_hibernating(self) -> bool:
        return self.mode == ScheduleMode.HIBERNATING

    @property
    def is_warmup(self) -> bool:
        return self.mode == ScheduleMode.WARMUP


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def _local_now(tz_name: str) -> datetime:
    """Hora actual en la zona del bot (cae a UTC si zoneinfo no está)."""
    if ZoneInfo is None:  # pragma: no cover
        return datetime.now(timezone.utc)
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception as exc:
        logger.warning("zona %r no válida, uso UTC: %s", tz_name, exc)
        return datetime.now(timezone.utc)


def _within(t: time, start: time, end: time) -> bool:
    """True si `t` está en `[start, end)`. Soporta cruce de medianoche."""
    if start <= end:
        return start <= t < end
    # Cruce de medianoche (ej. 23:30 → 06:30)
    return t >= start or t < end


class OperatingScheduler:
    """Determina el modo de operación según hora local."""

    def __init__(
        self,
        config: Optional[ScheduleConfig] = None,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or ScheduleConfig()
        # `clock` recibe el callable que devuelve hora local (ya tz-aware).
        # Si no se pasa, usamos `_local_now(config.timezone_name)`.
        self._clock = clock or (lambda: _local_now(self.config.timezone_name))

    # ------------------------------------------------------------------
    # API principal
    # ------------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def decide(self, *, at: Optional[datetime] = None) -> ModeDecision:
        if not self.config.enabled:
            return ModeDecision(
                mode=ScheduleMode.ACTIVE,
                local_time=time(0, 0),
                next_change_in=timedelta(days=1),
                next_mode=ScheduleMode.ACTIVE,
            )

        now_local = at if at is not None else self.now()
        t = now_local.time().replace(microsecond=0)

        mode = self._mode_for(t)
        next_mode, next_change_in = self._compute_next(now_local, mode)
        return ModeDecision(
            mode=mode,
            local_time=t,
            next_change_in=next_change_in,
            next_mode=next_mode,
        )

    def is_active(self) -> bool:
        return self.decide().is_active

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _mode_for(self, t: time) -> ScheduleMode:
        c = self.config
        # Hibernación primero (puede cruzar medianoche).
        if _within(t, c.hibernate_start, c.hibernate_end):
            return ScheduleMode.HIBERNATING
        # Warmup entre hibernate_end y active_start.
        if c.warmup_start <= c.active_start:
            if c.warmup_start <= t < c.active_start:
                return ScheduleMode.WARMUP
        else:  # warmup cruza medianoche (raro, pero soportado).
            if t >= c.warmup_start or t < c.active_start:
                return ScheduleMode.WARMUP
        return ScheduleMode.ACTIVE

    def _compute_next(
        self, now_local: datetime, current_mode: ScheduleMode
    ) -> tuple[ScheduleMode, timedelta]:
        """Devuelve (siguiente_modo, tiempo_hasta_el_cambio)."""
        c = self.config
        # Construimos los 3 instantes "límite" siguientes desde now_local.
        targets: list[tuple[time, ScheduleMode]] = [
            (c.hibernate_start, ScheduleMode.HIBERNATING),
            (c.warmup_start, ScheduleMode.WARMUP),
            (c.active_start, ScheduleMode.ACTIVE),
        ]
        # warmup_start == hibernate_end normalmente.
        if c.warmup_start != c.hibernate_end:
            targets.append((c.hibernate_end, ScheduleMode.WARMUP))

        # Ordenar por delta desde ahora.
        deltas: list[tuple[ScheduleMode, timedelta, time]] = []
        for tgt, mode in targets:
            if mode == current_mode:
                continue
            delta = _delta_until(now_local, tgt)
            deltas.append((mode, delta, tgt))
        if not deltas:
            return (current_mode, timedelta(hours=24))
        deltas.sort(key=lambda x: x[1])
        return deltas[0][0], deltas[0][1]


def _delta_until(now: datetime, target: time) -> timedelta:
    """Distancia (positiva) hasta la próxima ocurrencia de `target`."""
    base = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if base <= now:
        base = base + timedelta(days=1)
    return base - now


__all__ = [
    "ModeDecision",
    "OperatingScheduler",
    "ScheduleConfig",
    "ScheduleMode",
]
