"""Tests unitarios para ``ofertas_hunter.saneamiento.time_source``.

Cubre los tres helpers públicos del módulo:

- ``now_utc`` devuelve ``datetime`` tz-aware en UTC.
- ``now_utc_iso(clock)`` formatea con milisegundos y sufijo ``Z``.
- ``cutoff_iso(clock, hours)`` resta horas y formatea con el mismo formato.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ofertas_hunter.saneamiento.time_source import cutoff_iso, now_utc, now_utc_iso


FIXED_DT = datetime(2026, 5, 29, 12, 0, 0, 123456, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    """Reloj determinista usado por todos los tests del módulo."""

    return FIXED_DT


def test_now_utc_returns_tz_aware_utc() -> None:
    moment = now_utc()

    assert isinstance(moment, datetime)
    assert moment.tzinfo is not None
    # Equivalente a UTC; no comparamos directamente con timezone.utc porque
    # algunas plataformas pueden devolver objetos tz-aware equivalentes.
    assert moment.utcoffset() == timezone.utc.utcoffset(moment)


def test_now_utc_iso_formats_milliseconds_with_z_suffix() -> None:
    assert now_utc_iso(fixed_clock) == "2026-05-29T12:00:00.123Z"


def test_cutoff_iso_subtracts_hours_4h() -> None:
    assert cutoff_iso(fixed_clock, 4) == "2026-05-29T08:00:00.123Z"


def test_cutoff_iso_subtracts_hours_48h() -> None:
    assert cutoff_iso(fixed_clock, 48) == "2026-05-27T12:00:00.123Z"
