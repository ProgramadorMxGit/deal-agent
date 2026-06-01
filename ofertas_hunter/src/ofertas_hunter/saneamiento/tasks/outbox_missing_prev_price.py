"""``OutboxMissingPrevPriceTask`` — paridad con ``cleanup_outbox_missing_prev_price.py``.

Marca como ``state='discarded'`` los outbox ``pending`` cuyo payload
no tiene ``previous_price`` o ``discount_percent`` truthy, excluyendo
los offers con ``classification='price_error_confirmed'``
(case-insensitive). Esos items nunca pasarían el formatter de oferta
normal y sólo contaminan los ciclos del dispatcher.

Paridad estricta con ``scripts/cleanup_outbox_missing_prev_price.py``
(ver ``.kiro/specs/bot-saneamiento-vps/design.md`` →
``OutboxMissingPrevPriceTask``):

- SELECT con join ``outbox o JOIN offers of JOIN products p`` y
  filtro ``o.state='pending'`` + ``marketplace_clause``.
- Filtrado en Python por ``classification == 'price_error_confirmed'``
  (la SELECT ya devuelve ``LOWER(of.classification)`` → comparación
  efectivamente case-insensitive).
- Filtrado por **truthiness** del payload: ``not prev or not disc``,
  por lo que ``None``, ``0``, ``0.0`` y ``""`` cuentan como faltantes.

``writes_last_attempt_at = False`` (Requirement 4.5): el ``apply``
heredado de :class:`BaseSaneamientoTask` no toca ``last_attempt_at``,
respetando la semántica del script legacy.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Callable

from .base import BaseSaneamientoTask, CandidateRow, marketplace_clause

__all__ = ["OutboxMissingPrevPriceTask"]


class OutboxMissingPrevPriceTask(BaseSaneamientoTask):
    """Saneamiento_Task ``outbox-missing-prev-price`` (paridad legacy)."""

    name = "outbox-missing-prev-price"
    writes_last_attempt_at = False

    def select_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        marketplace: str,
        clock: Callable[[], datetime],
    ) -> list[CandidateRow]:
        # ``marketplace_clause`` devuelve un fragmento con placeholders
        # ``?`` y los params. Es seguro interpolarlo con f-string porque
        # no contiene input de usuario, sólo SQL hard-coded del helper.
        mkt_sql, mkt_params = marketplace_clause(marketplace)

        rows = conn.execute(
            f"""
            SELECT o.id                            AS outbox_id,
                   o.message_payload_json          AS payload_json,
                   LOWER(p.marketplace)            AS marketplace,
                   LOWER(of.classification)        AS classification,
                   p.title                         AS product_title
            FROM outbox o
            JOIN offers   of ON of.id = o.offer_id
            JOIN products p  ON p.id  = of.product_id
            WHERE o.state = 'pending'
              {mkt_sql}
            ORDER BY o.id
            """,
            mkt_params,
        ).fetchall()

        candidates: list[CandidateRow] = []
        for row in rows:
            classification = (
                row["classification"]
                if isinstance(row, sqlite3.Row)
                else row[3]
            ) or ""
            # ``classification`` ya viene en lowercase por el ``LOWER()``
            # del SELECT → la comparación literal cubre todas las
            # variantes de case (paridad con el script legacy).
            if classification == "price_error_confirmed":
                continue

            payload_str = (
                row["payload_json"]
                if isinstance(row, sqlite3.Row)
                else row[1]
            ) or "{}"
            try:
                payload = json.loads(payload_str)
            except (json.JSONDecodeError, TypeError):
                payload = {}

            prev = payload.get("previous_price")
            disc = payload.get("discount_percent")

            # Paridad literal con ``not prev or not disc`` del script:
            # rechaza ``None``, ``0``, ``0.0`` y ``""`` indistintamente.
            if not prev or not disc:
                outbox_id = (
                    row["outbox_id"]
                    if isinstance(row, sqlite3.Row)
                    else row[0]
                )
                mkt = (
                    row["marketplace"]
                    if isinstance(row, sqlite3.Row)
                    else row[2]
                )
                title = (
                    row["product_title"]
                    if isinstance(row, sqlite3.Row)
                    else row[4]
                )
                candidates.append(
                    CandidateRow(
                        outbox_id=int(outbox_id),
                        marketplace=mkt,
                        title=title,
                        extra={
                            "previous_price": prev,
                            "discount_percent": disc,
                        },
                    )
                )

        return candidates
