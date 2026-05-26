"""Heartbeat helpers para los agentes.

Cualquier agente que quiera ser supervisado por el watchdog debe llamar a
`heartbeat()` periódicamente. La función actualiza `agent_runs.last_heartbeat`
y, si la fila no existe, la crea.

`AgentRunRegistry` es la API mínima sobre la tabla `agent_runs` (ver
`migrations/001_init.sql`):

```
agent_runs(
    id, agent_name, started_at, ended_at, status,
    summary_json, last_heartbeat
)
```
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass
class AgentRunHandle:
    """Identifica una corrida activa de un agente."""

    run_id: int
    agent_name: str


class AgentRunRegistry:
    """Registry de corridas de agentes en SQLite."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.db = conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, agent_name: str, summary: Optional[dict] = None) -> AgentRunHandle:
        cur = self.db.execute(
            "INSERT INTO agent_runs (agent_name, started_at, status, summary_json, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                agent_name,
                self._now(),
                "running",
                json.dumps(summary or {}, ensure_ascii=False),
                self._now(),
            ),
        )
        return AgentRunHandle(run_id=cur.lastrowid, agent_name=agent_name)

    def heartbeat(self, handle: AgentRunHandle) -> None:
        self.db.execute(
            "UPDATE agent_runs SET last_heartbeat = ? WHERE id = ?",
            (self._now(), handle.run_id),
        )

    def finish(
        self,
        handle: AgentRunHandle,
        *,
        status: str = "ok",
        summary: Optional[dict] = None,
    ) -> None:
        if status not in ("ok", "error", "killed"):
            raise ValueError(f"status inválido: {status}")
        existing = self.db.execute(
            "SELECT summary_json FROM agent_runs WHERE id = ?", (handle.run_id,)
        ).fetchone()
        merged: dict = {}
        if existing and existing["summary_json"]:
            try:
                merged = json.loads(existing["summary_json"]) or {}
            except (TypeError, json.JSONDecodeError):
                merged = {}
        if summary:
            merged.update(summary)
        self.db.execute(
            "UPDATE agent_runs SET ended_at = ?, status = ?, summary_json = ? "
            "WHERE id = ?",
            (
                self._now(),
                status,
                json.dumps(merged, ensure_ascii=False),
                handle.run_id,
            ),
        )

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def last_heartbeat(self, agent_name: str) -> Optional[datetime]:
        row = self.db.execute(
            "SELECT last_heartbeat FROM agent_runs WHERE agent_name = ? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (agent_name,),
        ).fetchone()
        if not row or not row["last_heartbeat"]:
            return None
        return _parse_dt(row["last_heartbeat"])

    def latest_run(self, agent_name: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT id, agent_name, started_at, ended_at, status, summary_json, "
            "       last_heartbeat "
            "FROM agent_runs WHERE agent_name = ? ORDER BY id DESC LIMIT 1",
            (agent_name,),
        ).fetchone()
        if not row:
            return None
        return dict(row)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = ["AgentRunHandle", "AgentRunRegistry"]
