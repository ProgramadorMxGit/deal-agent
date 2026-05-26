"""DomHealer: intenta recuperar la extracción cuando los selectores fallan.

Flujo:

```
heal(marketplace, context, html, url, fixture_dir, db) -> HealOutcome
```

1. Guarda el HTML en `dom_snapshots`.
2. Pide a `parser.parse(html, url)` el resultado actual. Si ya es publicable,
   no hace nada.
3. Aplica heurísticas (`HeuristicSelectorRecovery`) que producen un dict
   alternativo de campos extraídos. Las heurísticas son específicas por
   marketplace.
4. Persiste un fixture con el HTML "saneado" (mismo contenido, sólo lo
   movemos a `data/heal_samples/...`).
5. Llama al `tester` (callable inyectable) que valida el fixture contra el
   parser. Si pasa, registra `selector_versions(test_pass=1)`. Si falla,
   registra `test_pass=0` y descarta el patch (no aplica nada).
6. Devuelve `HealOutcome(success, reasons, snapshot_id, version_id)`.

Si `LLM_HEAL_ENABLED=true`, se podría enchufar un fallback IA aquí. En esta
fase queda como hook (`llm_strategy`) pero por defecto es `None`.

Es **determinista** y testeable: la inyección de `tester` permite que los
tests verifiquen cada rama (heurística éxito, heurística falla, tester
falla, etc.).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .selector_versioner import SelectorVersioner


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass
class HealOutcome:
    success: bool
    reasons: list[str] = field(default_factory=list)
    snapshot_id: Optional[int] = None
    fixture_path: Optional[Path] = None
    version_id: Optional[int] = None
    recovered_fields: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Heurísticas: recuperación de campos sin selectores
# ---------------------------------------------------------------------------


_PRICE_NUMBER_RE = re.compile(r"\$\s*([0-9][0-9.,]+)")


class HeuristicSelectorRecovery:
    """Heurísticas neutrales que extraen campos del HTML sin selectores rígidos.

    Estas heurísticas son **independientes** del parser específico de cada
    marketplace; sirven como fallback cuando el parser oficial falla.
    """

    def recover(self, html: str) -> dict:
        if not html:
            return {}
        recovered: dict = {}

        # JSON-LD price
        ld_price = self._jsonld_price(html)
        if ld_price is not None:
            recovered["current_price"] = ld_price

        # OG image
        og_image = self._og_image(html)
        if og_image:
            recovered["image_url"] = og_image

        # OG title (cuando el h1 está roto)
        og_title = self._og_title(html)
        if og_title:
            recovered["title"] = og_title

        # Regex sobre cualquier "$1,299.00" visible (último recurso)
        match = _PRICE_NUMBER_RE.search(html)
        if "current_price" not in recovered and match:
            try:
                cleaned = match.group(1).replace(",", "")
                recovered["current_price_regex"] = float(cleaned)
            except ValueError:
                pass

        return recovered

    # Helpers
    def _jsonld_price(self, html: str) -> Optional[float]:
        for chunk in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            try:
                data = json.loads(chunk)
            except (json.JSONDecodeError, TypeError):
                continue
            for entry in _iter_ld_entries(data):
                offers = entry.get("offers")
                if not offers:
                    continue
                items = offers if isinstance(offers, list) else [offers]
                for offer in items:
                    if not isinstance(offer, dict):
                        continue
                    price = offer.get("price")
                    if price is not None:
                        try:
                            return float(price)
                        except (TypeError, ValueError):
                            continue
        return None

    def _og_image(self, html: str) -> Optional[str]:
        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
        return None

    def _og_title(self, html: str) -> Optional[str]:
        match = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
        return None


def _iter_ld_entries(data):
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_entries(item)
    elif isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            yield from _iter_ld_entries(data["@graph"])
        else:
            yield data


# ---------------------------------------------------------------------------
# DomHealer
# ---------------------------------------------------------------------------


# `tester(fixture_path) -> True/False`. Devuelve True si los tests pasan.
TesterCallable = Callable[[Path], bool]


class DomHealer:
    """DomHealer base. No depende del parser específico."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fixture_root: Path,
        tester: TesterCallable,
        recovery: Optional[HeuristicSelectorRecovery] = None,
    ) -> None:
        self.db = conn
        self.fixture_root = Path(fixture_root)
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.tester = tester
        self.recovery = recovery or HeuristicSelectorRecovery()
        self.versioner = SelectorVersioner(conn)

    # ------------------------------------------------------------------
    # API principal
    # ------------------------------------------------------------------

    def heal(
        self,
        *,
        marketplace: str,
        context: str,
        url: str,
        html: str,
        reason: str = "extraction_failed",
        applied_by: str = "heuristic",
    ) -> HealOutcome:
        outcome = HealOutcome(success=False)

        snapshot_id = self._save_snapshot(marketplace, context, url, html, reason)
        outcome.snapshot_id = snapshot_id

        recovered = self.recovery.recover(html)
        outcome.recovered_fields = recovered
        if not recovered:
            outcome.reasons.append("no_recovered_fields")
            return outcome

        # Persistir fixture local (HTML completo) para que tests futuros
        # puedan correrse offline contra él.
        fixture_path = self._persist_fixture(marketplace, context, html)
        outcome.fixture_path = fixture_path

        # Correr tests del fixture: si pasan → patch aplicado.
        try:
            tests_ok = bool(self.tester(fixture_path))
        except Exception as exc:
            logger.warning("dom_healer tester raised: %s", exc)
            outcome.reasons.append(f"tester_error: {exc}")
            tests_ok = False

        # Registrar `selector_version` con el resultado.
        version_id = self.versioner.record(
            marketplace=marketplace,
            context=context,
            key="recovered_fields",
            selector_value=json.dumps(
                {k: v for k, v in recovered.items() if isinstance(v, (str, int, float, bool))},
                ensure_ascii=False,
            )[:500],
            applied_by=applied_by,
            test_pass=tests_ok,
            fixture_path=str(fixture_path),
        )
        outcome.version_id = version_id

        if not tests_ok:
            self.versioner.revert(version_id, reason="tests_failed")
            outcome.reasons.append("tests_failed")
            return outcome

        outcome.success = True
        outcome.reasons.append("recovered_via_heuristics")
        return outcome

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------

    def _save_snapshot(
        self, marketplace: str, context: str, url: str, html: str, reason: str
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO dom_snapshots (marketplace, context, url, content, captured_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (marketplace, context, url, html or "", _now_iso(), reason),
        )
        return cur.lastrowid

    def _persist_fixture(
        self, marketplace: str, context: str, html: str
    ) -> Path:
        directory = self.fixture_root / marketplace / context
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"healed_{int(datetime.now(timezone.utc).timestamp())}.html"
        target.write_text(html or "", encoding="utf-8")
        return target


__all__ = [
    "DomHealer",
    "HealOutcome",
    "HeuristicSelectorRecovery",
    "TesterCallable",
]
