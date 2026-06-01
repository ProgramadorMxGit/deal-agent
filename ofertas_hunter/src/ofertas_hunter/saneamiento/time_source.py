"""Fuente de tiempo determinista para el Bot_Saneamiento.

Helpers expuestos:

- :func:`now_utc` — ``datetime.now(timezone.utc)``.
- :func:`now_utc_iso` — ISO-8601 con precisión de milisegundos y sufijo ``Z``.
- :func:`cutoff_iso` — ISO-8601 con precisión de milisegundos y sufijo ``Z`` para
  ``clock() - timedelta(hours=hours)``.

Diseñados para ser inyectables como ``clock`` en runner/tasks/tests, garantizando
reproducibilidad de las property tests con un reloj fijo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

__all__ = ["now_utc", "now_utc_iso", "cutoff_iso"]


def now_utc() -> datetime:
    """Devuelve el instante actual en UTC como ``datetime`` tz-aware."""

    return datetime.now(timezone.utc)


def _format_iso_z(value: datetime) -> str:
    """Formatea ``value`` como ISO-8601 con milisegundos y sufijo ``Z``.

    Implementación interna compartida por :func:`now_utc_iso` y
    :func:`cutoff_iso` para garantizar idéntico formato.
    """

    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_utc_iso(clock: Callable[[], datetime] = now_utc) -> str:
    """Devuelve ``clock()`` formateado como ISO-8601 ms con sufijo ``Z``.

    Por defecto usa :func:`now_utc`. Inyectable para tests deterministas.
    """

    return _format_iso_z(clock())


def cutoff_iso(clock: Callable[[], datetime], hours: float) -> str:
    """Devuelve ``clock() - timedelta(hours=hours)`` en ISO-8601 ms con sufijo ``Z``.

    Útil para construir cutoffs deterministas (p. ej. 4h para ``outbox-stale``,
    48h para ``outbox-duplicates``).
    """

    return _format_iso_z(clock() - timedelta(hours=hours))
