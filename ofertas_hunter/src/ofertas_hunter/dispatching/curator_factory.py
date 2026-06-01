"""Factory para construir un :class:`DiversityCurator` listo para inyectar.

Encapsula la lógica de wiring entre :class:`Settings`, :class:`KiroCliClient`,
:class:`DiversityScorer` y :class:`DiversityCurator` para que tanto el
``ServerContext`` (modos MCP [1]/[2]) como el ``Orchestrator`` (modo [3])
construyan exactamente el mismo objeto sin duplicar código.

Devuelve ``None`` (no curator) cuando la feature está desactivada por
configuración. El llamador debe interpretar ``None`` como "usar el
comportamiento legacy (``pick_random_eligible``)".
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Optional

from .diversity_curator import DiversityCurator
from .diversity_filters import HardCapConfig
from .diversity_scorer import DiversityScorer

if TYPE_CHECKING:
    from ..config import Settings


logger = logging.getLogger(__name__)


def build_diversity_curator(
    db: sqlite3.Connection,
    settings: "Settings",
) -> Optional[DiversityCurator]:
    """Construye un curator según los settings o devuelve ``None``.

    Reglas:
    - ``settings.diversity_curator_enabled=False`` → ``None`` (path legacy).
    - ``settings.diversity_curator_use_llm=False`` → curator sin LLM
      (sólo scorer determinístico, fallback siempre).
    - ``settings.diversity_curator_use_llm=True`` → curator con
      :class:`KiroCliClient` configurado con ``timeout_seconds`` y
      ``binary_path`` opcional. Si ``KiroCliClient`` no se puede importar
      (entorno donde kiro-cli no está disponible), degrada a curator sin
      LLM y emite warning.
    """
    if not settings.diversity_curator_enabled:
        return None

    scorer = DiversityScorer(
        history_size=settings.diversity_curator_history_size,
        top_n=settings.diversity_curator_candidate_limit,
    )

    hard_cap_config = HardCapConfig(
        enabled=settings.diversity_hard_cap_enabled,
        window_size=settings.diversity_window_size,
        max_same_category=settings.diversity_max_same_category_in_window,
        max_same_brand=settings.diversity_max_same_brand_in_window,
        max_same_product_family=settings.diversity_max_same_product_family_in_window,
        max_same_marketplace=settings.diversity_max_same_marketplace_in_window,
        fuzzy_title_threshold=settings.diversity_fuzzy_title_threshold,
        reject_similar_hours=settings.diversity_reject_similar_hours,
        allow_override_if_no_alternative=settings.diversity_allow_override_if_no_alternative,
    )

    llm_client = None
    if settings.diversity_curator_use_llm:
        try:
            from ..intelligence.kiro_cli_client import (
                KiroCliClient,
                KiroCliConfig,
            )

            llm_client = KiroCliClient(
                KiroCliConfig(
                    binary_path=settings.diversity_curator_kiro_cli_path or "",
                    timeout_seconds=settings.diversity_curator_llm_timeout_seconds,
                )
            )
            logger.info(
                "DiversityCurator: LLM activo (timeout=%.1fs, binary=%s)",
                settings.diversity_curator_llm_timeout_seconds,
                settings.diversity_curator_kiro_cli_path or "<auto>",
            )
        except Exception:
            logger.exception(
                "DiversityCurator: no se pudo inicializar KiroCliClient — "
                "degradando a curator sin LLM"
            )
            llm_client = None

    return DiversityCurator(
        db=db,
        scorer=scorer,
        llm_client=llm_client,
        history_size=settings.diversity_curator_history_size,
        candidate_limit=settings.diversity_curator_candidate_limit,
        hard_cap_config=hard_cap_config,
        trace_decisions=settings.diversity_trace_decisions,
    )


__all__ = ["build_diversity_curator"]
