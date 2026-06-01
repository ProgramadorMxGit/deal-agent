"""Frontier de exploración persistente.

La tabla `frontier` ya existe en el schema (`migrations/001_init.sql`):

```
frontier(id, marketplace, url_canonical, url_type, score, added_at, retries)
```

`FrontierRepo` provee:
- `add(url, kind, score)` — INSERT OR IGNORE (UNIQUE en (marketplace, url_canonical)).
- `add_many(urls)` — bulk con clasificación automática.
- `pop(marketplace, kind=None, limit=N)` — devuelve N URLs ordenadas por score
  desc + added_at asc, y las **borra** del frontier (consumo destructivo).
- `peek(marketplace, kind=None, limit=N)` — sólo lee.
- `mark_visited(marketplace, url)` — INSERT en `visited_urls`.
- `is_visited(marketplace, url)` — bool.
- `count_pending(marketplace)` — métrica.

Cuando una URL ya fue visitada, no se vuelve a meter al frontier.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from .url_classifier import ClassifiedUrl, classify


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass
class FrontierItem:
    id: int
    marketplace: str
    url: str
    kind: str
    score: float
    retries: int
    added_at: str


class FrontierRepo:
    """Repositorio de la tabla `frontier`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.db = conn

    # ------------------------------------------------------------------
    # add
    # ------------------------------------------------------------------

    def add(
        self,
        url: str,
        *,
        kind: Optional[str] = None,
        score: Optional[float] = None,
    ) -> Optional[int]:
        """Añade una URL si no estaba ya visitada o en frontier.

        Devuelve el id del frontier item, o None si fue rechazada.
        """
        info = classify(url)
        if info.kind in ("unknown", ""):
            return None
        if self.is_visited(info.marketplace, info.url):
            return None
        kind_final = kind or info.kind
        score_final = score if score is not None else info.score
        try:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO frontier "
                "(marketplace, url_canonical, url_type, score, added_at, retries) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (info.marketplace, info.url, kind_final, score_final, _now_iso()),
            )
            return cur.lastrowid or None
        except sqlite3.Error as exc:
            logger.debug("frontier.add failed: %s", exc)
            return None

    def add_many(self, urls: Iterable[str]) -> int:
        """Añade varias URLs, devuelve cuántas se aceptaron de verdad."""
        n = 0
        for url in urls:
            if self.add(url):
                n += 1
        return n

    def add_classified(self, info: ClassifiedUrl) -> Optional[int]:
        if info.kind in ("unknown", ""):
            return None
        return self.add(info.url, kind=info.kind, score=info.score)

    # ------------------------------------------------------------------
    # pop / peek
    # ------------------------------------------------------------------

    def pop(
        self,
        marketplace: str,
        *,
        kind: Optional[str] = None,
        limit: int = 5,
    ) -> list[FrontierItem]:
        """Saca las `limit` URLs de mayor score; las borra del frontier."""
        items = self.peek(marketplace, kind=kind, limit=limit)
        if not items:
            return []
        ids = tuple(it.id for it in items)
        placeholders = ",".join("?" for _ in ids)
        self.db.execute(f"DELETE FROM frontier WHERE id IN ({placeholders})", ids)
        return items

    def peek(
        self,
        marketplace: str,
        *,
        kind: Optional[str] = None,
        limit: int = 5,
    ) -> list[FrontierItem]:
        if kind is None:
            rows = self.db.execute(
                "SELECT id, marketplace, url_canonical, url_type, score, retries, added_at "
                "FROM frontier WHERE marketplace = ? "
                "ORDER BY score DESC, added_at ASC LIMIT ?",
                (marketplace, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id, marketplace, url_canonical, url_type, score, retries, added_at "
                "FROM frontier WHERE marketplace = ? AND url_type = ? "
                "ORDER BY score DESC, added_at ASC LIMIT ?",
                (marketplace, kind, limit),
            ).fetchall()
        return [
            FrontierItem(
                id=r["id"],
                marketplace=r["marketplace"],
                url=r["url_canonical"],
                kind=r["url_type"],
                score=r["score"],
                retries=r["retries"],
                added_at=r["added_at"],
            )
            for r in rows
        ]

    def pop_category_aware(
        self,
        marketplace: str,
        *,
        kind: Optional[str] = None,
        limit: int = 5,
        deficit_categories: Optional[list] = None,
        saturated_categories: Optional[list] = None,
        deficit_boost: float = 2.0,
        saturated_penalty: float = 0.5,
        candidate_multiplier: int = 6,
    ) -> list[FrontierItem]:
        """Pop con sesgo por categoría deficitaria.

        Lee un superset de candidatos (limit*candidate_multiplier), reordena por
        score ajustado (boost a deficit, penalty a saturadas) y consume los top
        `limit`. Si no hay plan de categorías, equivale a `pop` normal.
        """
        from .frontier_category import category_of_url, adjusted_score

        if not deficit_categories and not saturated_categories:
            return self.pop(marketplace, kind=kind, limit=limit)

        pool = self.peek(marketplace, kind=kind, limit=max(limit, limit * candidate_multiplier))
        if not pool:
            return []
        ranked = sorted(
            pool,
            key=lambda it: adjusted_score(
                it.score, category_of_url(it.url),
                deficit_categories, saturated_categories,
                deficit_boost=deficit_boost, saturated_penalty=saturated_penalty,
            ),
            reverse=True,
        )
        chosen = ranked[:limit]
        ids = tuple(it.id for it in chosen)
        if ids:
            placeholders = ",".join("?" for _ in ids)
            self.db.execute(f"DELETE FROM frontier WHERE id IN ({placeholders})", ids)
        return chosen

    def increment_retries(self, item_id: int) -> None:
        self.db.execute(
            "UPDATE frontier SET retries = retries + 1 WHERE id = ?", (item_id,)
        )

    def requeue(self, item: FrontierItem, *, score_penalty: float = 1.0) -> None:
        """Reinserta un item bajándole el score (caso fallo transitorio)."""
        self.add(item.url, kind=item.kind, score=max(0.0, item.score - score_penalty))

    # ------------------------------------------------------------------
    # visited
    # ------------------------------------------------------------------

    def mark_visited(self, marketplace: str, url: str) -> None:
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO visited_urls (marketplace, url_canonical, visited_at) "
                "VALUES (?, ?, ?)",
                (marketplace, url, _now_iso()),
            )
        except sqlite3.Error:
            pass

    def is_visited(self, marketplace: str, url: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM visited_urls WHERE marketplace = ? AND url_canonical = ? LIMIT 1",
            (marketplace, url),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # métricas
    # ------------------------------------------------------------------

    def count_pending(self, marketplace: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM frontier WHERE marketplace = ?",
            (marketplace,),
        ).fetchone()
        return int(row["n"])

    def count_visited(self, marketplace: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM visited_urls WHERE marketplace = ?",
            (marketplace,),
        ).fetchone()
        return int(row["n"])
