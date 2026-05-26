"""Tests del DegradationMonitor."""

from __future__ import annotations

from ofertas_hunter.self_healing.degradation_monitor import DegradationMonitor


def test_not_degraded_with_few_samples():
    mon = DegradationMonitor(window=20, threshold=0.4, min_samples=5)
    for _ in range(3):
        mon.record("amazon", "product_page", success=False)
    assert mon.is_degraded("amazon", "product_page") is False


def test_degraded_when_failure_rate_exceeds_threshold():
    mon = DegradationMonitor(window=10, threshold=0.4, min_samples=5)
    for _ in range(5):
        mon.record("amazon", "product_page", success=False)
    for _ in range(5):
        mon.record("amazon", "product_page", success=True)
    # 5 failures / 10 = 0.5 >= 0.4
    assert mon.is_degraded("amazon", "product_page") is True


def test_not_degraded_below_threshold():
    mon = DegradationMonitor(window=10, threshold=0.4, min_samples=5)
    for _ in range(2):
        mon.record("ml", "product_page", success=False)
    for _ in range(8):
        mon.record("ml", "product_page", success=True)
    assert mon.is_degraded("ml", "product_page") is False


def test_window_truncates_old_samples():
    mon = DegradationMonitor(window=5, threshold=0.4, min_samples=5)
    # 5 fallos primero
    for _ in range(5):
        mon.record("ml", "x", success=False)
    assert mon.is_degraded("ml", "x") is True
    # Ahora 5 éxitos: el deque desplaza los fallos
    for _ in range(5):
        mon.record("ml", "x", success=True)
    assert mon.is_degraded("ml", "x") is False


def test_independent_contexts():
    mon = DegradationMonitor(window=10, threshold=0.4, min_samples=5)
    for _ in range(5):
        mon.record("amazon", "product_page", success=False)
    for _ in range(5):
        mon.record("ml", "product_page", success=True)
    assert mon.stats_for("amazon", "product_page")["degraded"] is True
    assert mon.stats_for("ml", "product_page")["degraded"] is False


def test_reset_clears_samples():
    mon = DegradationMonitor(window=10, threshold=0.4, min_samples=5)
    for _ in range(8):
        mon.record("amazon", "product_page", success=False)
    assert mon.is_degraded("amazon", "product_page") is True
    mon.reset("amazon", "product_page")
    assert mon.is_degraded("amazon", "product_page") is False
