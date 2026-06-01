"""Curador IA del outbox — scorer + hard caps + LLM + fallback + override.

Pipeline (orden):
1. Obtener candidatos elegibles por gates (el outbox ya filtró cooldown,
   scheduled_for, requires_live_validation).
2. Calcular metadata NORMALIZADA por candidato:
   category_normalized, brand_normalized, product_family, title_fingerprint.
3. Cargar historial reciente de publicaciones exitosas (normalizado).
4. Aplicar TOPES DUROS de diversidad (categoría/marca/familia/marketplace +
   fuzzy título + familia reciente 24h).
5. Si quedan candidatos → elegir por score (LLM si está activo, si no, top-1
   determinístico del DiversityScorer).
6. Si NO quedan candidatos:
   - si allow_override_if_no_alternative=True → elegir el "menos malo" y
     registrar diversity_override_no_alternative.
   - si False → NO publicar en este ciclo (None).
7. Persistir runtime_event con trazabilidad completa.

NUNCA relaja gates de seguridad. Un item que llega aquí ya es válido para
publicar; la diversidad solo decide el orden/elegibilidad temporal.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from ..models import OutboxItem
from .diversity_filters import (
    CandidateMeta,
    HardCapConfig,
    HistoryItem,
    apply_hard_caps,
    least_repetitive,
)
from .diversity_metadata import (
    infer_offer_category,
    normalize_brand,
    normalize_title,
    product_family,
    title_fingerprint,
)
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
    """Orquesta metadata + hard caps + scoring + LLM + fallback + override."""

    def __init__(
        self,
        *,
        db: sqlite3.Connection,
        scorer: DiversityScorer,
        llm_client=None,  # KiroCliClient | None
        history_size: int = 10,
        candidate_limit: int = 10,
        hard_cap_config: Optional[HardCapConfig] = None,
        trace_decisions: bool = True,
    ) -> None:
        self.db = db
        self.scorer = scorer
        self.llm_client = llm_client
        self.history_size = history_size
        self.candidate_limit = candidate_limit
        self.hard_cap_config = hard_cap_config or HardCapConfig()
        self.trace_decisions = trace_decisions

    async def pick(
        self,
        outbox: InMemoryOutbox,
        last_normal_publication_at: Optional[datetime],
        now: datetime,
    ) -> Optional[OutboxItem]:
        eligible = outbox.eligible_now(last_normal_publication_at, now)
        if not eligible:
            return None

        # 2. Metadata normalizada por candidato (enriquece el payload).
        metas = [self._build_candidate_meta(item) for item in eligible]

        # 3. Historial reciente normalizado.
        history = self._load_history()

        # 4. Topes duros de diversidad.
        cap_result = apply_hard_caps(
            metas,
            history,
            self.hard_cap_config,
            now,
        )
        kept = cap_result.kept

        # 5/6. Elegir.
        override_used = False
        override_reason: Optional[str] = None

        if kept:
            chosen_meta, llm_used, fallback_used, reason, latency_ms = await self._choose_scored(
                kept, history
            )
        else:
            # Sin candidatos tras hard caps.
            if not self.hard_cap_config.allow_override_if_no_alternative:
                self._emit_decision(
                    chosen=None,
                    candidates_count=len(metas),
                    candidates_after_hard_caps=0,
                    rejected=cap_result.rejected,
                    window_stats=cap_result.window_stats,
                    fallback_used=False,
                    llm_used=False,
                    override_used=False,
                    override_reason="no_alternative_and_override_disabled",
                    reason="no_publish_diversity_blocked",
                    latency_ms=0,
                )
                logger.info(
                    "DiversityCurator: todos los candidatos violan diversidad "
                    "y override desactivado — no se publica este ciclo"
                )
                return None
            chosen_meta = least_repetitive(metas, history, self.hard_cap_config)
            override_used = True
            override_reason = "diversity_override_no_alternative"
            llm_used = False
            fallback_used = True
            reason = override_reason
            latency_ms = 0
            logger.info(
                "DiversityCurator: override por falta de alternativa — "
                "publicando el menos repetitivo (outbox_id=%s)",
                chosen_meta.item.id if chosen_meta else None,
            )

        if chosen_meta is None:
            return None

        self._emit_decision(
            chosen=chosen_meta,
            candidates_count=len(metas),
            candidates_after_hard_caps=len(kept),
            rejected=cap_result.rejected,
            window_stats=cap_result.window_stats,
            fallback_used=fallback_used,
            llm_used=llm_used,
            override_used=override_used,
            override_reason=override_reason,
            reason=reason,
            latency_ms=latency_ms,
        )
        return chosen_meta.item

    # ------------------------------------------------------------------
    # Selección puntuada (LLM o determinística) sobre candidatos kept
    # ------------------------------------------------------------------

    async def _choose_scored(
        self,
        kept: list[CandidateMeta],
        history: list[HistoryItem],
    ) -> tuple[Optional[CandidateMeta], bool, bool, str, int]:
        """Devuelve (meta, llm_used, fallback_used, reason, latency_ms)."""
        meta_by_id = {m.item.id: m for m in kept}
        items = [m.item for m in kept]

        # Atajo: 1 candidato.
        if len(items) == 1:
            return kept[0], False, True, "single_candidate", 0

        scorer_history = self._to_scorer_history(history)
        ranked = self.scorer.rank(items, scorer_history)
        if not ranked:
            return kept[0], False, True, "no_ranked_fallback", 0

        top = ranked[: self.candidate_limit]
        fallback_item = top[0].item
        fallback_meta = meta_by_id.get(fallback_item.id, kept[0])

        # Sin LLM → top1 determinístico.
        if self.llm_client is None:
            return fallback_meta, False, True, "no_llm_client", 0

        prompt = self._build_prompt(history, top)
        t0 = time.monotonic()
        try:
            response = await self.llm_client.ask_json(prompt)
        except Exception:
            logger.exception("DiversityCurator: llm_client raised")
            response = None
        latency_ms = int((time.monotonic() - t0) * 1000)

        valid_ids = {c.item.id for c in top}
        chosen_id: Optional[int] = None
        llm_reason = ""
        if isinstance(response, dict):
            raw_id = response.get("chosen_id")
            if isinstance(raw_id, bool):
                chosen_id = None
            elif isinstance(raw_id, (int, float)):
                try:
                    chosen_id = int(raw_id)
                except (TypeError, ValueError):
                    chosen_id = None
            llm_reason = str(response.get("reason") or "")[:200]

        if chosen_id is None or chosen_id not in valid_ids:
            reason = (
                "llm_invalid_response" if response is None
                else f"llm_id_outside_topN:{chosen_id}"
            )
            return fallback_meta, True, True, reason, latency_ms

        chosen_meta = meta_by_id.get(chosen_id, fallback_meta)
        return chosen_meta, True, False, (llm_reason or "llm_chose"), latency_ms

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def _build_candidate_meta(self, item: OutboxItem) -> CandidateMeta:
        payload = dict(item.message_payload or {})
        title = payload.get("title") or ""
        marketplace = payload.get("marketplace") or "unknown"

        cat = infer_offer_category(
            title, payload, marketplace, payload.get("source")
        )
        brand_n = normalize_brand(payload.get("brand"), title)
        family = product_family(title, payload.get("brand"))
        fp = title_fingerprint(title)
        item_id = payload.get("item_id") or payload.get("asin")

        # Enriquecer el payload con metadata normalizada (sobrescribe category
        # y brand crudos por los normalizados para que el scorer y el
        # published payload los usen). Se conservan los crudos como *_raw.
        payload["category_raw"] = cat.raw
        payload["category_normalized"] = cat.normalized
        payload["category_source"] = cat.source
        payload["category_confidence"] = cat.confidence
        payload["brand_normalized"] = brand_n
        payload["product_family"] = family
        payload["title_fingerprint"] = fp
        # El scorer lee payload["category"]/["brand"]: alimentarlos con los
        # valores normalizados (sin perder el crudo, ya guardado en *_raw).
        payload["category"] = cat.normalized
        if brand_n:
            payload["brand"] = brand_n

        enriched = replace(item, message_payload=payload)
        return CandidateMeta(
            item=enriched,
            marketplace=marketplace,
            category=cat.normalized,
            brand=brand_n,
            product_family=family,
            title_fingerprint=fp,
            title=title,
            item_id=item_id,
        )

    def _load_history(self) -> list[HistoryItem]:
        """Lee últimas `history_size` publicaciones exitosas y las normaliza."""
        try:
            rows = self.db.execute(
                """
                SELECT
                    pm.sent_at,
                    o.message_payload_json
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

        history: list[HistoryItem] = []
        for r in rows:
            try:
                payload_raw = (
                    r["message_payload_json"] if hasattr(r, "keys") and "message_payload_json" in r.keys()
                    else r[1]
                )
                payload = json.loads(payload_raw)
            except Exception:
                logger.warning(
                    "DiversityCurator: skipping unparseable history row", exc_info=True
                )
                continue
            sent_at_str = r["sent_at"] if hasattr(r, "keys") and "sent_at" in r.keys() else r[0]
            try:
                sent_at = datetime.fromisoformat(str(sent_at_str).replace("Z", "+00:00"))
            except Exception:
                logger.warning(
                    "DiversityCurator: skipping history row with unparseable sent_at: %r",
                    sent_at_str,
                )
                continue

            title = payload.get("title") or ""
            cat = infer_offer_category(title, payload, payload.get("marketplace"), payload.get("source"))
            history.append(
                HistoryItem(
                    marketplace=payload.get("marketplace") or "unknown",
                    category=cat.normalized,
                    brand=normalize_brand(payload.get("brand"), title),
                    product_family=product_family(title, payload.get("brand")),
                    title_fingerprint=title_fingerprint(title),
                    item_id=payload.get("item_id") or payload.get("asin"),
                    sent_at=sent_at,
                )
            )
        return history

    def _to_scorer_history(self, history: list[HistoryItem]) -> list[HistoryEntry]:
        out: list[HistoryEntry] = []
        for h in history:
            out.append(
                HistoryEntry(
                    marketplace=h.marketplace,
                    category=h.category,
                    brand=h.brand,
                    price_bucket="mid",  # bucket no relevante para hard caps
                    sent_at=h.sent_at,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Prompt LLM
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        history: list[HistoryItem],
        top_candidates: list[ScoredCandidate],
    ) -> str:
        system = (
            'Eres el curador del grupo de WhatsApp "Ofertas Reales IA".\n'
            "Tu trabajo: elegir UN item del outbox para publicar ahora, "
            "optimizando la variedad percibida por los suscriptores.\n\n"
            "Reglas:\n"
            "- No repetir categoría que ya apareció en las últimas 3 publicaciones.\n"
            "- Variar marketplace (alternar Amazon ↔ Mercado Libre cuando sea posible).\n"
            "- Variar marcas y familias de producto.\n"
            "- Si una categoría NO ha aparecido en el historial, prefiérela.\n\n"
            "Responde EXCLUSIVAMENTE con JSON válido en este formato:\n"
            '{"chosen_id": <int>, "reason": "<una frase corta en español>"}\n\n'
            "NO incluyas markdown, ni explicaciones extra fuera del JSON.\n\n"
        )
        hist_lines = []
        for h in history:
            hist_lines.append(
                f"- {h.sent_at.strftime('%H:%M')} [{h.marketplace}/{h.category or '—'}/"
                f"{h.brand or '—'}] fam={h.product_family or '—'}"
            )
        hist_block = "\n".join(hist_lines) if hist_lines else "(sin publicaciones recientes)"

        cand_lines = []
        for c in top_candidates:
            p = c.item.message_payload or {}
            cand_lines.append(
                f"- id={c.item.id} [{p.get('marketplace')}/{p.get('category_normalized') or '—'}/"
                f"{p.get('brand_normalized') or '—'}] fam={p.get('product_family') or '—'} "
                f"precio={p.get('current_price')} disc={p.get('discount_percent')}% score={c.score:.3f}"
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

    # ------------------------------------------------------------------
    # Trazabilidad
    # ------------------------------------------------------------------

    def _emit_decision(
        self,
        *,
        chosen: Optional["CandidateMeta"],
        candidates_count: int,
        candidates_after_hard_caps: int,
        rejected: list,
        window_stats: dict,
        fallback_used: bool,
        llm_used: bool,
        override_used: bool,
        override_reason: Optional[str],
        reason: str,
        latency_ms: int,
    ) -> None:
        if not self.trace_decisions:
            return
        try:
            payload = {
                # alias retro-compatible (diagnósticos/consultas existentes)
                "chosen_id": chosen.item.id if chosen else None,
                "selected_outbox_id": chosen.item.id if chosen else None,
                "selected_title": (chosen.title[:120] if chosen else None),
                "selected_marketplace": chosen.marketplace if chosen else None,
                "selected_category_normalized": chosen.category if chosen else None,
                "selected_brand_normalized": chosen.brand if chosen else None,
                "selected_product_family": chosen.product_family if chosen else None,
                "candidates_count": candidates_count,
                "candidates_after_hard_caps": candidates_after_hard_caps,
                "rejected_candidates": [
                    {
                        "outbox_id": r.outbox_id,
                        "title": r.title,
                        "reason": r.reason,
                        "category": r.category,
                        "brand": r.brand,
                        "product_family": r.product_family,
                    }
                    for r in rejected[:25]
                ],
                "fallback_used": fallback_used,
                "llm_used": llm_used,
                "override_used": override_used,
                "override_reason": override_reason,
                "reason": reason,
                "history_size": self.history_size,
                "llm_latency_ms": latency_ms,
                "window_stats": window_stats,
            }
            now_iso = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            self.db.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (DECISION_EVENT, "info", json.dumps(payload, ensure_ascii=False), now_iso),
            )
            self.db.commit()
        except Exception:
            logger.exception("DiversityCurator: emit_decision falló")


__all__ = ["DiversityCurator", "DECISION_EVENT"]
