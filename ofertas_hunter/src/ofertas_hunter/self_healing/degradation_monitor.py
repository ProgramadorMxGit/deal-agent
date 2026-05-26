"""Monitor de degradación de extracción.

Mantiene un deque acotado por `(marketplace, context)` con `True/False`
representando éxito de extracción. Si `failure_rate >= threshold` y el deque
tiene al menos `min_samples`, el contexto se considera degradado.

Se usa antes de invocar al `DomHealer` para evitar healing prematuro y para
evitar healing en bucle.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class _ContextStats:
    samples: deque[bool] = field(default_factory=lambda: deque(maxlen=50))


class DegradationMonitor:
    def __init__(
        self,
        *,
        window: int = 20,
        threshold: float = 0.4,
        min_samples: int = 5,
    ) -> None:
        self.window = window
        self.threshold = threshold
        self.min_samples = min_samples
        self._stats: dict[tuple[str, str], _ContextStats] = {}

    def _key(self, marketplace: str, context: str) -> tuple[str, str]:
        return (marketplace.lower(), context.lower())

    def record(self, marketplace: str, context: str, *, success: bool) -> None:
        key = self._key(marketplace, context)
        stats = self._stats.setdefault(key, _ContextStats())
        if stats.samples.maxlen != self.window:
            stats.samples = deque(stats.samples, maxlen=self.window)
        stats.samples.append(success)

    def is_degraded(self, marketplace: str, context: str) -> bool:
        key = self._key(marketplace, context)
        stats = self._stats.get(key)
        if stats is None or len(stats.samples) < self.min_samples:
            return False
        failures = sum(1 for v in stats.samples if not v)
        rate = failures / len(stats.samples)
        return rate >= self.threshold

    def reset(self, marketplace: str, context: str) -> None:
        key = self._key(marketplace, context)
        if key in self._stats:
            self._stats[key].samples.clear()

    def stats_for(self, marketplace: str, context: str) -> dict:
        key = self._key(marketplace, context)
        stats = self._stats.get(key)
        if stats is None:
            return {"samples": 0, "failures": 0, "rate": 0.0, "degraded": False}
        failures = sum(1 for v in stats.samples if not v)
        rate = failures / len(stats.samples) if stats.samples else 0.0
        return {
            "samples": len(stats.samples),
            "failures": failures,
            "rate": round(rate, 3),
            "degraded": self.is_degraded(marketplace, context),
        }
