"""Curador IA del outbox — orquesta scorer + LLM + fallback.

Reemplaza `pick_random_eligible` con un selector inteligente:
1. Filtra elegibles (cooldown, scheduled_for) usando el outbox.
2. Score determinístico de diversidad → top N candidatos.
3. Consulta al LLM via kiro-cli con prompt corto.
4. Si el LLM responde con un id válido del top N → retorna ese item.
5. Si falla cualquier paso → fallback al top 1 del scorer.

Emite `runtime_event(kind="diversity_curator_decision")` con la metadata
de cada decisión para auditoría.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from ..models import OutboxItem
from .diversity_scorer import (
    DiversityScorer,
    HistoryEntry,
    ScoredCandidate,
    price_bucket,
)
from .outbox import InMemoryOutbox


logger = logging.getLogger(__name__)


DECISION_EVENT = "diversity_curator_decision"


class DiversityCurator:
    """Orquesta scoring + LLM + fallback. Compatible con `ItemSelector`."""

    def __init__(
        self,
        *,
        db: sqlite3.Connection,
        scorer: DiversityScorer,
        llm_client=None,  # KiroCliClient | None
        history_size: int = 10,
        candidate_limit: int = 10,
    ) -> None:
        self.db = db
        self.scorer = scorer
        self.llm_client = llm_client
        self.history_size = history_size
        self.candidate_limit = candidate_limit

    async def pick(
        self,
        outbox: InMemoryOutbox,
        last_normal_publication_at: Optional[datetime],
        now: datetime,
    ) -> Optional[OutboxItem]:
        eligible = outbox.eligible_now(last_normal_publication_at, now)
        if not eligible:
            return None
        # Atajo: 1 candidato → retornar directo (no LLM, no event)
        if len(eligible) == 1:
            return eligible[0]

        history = self._load_history()
        ranked = self.scorer.rank(eligible, history)
        if not ranked:
            return None

        top_candidates = ranked[: self.candidate_limit]
        fallback = top_candidates[0].item

        # Sin LLM → top 1
        if self.llm_client is None:
            self._emit_decision(
                chosen_id=fallback.id,
                fallback_used=True,
                reason="no_llm_client",
                candidates_count=len(top_candidates),
                latency_ms=0,
            )
            return fallback

        # Llamada al LLM (cualquier excepción → response=None)
        prompt = self._build_prompt(history, top_candidates)
        t0 = time.monotonic()
        try:
            response = await self.llm_client.ask_json(prompt)
        except Exception:
            logger.exception("DiversityCurator: llm_client raised")
            response = None
        latency_ms = int((time.monotonic() - t0) * 1000)

        valid_ids = {c.item.id for c in top_candidates}
        chosen_id: Optional[int] = None
        reason: Optional[str] = None

        if isinstance(response, dict):
            raw_id = response.get("chosen_id")
            try:
                if isinstance(raw_id, bool):
                    # bool es subclase de int — descartarlo explícitamente
                    chosen_id = None
                elif isinstance(raw_id, (int, float)):
                    chosen_id = int(raw_id)
            except (TypeError, ValueError):
                chosen_id = None
            reason = str(response.get("reason") or "")[:200]

        if chosen_id is None or chosen_id not in valid_ids:
            self._emit_decision(
                chosen_id=fallback.id,
                fallback_used=True,
                reason=(
                    "llm_invalid_response" if response is None
                    else f"llm_id_outside_topN:{chosen_id}"
                ),
                candidates_count=len(top_candidates),
                latency_ms=latency_ms,
            )
            return fallback

        # Match: el LLM eligió un id del top N
        chosen_item = next(c.item for c in top_candidates if c.item.id == chosen_id)
        self._emit_decision(
            chosen_id=chosen_id,
            fallback_used=False,
            reason=reason or "llm_chose",
            candidates_count=len(top_candidates),
            latency_ms=latency_ms,
        )
        return chosen_item

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_history(self) -> list[HistoryEntry]:
        """Lee últimas `history_size` publicaciones exitosas con metadata."""
        try:
            rows = self.db.execute(
                """
                SELECT pm.sent_at, o.message_payload_json
                FROM published_messages pm
                JOIN outbox o ON pm.outbox_id = o.id
                WHERE pm.success = 1
                ORDER BY pm.id DESC
                LIMIT ?
                """,
                (self.history_size,),
            ).fetchall()
        except Exception:
            logger.exception("DiversityCurator: _load_history query failed")
            return []

        history: list[HistoryEntry] = []
        for r in rows:
            try:
                payload_raw = (
                    r["message_payload_json"] if "message_payload_json" in r.keys()
                    else r[1]
                )
                payload = json.loads(payload_raw)
            except Exception:
                logger.warning(
                    "DiversityCurator: skipping unparseable history row",
                    exc_info=True,
                )
                continue
            sent_at_str = r["sent_at"] if "sent_at" in r.keys() else r[0]
            try:
                sent_at = datetime.fromisoformat(
                    str(sent_at_str).replace("Z", "+00:00")
                )
            except Exception:
                logger.warning(
                    "DiversityCurator: skipping history row with unparseable sent_at: %r",
                    sent_at_str,
                )
                continue
            history.append(
                HistoryEntry(
                    marketplace=payload.get("marketplace") or "unknown",
                    category=payload.get("category"),
                    brand=payload.get("brand"),
                    price_bucket=price_bucket(payload.get("current_price")),
                    sent_at=sent_at,
                )
            )
        return history

    def _build_prompt(
        self,
        history: list[HistoryEntry],
        top_candidates: list[ScoredCandidate],
    ) -> str:
        """Genera el prompt completo (system+user) para kiro-cli."""
        system = (
            'Eres el curador del grupo de WhatsApp "Ofertas Reales IA".\n'
            "Tu trabajo: elegir UN item del outbox para publicar ahora, "
            "optimizando la variedad percibida por los suscriptores.\n\n"
            "Reglas:\n"
            "- No repetir categoría que ya apareció en las últimas 3 publicaciones.\n"
            "- Variar marketplace (alternar Amazon ↔ Mercado Libre cuando sea posible).\n"
            "- Variar marcas y rangos de precio.\n"
            "- Si una categoría NO ha aparecido en el historial, prefiérela.\n\n"
            "Responde EXCLUSIVAMENTE con JSON válido en este formato:\n"
            '{"chosen_id": <int>, "reason": "<una frase corta en español>"}\n\n'
            "NO incluyas markdown, ni explicaciones extra fuera del JSON.\n\n"
        )
        hist_lines = []
        for h in history:
            hist_lines.append(
                f"- {h.sent_at.strftime('%H:%M')} [{h.marketplace}/{h.category or '—'}/"
                f"{h.brand or '—'}] bucket={h.price_bucket}"
            )
        hist_block = "\n".join(hist_lines) if hist_lines else "(sin publicaciones recientes)"

        cand_lines = []
        for c in top_candidates:
            p = c.item.message_payload or {}
            cand_lines.append(
                f"- id={c.item.id} [{p.get('marketplace')}/{p.get('category') or '—'}/"
                f"{p.get('brand') or '—'}] precio={p.get('current_price')} "
                f"disc={p.get('discount_percent')}% score={c.score:.3f}"
            )
        cand_block = "\n".join(cand_lines)

        user = (
            f"HISTORIAL (últimas {len(history)} publicaciones, más reciente arriba):\n"
            f"{hist_block}\n\n"
            f"CANDIDATOS DEL OUTBOX (ya filtrados por diversidad, top {len(top_candidates)}):\n"
            f"{cand_block}\n\n"
            "Elige el item que mejor diversifique el grupo. Responde con JSON."
        )
        return system + user

    def _emit_decision(
        self,
        *,
        chosen_id: Optional[int],
        fallback_used: bool,
        reason: str,
        candidates_count: int,
        latency_ms: int,
    ) -> None:
        """Persiste un runtime_event con la decisión. Nunca propaga errores."""
        try:
            payload = json.dumps(
                {
                    "chosen_id": chosen_id,
                    "fallback_used": fallback_used,
                    "reason": reason,
                    "candidates_count": candidates_count,
                    "history_size": self.history_size,
                    "llm_latency_ms": latency_ms,
                },
                ensure_ascii=False,
            )
            now_iso = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (DECISION_EVENT, "info", payload, now_iso),
            )
            self.db.commit()
        except Exception:
            logger.exception("DiversityCurator: emit_decision falló")


__all__ = ["DiversityCurator", "DECISION_EVENT"]
