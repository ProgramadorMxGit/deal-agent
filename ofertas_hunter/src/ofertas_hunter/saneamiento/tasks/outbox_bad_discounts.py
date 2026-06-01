"""``OutboxBadDiscountsTask`` — paridad con ``_vps_cleanup_bad_discounts.py``.

Marca como ``state='discarded'`` los outbox ``pending`` cuyo payload
tenga descuentos inconsistentes: ``prev <= cur``, ``prev <= 0`` o
``cur <= 0`` (rama de signos inválidos), o bien
``abs(real_pct - discount_percent) > 3.0`` donde
``real_pct = (prev - cur) / prev * 100.0`` (rama de mismatch).

Paridad estricta con ``scripts/_vps_cleanup_bad_discounts.py``
(ver ``.kiro/specs/bot-saneamiento-vps/design.md`` →
``OutboxBadDiscountsTask``):

- SELECT con join ``outbox o JOIN offers of JOIN products p`` y
  filtro ``o.state='pending'`` + ``marketplace_clause``.
- ``json_extract`` de ``current_price``, ``previous_price`` y
  ``discount_percent`` desde ``message_payload_json``.
- Filtrado en Python con dos ramas exactas al script:

    if cur is None or prev is None or disc is None:
        continue                  # cualquier None ⇒ NO candidato
    if prev <= 0 or cur <= 0 or prev <= cur:
        yield candidate           # signos inválidos o prev<=cur ⇒ candidato
        continue
    real = (prev - cur) / prev * 100.0
    if abs(real - disc) > 3.0:
        yield candidate           # mismatch > 3 puntos ⇒ candidato

  La primera rama de signos protege contra divisiones por cero
  (``prev <= 0`` se evalúa antes que ``real``). El umbral del
  mismatch es **estrictamente** ``> 3.0``: una diferencia exacta
  de 3.0 puntos NO es candidato (paridad literal con el script).

``writes_last_attempt_at = True`` (Requirement 4.4): el ``apply``
heredado de :class:`BaseSaneamientoTask` actualiza
``last_attempt_at`` con el ``now_iso`` del clock inyectado.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from .base import BaseSaneamientoTask, CandidateRow, marketplace_clause

__all__ = ["OutboxBadDiscountsTask"]


class OutboxBadDiscountsTask(BaseSaneamientoTask):
    """Saneamiento_Task ``outbox-bad-discounts`` (paridad legacy)."""

    name = "outbox-bad-discounts"
    writes_last_attempt_at = True

    def select_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        marketplace: str,
        clock: Callable[[], datetime],
    ) -> list[CandidateRow]:
        # ``marketplace_clause`` devuelve un fragmento con placeholders
        # ``?`` y los params correspondientes. Es seguro interpolarlo
        # con f-string porque no contiene input de usuario, sólo SQL
        # hard-coded del helper.
        mkt_sql, mkt_params = marketplace_clause(marketplace)

        rows = conn.execute(
            f"""
            SELECT o.id                                                AS outbox_id,
                   LOWER(p.marketplace)                                AS marketplace,
                   json_extract(o.message_payload_json, '$.title')     AS title,
                   json_extract(o.message_payload_json, '$.current_price')   AS cur,
                   json_extract(o.message_payload_json, '$.previous_price')  AS prev,
                   json_extract(o.message_payload_json, '$.discount_percent') AS disc
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
            cur = row["cur"]
            prev = row["prev"]
            disc = row["disc"]

            # Paridad con el script: cualquier ``None`` (incluyendo
            # claves ausentes que ``json_extract`` materializa como
            # NULL) ⇒ NO candidato.
            if cur is None or prev is None or disc is None:
                continue

            outbox_id = int(row["outbox_id"])
            mkt = row["marketplace"]
            title = row["title"]

            # Primera rama: signos inválidos o ``prev <= cur``.
            # Se evalúa ANTES del cálculo de ``real`` para evitar
            # división por cero cuando ``prev <= 0``.
            if prev <= 0 or cur <= 0 or prev <= cur:
                candidates.append(
                    CandidateRow(
                        outbox_id=outbox_id,
                        marketplace=mkt,
                        title=title,
                        extra={
                            "current_price": cur,
                            "previous_price": prev,
                            "discount_percent": disc,
                            "reason": "prev<=cur",
                        },
                    )
                )
                continue

            # Segunda rama: mismatch entre el descuento declarado y el
            # descuento real implícito por (prev, cur). Estrictamente
            # ``> 3.0``: una diferencia exacta de 3.0 NO dispara.
            real = (prev - cur) / prev * 100.0
            if abs(real - disc) > 3.0:
                candidates.append(
                    CandidateRow(
                        outbox_id=outbox_id,
                        marketplace=mkt,
                        title=title,
                        extra={
                            "current_price": cur,
                            "previous_price": prev,
                            "discount_percent": disc,
                            "real": real,
                            "reason": "mismatch",
                        },
                    )
                )

        return candidates
