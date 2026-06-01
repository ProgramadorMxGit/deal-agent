"""``OutboxStaleTask`` — paridad con ``_vps_cleanup_stale.py``.

Marca como ``state='discarded'`` los outbox ``pending`` con
``type='normal'``, ``previous_price`` no nulo en payload y
``enqueued_at`` estrictamente anterior al cutoff de 4 horas atrás
del clock inyectado.

Paridad estricta con ``scripts/_vps_cleanup_stale.py`` (ver
``.kiro/specs/bot-saneamiento-vps/design.md`` →
``OutboxStaleTask``):

- SELECT con join ``outbox o JOIN offers of JOIN products p`` y
  filtros::

      WHERE o.state = 'pending'
        AND o.type  = 'normal'
        AND o.enqueued_at < :cutoff_4h
        AND json_extract(o.message_payload_json, '$.previous_price') IS NOT NULL
        <MKT>

- Cutoff: ``:cutoff_4h = cutoff_iso(clock, 4)`` — la SQL usa ``<``
  estricto, así que una fila con ``enqueued_at`` exactamente igual
  al cutoff NO es candidato (paridad literal con el script).
- Type guard ``o.type='normal'`` excluye filas ``price_error`` y
  cualquier otro tipo.

``writes_last_attempt_at = True`` (Requirement 4.2, 4.8): el
``apply`` heredado de :class:`BaseSaneamientoTask` actualiza
``last_attempt_at`` con el ``now_iso`` del clock inyectado.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from ..time_source import cutoff_iso
from .base import BaseSaneamientoTask, CandidateRow, marketplace_clause

__all__ = ["OutboxStaleTask"]


class OutboxStaleTask(BaseSaneamientoTask):
    """Saneamiento_Task ``outbox-stale`` (paridad legacy)."""

    name = "outbox-stale"
    writes_last_attempt_at = True

    def select_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        marketplace: str,
        clock: Callable[[], datetime],
    ) -> list[CandidateRow]:
        # Cutoff de 4 horas atrás del clock inyectado, formateado
        # ISO-8601 ms con sufijo ``Z`` (paridad con el resto del bot).
        cutoff_4h = cutoff_iso(clock, 4)

        # ``marketplace_clause`` devuelve un fragmento con placeholders
        # ``?`` y los params correspondientes. Es seguro interpolarlo
        # con f-string: el helper sólo emite SQL hard-coded.
        mkt_sql, mkt_params = marketplace_clause(marketplace)

        rows = conn.execute(
            f"""
            SELECT o.id                                              AS outbox_id,
                   LOWER(p.marketplace)                              AS marketplace,
                   json_extract(o.message_payload_json, '$.title')   AS title
            FROM outbox o
            JOIN offers   of ON of.id = o.offer_id
            JOIN products p  ON p.id  = of.product_id
            WHERE o.state = 'pending'
              AND o.type  = 'normal'
              AND o.enqueued_at < ?
              AND json_extract(o.message_payload_json, '$.previous_price') IS NOT NULL
              {mkt_sql}
            ORDER BY o.id
            """,
            (cutoff_4h, *mkt_params),
        ).fetchall()

        return [
            CandidateRow(
                outbox_id=int(row["outbox_id"]),
                marketplace=row["marketplace"],
                title=row["title"],
            )
            for row in rows
        ]
