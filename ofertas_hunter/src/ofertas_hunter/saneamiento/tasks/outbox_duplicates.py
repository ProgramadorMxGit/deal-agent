"""``OutboxDuplicatesTask`` — paridad con ``_vps_cleanup_duplicates.py``.

Marca como ``state='discarded'`` los outbox ``pending`` cuyo
``item_id`` o ``asin`` ya fue publicado con éxito en las últimas 48
horas (según ``published_messages``). Replica el algoritmo en dos
pasos descrito en ``.kiro/specs/bot-saneamiento-vps/design.md``
(sección ``OutboxDuplicatesTask``):

1. **Paso A**: recolectar los ``item_id``/``asin`` con
   ``published_messages.success = 1`` y ``sent_at >= now-48h``.
2. **Paso B**: por cada id no nulo, buscar outbox ``pending`` cuyo
   payload contenga ese mismo id como ``item_id`` o ``asin``,
   joineando ``offers``/``products`` para aplicar el filtro de
   marketplace.

Las filas resultantes se deduplican por ``outbox.id`` antes de
devolverse para que un outbox que matchee tanto por ``item_id`` como
por ``asin`` aparezca una sola vez.

``writes_last_attempt_at = True`` (apply heredado de
``BaseSaneamientoTask`` muta también ``last_attempt_at``).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from ..time_source import cutoff_iso
from .base import BaseSaneamientoTask, CandidateRow, marketplace_clause

__all__ = ["OutboxDuplicatesTask"]


class OutboxDuplicatesTask(BaseSaneamientoTask):
    """Saneamiento_Task ``outbox-duplicates`` (paridad legacy)."""

    name = "outbox-duplicates"
    writes_last_attempt_at = True

    def select_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        marketplace: str,
        clock: Callable[[], datetime],
    ) -> list[CandidateRow]:
        cutoff_48h = cutoff_iso(clock, 48)

        # ----- Paso A: ids publicados con éxito en las últimas 48h.
        rows_a = conn.execute(
            """
            SELECT json_extract(o.message_payload_json, '$.item_id') AS iid,
                   json_extract(o.message_payload_json, '$.asin')    AS asin
            FROM published_messages pm
            JOIN outbox o ON pm.outbox_id = o.id
            WHERE pm.success = 1
              AND pm.sent_at >= ?
            """,
            (cutoff_48h,),
        ).fetchall()

        published_ids: set[str] = set()
        for row in rows_a:
            iid = row["iid"] if isinstance(row, sqlite3.Row) else row[0]
            asin = row["asin"] if isinstance(row, sqlite3.Row) else row[1]
            if iid:
                published_ids.add(str(iid))
            if asin:
                published_ids.add(str(asin))

        if not published_ids:
            return []

        # ----- Paso B: outbox pending cuyo payload referencia esos ids.
        # ``marketplace_clause`` devuelve un fragmento con placeholders
        # ``?`` y los params. Es seguro interpolarlo con f-string porque
        # no contiene input de usuario, sólo SQL hard-coded del helper.
        mkt_sql, mkt_params = marketplace_clause(marketplace)

        candidates_by_id: dict[int, CandidateRow] = {}
        for iid in published_ids:
            rows_b = conn.execute(
                f"""
                SELECT o.id                                       AS outbox_id,
                       LOWER(p.marketplace)                       AS mkt,
                       json_extract(o.message_payload_json,
                                    '$.title')                    AS title
                FROM outbox o
                JOIN offers   of ON of.id = o.offer_id
                JOIN products p  ON p.id  = of.product_id
                WHERE o.state = 'pending'
                  AND (
                       json_extract(o.message_payload_json,
                                    '$.item_id') = ?
                    OR json_extract(o.message_payload_json,
                                    '$.asin')    = ?
                  )
                  {mkt_sql}
                """,
                (iid, iid, *mkt_params),
            ).fetchall()

            for row in rows_b:
                outbox_id = (
                    row["outbox_id"]
                    if isinstance(row, sqlite3.Row)
                    else row[0]
                )
                mkt = row["mkt"] if isinstance(row, sqlite3.Row) else row[1]
                title = row["title"] if isinstance(row, sqlite3.Row) else row[2]
                if outbox_id not in candidates_by_id:
                    candidates_by_id[outbox_id] = CandidateRow(
                        outbox_id=int(outbox_id),
                        marketplace=mkt,
                        title=title,
                        extra={"matched_id": iid},
                    )

        # Orden estable ascendente por ``outbox.id`` para tests deterministas.
        return [candidates_by_id[k] for k in sorted(candidates_by_id.keys())]
