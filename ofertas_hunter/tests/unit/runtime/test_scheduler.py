"""Tests del OperatingScheduler."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from ofertas_hunter.runtime.scheduler import (
    ModeDecision,
    OperatingScheduler,
    ScheduleConfig,
    ScheduleMode,
    _delta_until,
    _within,
)


def _at(hh: int, mm: int = 0) -> datetime:
    """Datetime de prueba en UTC; las pruebas trabajan con horas literales."""
    return datetime(2026, 5, 25, hh, mm, 0, tzinfo=timezone.utc)


def _scheduler(now: datetime, **overrides) -> OperatingScheduler:
    config = ScheduleConfig(
        timezone_name="UTC",
        hibernate_start=overrides.get("hibernate_start", time(23, 30)),
        hibernate_end=overrides.get("hibernate_end", time(6, 30)),
        warmup_start=overrides.get("warmup_start", time(6, 30)),
        active_start=overrides.get("active_start", time(7, 0)),
        enabled=overrides.get("enabled", True),
    )
    return OperatingScheduler(config=config, clock=lambda: now)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestWithin:
    def test_normal_range(self):
        assert _within(time(8), time(7), time(9)) is True
        assert _within(time(6), time(7), time(9)) is False
        assert _within(time(9), time(7), time(9)) is False  # exclusivo

    def test_crosses_midnight(self):
        # 23:30 → 06:30
        assert _within(time(23, 45), time(23, 30), time(6, 30)) is True
        assert _within(time(0, 0), time(23, 30), time(6, 30)) is True
        assert _within(time(6, 0), time(23, 30), time(6, 30)) is True
        assert _within(time(6, 30), time(23, 30), time(6, 30)) is False  # exclusivo
        assert _within(time(12, 0), time(23, 30), time(6, 30)) is False
        assert _within(time(23, 0), time(23, 30), time(6, 30)) is False


# ---------------------------------------------------------------------------
# Decisión por hora
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "now,expected",
    [
        (_at(0, 0), ScheduleMode.HIBERNATING),
        (_at(2, 30), ScheduleMode.HIBERNATING),
        (_at(6, 0), ScheduleMode.HIBERNATING),
        (_at(6, 29, ), ScheduleMode.HIBERNATING),
        (_at(6, 30), ScheduleMode.WARMUP),
        (_at(6, 45), ScheduleMode.WARMUP),
        (_at(6, 59), ScheduleMode.WARMUP),
        (_at(7, 0), ScheduleMode.ACTIVE),
        (_at(12, 0), ScheduleMode.ACTIVE),
        (_at(20, 0), ScheduleMode.ACTIVE),
        (_at(23, 29), ScheduleMode.ACTIVE),
        (_at(23, 30), ScheduleMode.HIBERNATING),
        (_at(23, 45), ScheduleMode.HIBERNATING),
    ],
)
def test_default_mode_for_each_hour(now, expected):
    sched = _scheduler(now)
    assert sched.decide().mode is expected


def test_mode_decision_includes_next_change():
    # 06:00 → próximo cambio: warmup en 30min
    sched = _scheduler(_at(6, 0))
    decision = sched.decide()
    assert decision.is_hibernating
    assert decision.next_mode is ScheduleMode.WARMUP
    assert decision.next_change_in == timedelta(minutes=30)


def test_warmup_next_is_active_in_30_min():
    sched = _scheduler(_at(6, 30))
    decision = sched.decide()
    assert decision.is_warmup
    assert decision.next_mode is ScheduleMode.ACTIVE
    assert decision.next_change_in == timedelta(minutes=30)


def test_active_next_is_hibernate_at_2330():
    sched = _scheduler(_at(15, 0))
    decision = sched.decide()
    assert decision.is_active
    assert decision.next_mode is ScheduleMode.HIBERNATING
    assert decision.next_change_in == timedelta(hours=8, minutes=30)


def test_hibernate_at_2330_next_is_warmup_at_0630():
    sched = _scheduler(_at(23, 30))
    decision = sched.decide()
    assert decision.is_hibernating
    assert decision.next_mode is ScheduleMode.WARMUP
    assert decision.next_change_in == timedelta(hours=7)


def test_disabled_always_active():
    sched = _scheduler(_at(2, 0), enabled=False)
    assert sched.decide().is_active is True


def test_custom_windows():
    """22:00 → 05:00 hibernación, 05:00 → 06:00 warmup."""
    sched = _scheduler(
        _at(23, 0),
        hibernate_start=time(22, 0),
        hibernate_end=time(5, 0),
        warmup_start=time(5, 0),
        active_start=time(6, 0),
    )
    assert sched.decide().is_hibernating
    sched_2 = _scheduler(
        _at(5, 30),
        hibernate_start=time(22, 0),
        hibernate_end=time(5, 0),
        warmup_start=time(5, 0),
        active_start=time(6, 0),
    )
    assert sched_2.decide().is_warmup
    sched_3 = _scheduler(
        _at(6, 0),
        hibernate_start=time(22, 0),
        hibernate_end=time(5, 0),
        warmup_start=time(5, 0),
        active_start=time(6, 0),
    )
    assert sched_3.decide().is_active


def test_delta_until_today_and_tomorrow():
    now = _at(20, 0)
    # 23:30 hoy
    assert _delta_until(now, time(23, 30)) == timedelta(hours=3, minutes=30)
    # 06:30 → mañana
    assert _delta_until(now, time(6, 30)) == timedelta(hours=10, minutes=30)


def test_is_active_shortcut():
    assert _scheduler(_at(12, 0)).is_active() is True
    assert _scheduler(_at(2, 0)).is_active() is False
