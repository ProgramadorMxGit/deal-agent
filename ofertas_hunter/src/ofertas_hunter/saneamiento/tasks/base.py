"""Clase base y helpers compartidos de las ``Saneamiento_Task``.

Contiene:

- :class:`BaseSaneamientoTask` — ABC con ``name``,
  ``writes_last_attempt_at``, ``select_candidates(...)`` abstracto y
  ``apply(...)`` con el ``UPDATE outbox`` compartido por todas las tasks.
- :class:`CandidateRow` — frozen dataclass que cada
  ``select_candidates`` produce.
- :func:`marketplace_clause` — helper para inyectar el filtro por
  marketplace en los SELECT de cada task.

Diseño en ``.kiro/specs/bot-saneamiento-vps/design.md`` (sección
``Components and Interfaces`` → ``BaseSaneamientoTask`` y helper
``_marketplace_filter_sql``). Requirements 4.7, 5.2, 5.3.
"""

from __future__ import annotations

import abc
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar

from ..time_source import now_utc_iso

__all__ = ["BaseSaneamientoTask", "CandidateRow", "marketplace_clause"]


# ---------------------------------------------------------------------------
# CandidateRow — output de cada ``select_candidates``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRow:
    """Fila candidata a saneamiento.

    ``outbox_id`` es la PK de la fila ``outbox`` que se mutará en
    ``apply``. ``marketplace`` y ``title`` se incluyen para dar contexto
    a los logs/eventos. ``extra`` permite a cada task adjuntar metadata
    específica (p. ej. ``{"reason": "duplicate"}``).
    """

    outbox_id: int
    marketplace: str | None
    title: str | None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# marketplace_clause — fragmento SQL compartido por todos los SELECT.
# ---------------------------------------------------------------------------


def marketplace_clause(marketplace: str) -> tuple[str, tuple[Any, ...]]:
    """Devuelve ``(sql_fragment, params)`` para filtrar por marketplace.

    - ``"all"``          → ``("", ())`` (sin filtro).
    - ``"amazon"``       → ``(" AND LOWER(p.marketplace) = ? ", ("amazon",))``.
    - ``"mercadolibre"`` → ``(" AND LOWER(p.marketplace) = ? ", ("mercadolibre",))``.

    Para cualquier otro valor se eleva ``ValueError`` (defensivo: la CLI
    ya valida los inputs aceptados antes de llamar a las tasks).
    """

    if marketplace == "all":
        return ("", ())
    if marketplace in ("amazon", "mercadolibre"):
        return (" AND LOWER(p.marketplace) = ? ", (marketplace,))
    raise ValueError(
        f"marketplace inválido: {marketplace!r} "
        "(esperado uno de: 'all', 'amazon', 'mercadolibre')"
    )


# ---------------------------------------------------------------------------
# BaseSaneamientoTask — ABC con UPDATE compartido.
# ---------------------------------------------------------------------------


class BaseSaneamientoTask(abc.ABC):
    """Clase base para todas las tareas de saneamiento.

    Subclases declaran ``name`` y, opcionalmente,
    ``writes_last_attempt_at`` (default ``True``). Sólo deben
    implementar ``select_candidates``: el ``apply`` por defecto se
    encarga del ``UPDATE outbox`` para garantizar paridad de
    semántica entre todas las tasks (Correctness Properties:
    idempotencia + no-borrado).
    """

    name: ClassVar[str]
    writes_last_attempt_at: ClassVar[bool] = True

    @abc.abstractmethod
    def select_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        marketplace: str,
        clock: Callable[[], datetime],
    ) -> list[CandidateRow]:
        """Devuelve los candidatos a descartar para este marketplace."""

    def apply(
        self,
        conn: sqlite3.Connection,
        candidates: list[CandidateRow],
        *,
        clock: Callable[[], datetime],
    ) -> int:
        """Marca cada ``candidate`` como ``discarded`` (idempotente).

        Aplica un único ``UPDATE outbox`` con guard ``state='pending'``,
        de modo que filas ya ``discarded``/``sent`` nunca se reescriben
        (Requirement 4.4 + Correctness Property "no-borrado").

        Si ``writes_last_attempt_at=True`` (default), actualiza también
        ``last_attempt_at`` con el ``now_iso`` derivado del ``clock``
        inyectado. Cuando es ``False`` (paridad con
        ``OutboxMissingPrevPriceTask``, Requirement 4.5), la columna
        ``last_attempt_at`` no se modifica.

        Devuelve el número de filas efectivamente mutadas
        (``cursor.rowcount`` agregado del UPDATE).
        """

        if not candidates:
            return 0

        ids = [c.outbox_id for c in candidates]
        placeholders = ",".join("?" * len(ids))

        if self.writes_last_attempt_at:
            now_iso = now_utc_iso(clock)
            sql = (
                "UPDATE outbox SET state='discarded', last_attempt_at=? "
                f"WHERE id IN ({placeholders}) AND state='pending'"
            )
            params: tuple[Any, ...] = (now_iso, *ids)
        else:
            sql = (
                "UPDATE outbox SET state='discarded' "
                f"WHERE id IN ({placeholders}) AND state='pending'"
            )
            params = tuple(ids)

        cursor = conn.execute(sql, params)
        return cursor.rowcount or 0
