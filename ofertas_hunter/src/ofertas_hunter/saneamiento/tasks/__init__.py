"""Registry of Saneamiento_Task classes in canonical execution order.

This module exposes:

- ``BaseSaneamientoTask``, ``CandidateRow``, ``marketplace_clause`` — shared base.
- ``TASK_REGISTRY`` — ordered ``dict[str, type[BaseSaneamientoTask]]`` mapping
  each task ``name`` to its class. Iteration order matches the canonical run
  order used when ``--task=all`` (Requirement 4.1):

    1. ``outbox-duplicates``
    2. ``outbox-missing-prev-price``
    3. ``outbox-bad-discounts``
    4. ``outbox-stale``
"""

from __future__ import annotations

from .base import BaseSaneamientoTask, CandidateRow, marketplace_clause
from .outbox_bad_discounts import OutboxBadDiscountsTask
from .outbox_duplicates import OutboxDuplicatesTask
from .outbox_missing_prev_price import OutboxMissingPrevPriceTask
from .outbox_stale import OutboxStaleTask

__all__ = [
    "BaseSaneamientoTask",
    "CandidateRow",
    "marketplace_clause",
    "OutboxBadDiscountsTask",
    "OutboxDuplicatesTask",
    "OutboxMissingPrevPriceTask",
    "OutboxStaleTask",
    "TASK_REGISTRY",
]

# Order matters: this is the canonical execution order for ``--task=all``
# (Requirement 4.1). dict preserves insertion order in Python 3.7+.
TASK_REGISTRY: dict[str, type[BaseSaneamientoTask]] = {
    OutboxDuplicatesTask.name: OutboxDuplicatesTask,
    OutboxMissingPrevPriceTask.name: OutboxMissingPrevPriceTask,
    OutboxBadDiscountsTask.name: OutboxBadDiscountsTask,
    OutboxStaleTask.name: OutboxStaleTask,
}
