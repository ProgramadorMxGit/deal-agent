"""Persistencia de versiones de selectores en SQLite.

Tabla `selector_versions`:

```
id, marketplace, context, key, selector_value, applied_at,
applied_by, fixture_path, test_pass, reverted_at
```

`test_pass=1` significa que los tests sintéticos pasaron contra el fixture.
Si más tarde el selector resulta no funcional en producción, se llama a
`revert(version_id, reason)` que escribe `reverted_at`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass
class SelectorVersion:
    id: int
    marketplace: str
    context: str
    key: str
    selector_value: str
    applied_at: str
    applied_by: str
    test_pass: bool
    fixture_path: Optional[str]
    reverted_at: Optional[str]


class SelectorVersioner:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.db = conn

    def record(
        self,
        *,
        marketplace: str,
        context: str,
        key: str,
        selector_value: str,
        applied_by: str,
        test_pass: bool,
        fixture_path: Optional[str] = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO selector_versions "
            "(marketplace, context, key, selector_value, applied_at, applied_by, "
            " fixture_path, test_pass) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                marketplace,
                context,
                key,
                selector_value,
                _now_iso(),
                applied_by,
                fixture_path,
                1 if test_pass else 0,
            ),
        )
        return cur.lastrowid

    def revert(self, version_id: int, *, reason: str) -> None:
        # Reusamos `fixture_path` para guardar la razón de revert: la tabla del
        # schema no tiene columna específica. Si en el futuro queremos
        # auditoría detallada, podemos añadir una nueva tabla.
        self.db.execute(
            "UPDATE selector_versions SET reverted_at = ?, applied_by = applied_by || ? "
            "WHERE id = ?",
            (_now_iso(), f" | reverted_reason={reason}", version_id),
        )

    def list_for(
        self, marketplace: str, context: str, *, limit: int = 10
    ) -> list[SelectorVersion]:
        rows = self.db.execute(
            "SELECT id, marketplace, context, key, selector_value, applied_at, "
            "       applied_by, fixture_path, test_pass, reverted_at "
            "FROM selector_versions "
            "WHERE marketplace = ? AND context = ? "
            "ORDER BY applied_at DESC LIMIT ?",
            (marketplace, context, limit),
        ).fetchall()
        out: list[SelectorVersion] = []
        for r in rows:
            out.append(
                SelectorVersion(
                    id=r["id"],
                    marketplace=r["marketplace"],
                    context=r["context"],
                    key=r["key"],
                    selector_value=r["selector_value"],
                    applied_at=r["applied_at"],
                    applied_by=r["applied_by"],
                    test_pass=bool(r["test_pass"]),
                    fixture_path=r["fixture_path"],
                    reverted_at=r["reverted_at"],
                )
            )
        return out
