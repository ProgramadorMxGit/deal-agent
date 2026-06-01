"""Tests de detección de ventana nocturna para nightly_maintenance."""

from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from ofertas_hunter.maintenance.nightly import (
    NightlyMaintenanceConfig,
    is_within_window,
)


def _at(h, m, tz=timezone.utc):
    return datetime(2026, 5, 30, h, m, tzinfo=tz)


def test_within_window_simple():
    start, end = time(3, 0), time(4, 30)
    assert is_within_window(_at(3, 0), start, end) is True
    assert is_within_window(_at(4, 0), start, end) is True
    assert is_within_window(_at(4, 29), start, end) is True


def test_outside_window_simple():
    start, end = time(3, 0), time(4, 30)
    assert is_within_window(_at(2, 59), start, end) is False
    assert is_within_window(_at(4, 30), start, end) is False  # end exclusivo
    assert is_within_window(_at(12, 0), start, end) is False
    assert is_within_window(_at(23, 0), start, end) is False


def test_window_crossing_midnight():
    start, end = time(23, 30), time(1, 0)
    assert is_within_window(_at(23, 45), start, end) is True
    assert is_within_window(_at(0, 30), start, end) is True
    assert is_within_window(_at(1, 0), start, end) is False
    assert is_within_window(_at(12, 0), start, end) is False


def test_config_defaults():
    c = NightlyMaintenanceConfig()
    assert c.enabled is True
    assert c.start == time(3, 0)
    assert c.end == time(4, 30)
    assert c.timezone_name == "America/Mexico_City"
    assert c.keep_mcp_tool_called_hours == 48
    assert c.keep_verbose_days == 7
    assert c.vacuum_enabled is True
    assert c.vacuum_min_free_gb == 10.0
    assert c.batch_size == 100000
    assert c.restart_after is True
    assert c.max_seconds == 3600
    assert c.create_backup is False
    assert c.backup_retention == 1


def test_config_from_settings_like():
    class S:
        nightly_maintenance_enabled = True
        nightly_maintenance_start = "02:15"
        nightly_maintenance_end = "03:45"
        nightly_maintenance_timezone = "UTC"
        runtime_events_keep_mcp_tool_called_hours = 24
        runtime_events_keep_verbose_days = 3
        nightly_vacuum_enabled = False
        nightly_vacuum_min_free_gb = 5.0
        nightly_maintenance_batch_size = 50000
        nightly_maintenance_restart_after = False
        nightly_maintenance_max_seconds = 1200
        nightly_maintenance_create_backup = True
        nightly_maintenance_backup_retention = 2

    c = NightlyMaintenanceConfig.from_settings(S())
    assert c.enabled is True
    assert c.start == time(2, 15)
    assert c.end == time(3, 45)
    assert c.timezone_name == "UTC"
    assert c.keep_mcp_tool_called_hours == 24
    assert c.keep_verbose_days == 3
    assert c.vacuum_enabled is False
    assert c.vacuum_min_free_gb == 5.0
    assert c.batch_size == 50000
    assert c.restart_after is False
    assert c.max_seconds == 1200
    assert c.create_backup is True
    assert c.backup_retention == 2
