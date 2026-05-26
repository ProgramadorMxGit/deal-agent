"""Helper para `runtime_events` (auditoría operativa).

Reusa la tabla del schema:

```
runtime_events(
    id, kind, severity, payload_json, created_at, acknowledged_at
)
```

Los agentes ya emiten eventos directos en algunos casos (cookie expiry,
captcha). Centralizamos aquí para que el watchdog y otros agentes futuros
no dupliquen el INSERT.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def emit_runtime_event(
    conn: sqlite3.Connection,
    *,
    kind: str,
    severity: str,
    payload: Optional[dict] = None,
) -> int:
    """Inserta un runtime_event y devuelve su id.

    `severity` ∈ {info, warning, error, critical}.
    """
    if severity not in ("info", "warning", "error", "critical"):
        raise ValueError(f"severity inválida: {severity}")
    cur = conn.execute(
        "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            kind,
            severity,
            json.dumps(payload or {}, ensure_ascii=False),
            _now_iso(),
        ),
    )
    return cur.lastrowid
