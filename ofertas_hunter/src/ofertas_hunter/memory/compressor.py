"""Memory compressor: limita el tamaño de tablas auditables y genera resúmenes.

Tablas que se compactan:

| Tabla                  | Política por defecto                                |
|------------------------|-----------------------------------------------------|
| `dom_snapshots`        | mantener N más recientes (default 200)              |
| `runtime_events`       | TTL 30 días (mantener más reciente, sin tope)       |
| `discarded_candidates` | TTL 30 días + máx 5000                              |
| `agent_runs`           | mantener N más recientes (default 1000)             |

Resúmenes (`memory_summaries`):

- `discard_reasons`: top 10 razones de descarte agrupadas por `source`.
- `unstable_selectors`: selectores con > N versiones en 30d.
- `runtime_events_by_severity`: agregados por severidad/kind.
- `marketplace_observations`: cantidad de `price_observations` por marketplace.

Ejecución típica: cada 6h o vía CLI/script.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


@dataclass
class CompressorConfig:
    dom_snapshots_keep: int = 200
    runtime_events_ttl_days: int = 30
    discarded_candidates_ttl_days: int = 30
    discarded_candidates_keep_max: int = 5000
    agent_runs_keep: int = 1000
    unstable_selector_threshold: int = 3  # versiones en `unstable_window_days`
    unstable_window_days: int = 30


# ---------------------------------------------------------------------------
# Reporte
# ---------------------------------------------------------------------------


@dataclass
class CompressionReport:
    deleted_dom_snapshots: int = 0
    deleted_runtime_events: int = 0
    deleted_discarded_candidates: int = 0
    deleted_agent_runs: int = 0
    summaries_generated: int = 0
    summary_kinds: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Compressor
# ---------------------------------------------------------------------------


class MemoryCompressor:
    """Compacta tablas y genera summaries en `memory_summaries`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        config: Optional[CompressorConfig] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.db = conn
        self.config = config or CompressorConfig()
        self._clock = clock

    # ------------------------------------------------------------------
    # API principal
    # ------------------------------------------------------------------

    def run(self) -> CompressionReport:
        report = CompressionReport()
        report.deleted_dom_snapshots = self._compact_dom_snapshots()
        report.deleted_runtime_events = self._compact_runtime_events()
        report.deleted_discarded_candidates = self._compact_discarded_candidates()
        report.deleted_agent_runs = self._compact_agent_runs()

        kinds = self._generate_summaries()
        report.summary_kinds = kinds
        report.summaries_generated = len(kinds)
        return report

    # ------------------------------------------------------------------
    # Compactación por tabla
    # ------------------------------------------------------------------

    def _compact_dom_snapshots(self) -> int:
        keep = self.config.dom_snapshots_keep
        if keep <= 0:
            return 0
        cur = self.db.execute(
            "DELETE FROM dom_snapshots WHERE id NOT IN ("
            "  SELECT id FROM dom_snapshots ORDER BY id DESC LIMIT ?"
            ")",
            (keep,),
        )
        return cur.rowcount or 0

    def _compact_runtime_events(self) -> int:
        cutoff = self._cutoff_iso(days=self.config.runtime_events_ttl_days)
        cur = self.db.execute(
            "DELETE FROM runtime_events WHERE created_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    def _compact_discarded_candidates(self) -> int:
        cutoff = self._cutoff_iso(days=self.config.discarded_candidates_ttl_days)
        cur1 = self.db.execute(
            "DELETE FROM discarded_candidates WHERE created_at < ?",
            (cutoff,),
        )
        deleted_ttl = cur1.rowcount or 0
        # Cap absoluto por si TTL no fue suficiente
        cur2 = self.db.execute(
            "DELETE FROM discarded_candidates WHERE id NOT IN ("
            "  SELECT id FROM discarded_candidates ORDER BY id DESC LIMIT ?"
            ")",
            (self.config.discarded_candidates_keep_max,),
        )
        deleted_cap = cur2.rowcount or 0
        return deleted_ttl + deleted_cap

    def _compact_agent_runs(self) -> int:
        keep = self.config.agent_runs_keep
        if keep <= 0:
            return 0
        cur = self.db.execute(
            "DELETE FROM agent_runs WHERE id NOT IN ("
            "  SELECT id FROM agent_runs ORDER BY id DESC LIMIT ?"
            ")",
            (keep,),
        )
        return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------

    def _generate_summaries(self) -> list[str]:
        kinds: list[str] = []
        kinds.append(self._summary_discard_reasons())
        kinds.append(self._summary_unstable_selectors())
        kinds.append(self._summary_runtime_events())
        kinds.append(self._summary_marketplace_observations())
        return [k for k in kinds if k]

    def _summary_discard_reasons(self) -> str:
        rows = self.db.execute(
            "SELECT source, reason, COUNT(*) AS n FROM discarded_candidates "
            "GROUP BY source, reason ORDER BY n DESC LIMIT 10"
        ).fetchall()
        content = {
            "kind": "discard_reasons",
            "generated_at": self._now_iso(),
            "rows": [
                {"source": r["source"], "reason": r["reason"], "count": r["n"]}
                for r in rows
            ],
        }
        self._write_summary("discard_reasons", content)
        return "discard_reasons"

    def _summary_unstable_selectors(self) -> str:
        cutoff = self._cutoff_iso(days=self.config.unstable_window_days)
        threshold = self.config.unstable_selector_threshold
        rows = self.db.execute(
            "SELECT marketplace, context, key, COUNT(*) AS n "
            "FROM selector_versions WHERE applied_at >= ? "
            "GROUP BY marketplace, context, key HAVING n >= ? "
            "ORDER BY n DESC LIMIT 20",
            (cutoff, threshold),
        ).fetchall()
        content = {
            "kind": "unstable_selectors",
            "generated_at": self._now_iso(),
            "window_days": self.config.unstable_window_days,
            "threshold": threshold,
            "rows": [
                {
                    "marketplace": r["marketplace"],
                    "context": r["context"],
                    "key": r["key"],
                    "versions": r["n"],
                }
                for r in rows
            ],
        }
        self._write_summary("unstable_selectors", content)
        return "unstable_selectors"

    def _summary_runtime_events(self) -> str:
        rows = self.db.execute(
            "SELECT severity, kind, COUNT(*) AS n FROM runtime_events "
            "GROUP BY severity, kind ORDER BY "
            "  CASE severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2 "
            "                 WHEN 'warning' THEN 3 ELSE 4 END, n DESC LIMIT 50"
        ).fetchall()
        content = {
            "kind": "runtime_events_by_severity",
            "generated_at": self._now_iso(),
            "rows": [
                {"severity": r["severity"], "kind": r["kind"], "count": r["n"]}
                for r in rows
            ],
        }
        self._write_summary("runtime_events_by_severity", content)
        return "runtime_events_by_severity"

    def _summary_marketplace_observations(self) -> str:
        rows = self.db.execute(
            "SELECT p.marketplace, COUNT(po.id) AS n "
            "FROM price_observations po JOIN products p ON p.id = po.product_id "
            "GROUP BY p.marketplace ORDER BY n DESC"
        ).fetchall()
        content = {
            "kind": "marketplace_observations",
            "generated_at": self._now_iso(),
            "rows": [
                {"marketplace": r["marketplace"], "observations": r["n"]} for r in rows
            ],
        }
        self._write_summary("marketplace_observations", content)
        return "marketplace_observations"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write_summary(self, kind: str, content: dict) -> None:
        self.db.execute(
            "INSERT INTO memory_summaries (kind, content, generated_at) VALUES (?, ?, ?)",
            (kind, json.dumps(content, ensure_ascii=False), self._now_iso()),
        )

    def _now_iso(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def _cutoff_iso(self, *, days: int) -> str:
        cutoff = self._clock() - timedelta(days=days)
        return (
            cutoff.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )


__all__ = ["CompressionReport", "CompressorConfig", "MemoryCompressor"]
