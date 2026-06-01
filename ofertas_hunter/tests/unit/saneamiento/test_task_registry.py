"""Tests for ``ofertas_hunter.saneamiento.tasks.TASK_REGISTRY``.

Validates Requirements 1.3 and 4.1: the registry must contain exactly
the four canonical tasks, in the canonical execution order used when
``--task=all``.
"""

from __future__ import annotations

from ofertas_hunter.saneamiento.tasks import (
    TASK_REGISTRY,
    BaseSaneamientoTask,
    OutboxBadDiscountsTask,
    OutboxDuplicatesTask,
    OutboxMissingPrevPriceTask,
    OutboxStaleTask,
)

CANONICAL_ORDER = [
    "outbox-duplicates",
    "outbox-missing-prev-price",
    "outbox-bad-discounts",
    "outbox-stale",
]


def test_registry_has_exactly_four_entries() -> None:
    """TASK_REGISTRY must register exactly the four canonical tasks."""
    assert len(TASK_REGISTRY) == 4
    assert set(TASK_REGISTRY.keys()) == set(CANONICAL_ORDER)


def test_registry_iteration_order_is_canonical() -> None:
    """Iteration order must match the canonical ``--task=all`` order.

    Requirement 4.1: when running ``--task=all`` the orchestrator iterates
    ``TASK_REGISTRY`` and that order is part of the public contract.
    """
    assert list(TASK_REGISTRY.keys()) == CANONICAL_ORDER


def test_registry_maps_each_name_to_its_class() -> None:
    """Each entry must map to its concrete ``BaseSaneamientoTask`` subclass."""
    assert TASK_REGISTRY["outbox-duplicates"] is OutboxDuplicatesTask
    assert TASK_REGISTRY["outbox-missing-prev-price"] is OutboxMissingPrevPriceTask
    assert TASK_REGISTRY["outbox-bad-discounts"] is OutboxBadDiscountsTask
    assert TASK_REGISTRY["outbox-stale"] is OutboxStaleTask


def test_registry_values_are_base_task_subclasses() -> None:
    """Every registered class must derive from ``BaseSaneamientoTask``."""
    for cls in TASK_REGISTRY.values():
        assert issubclass(cls, BaseSaneamientoTask)


def test_registry_keys_match_class_name_attribute() -> None:
    """Each registry key must equal its class' ``name`` attribute."""
    for key, cls in TASK_REGISTRY.items():
        assert key == cls.name
