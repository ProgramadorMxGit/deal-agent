# Diversity Curator Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reemplazar la elección aleatoria del dispatcher por un agente IA que evalúa diversidad (categoría, marca, marketplace, precio) consultando un LLM via kiro-cli, con fallback determinístico cuando el LLM falla.

**Architecture:** Insertamos un `ItemSelector` opcional en `OutboxDispatcher`. El selector (`DiversityCurator`) usa un `DiversityScorer` para precalcular top 10 candidatos diversos, luego consulta `KiroCliClient` para que el LLM elija. Si el LLM falla, retorna el top 1 del scorer. Cero impacto cuando `item_selector=None`.

**Tech Stack:** Python 3.12, asyncio, sqlite3, subprocess (para kiro-cli), pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-28-diversity-curator-agent-design.md`

---

## File Structure

| Acción | Path | Responsabilidad |
|---|---|---|
| Crear | `src/ofertas_hunter/dispatching/diversity_scorer.py` | Scoring determinístico de diversidad |
| Crear | `src/ofertas_hunter/intelligence/kiro_cli_client.py` | Wrapper async sobre subprocess kiro-cli |
| Crear | `src/ofertas_hunter/dispatching/diversity_curator.py` | Orquesta scorer + LLM + fallback |
| Crear | `src/ofertas_hunter/dispatching/curator_factory.py` | Helper para construir el curator (compartido) |
| Modificar | `src/ofertas_hunter/dispatching/dispatcher.py` | Aceptar `item_selector` opcional |
| Modificar | `src/ofertas_hunter/config.py` | Settings nuevos |
| Modificar | `src/ofertas_hunter/mcp/context.py` | Inyectar curator en `get_dispatcher()` |
| Modificar | `src/ofertas_hunter/orchestrator.py` | Inyectar curator en factory de dispatcher |
| Crear | `tests/unit/dispatching/test_diversity_scorer.py` | Tests del scorer |
| Crear | `tests/unit/intelligence/test_kiro_cli_client.py` | Tests del cliente |
| Crear | `tests/unit/dispatching/test_diversity_curator.py` | Tests del curator (A-G) |
| Crear | `tests/unit/dispatching/test_dispatcher_with_selector.py` | Integración dispatcher+selector |

---

## Task 1: DiversityScorer — fundamento

**Files:**
- Create: `src/ofertas_hunter/dispatching/diversity_scorer.py`
- Test: `tests/unit/dispatching/test_diversity_scorer.py`

- [ ] **Step 1: Write the failing test (basic ranking)**

```python
# tests/unit/dispatching/test_diversity_scorer.py
"""Tests del DiversityScorer (scoring determinístico de diversidad)."""
from datetime import datetime, timezone

import pytest

from ofertas_hunter.dispatching.diversity_scorer import (
    DiversityScorer,
    HistoryEntry,
    price_bucket,
)
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType


def _now():
    return datetime.now(timezone.utc)


def _item(
    *, id: int, marketplace: str = "amazon", category: str | None = None,
    brand: str | None = None, current_price: float = 1000.0,
    discount: float = 50.0,
) -> OutboxItem:
    return OutboxItem(
        id=id,
        offer_id=id,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": f"Item {id}",
            "marketplace": marketplace,
            "category": category,
            "brand": brand,
            "current_price": current_price,
            "discount_percent": discount,
        },
        enqueued_at=_now(),
        attempts=0,
        state=OutboxState.PENDING.value,
    )


def _hist(*, marketplace: str, category: str | None = None,
          brand: str | None = None, price_bucket: str = "mid") -> HistoryEntry:
    return HistoryEntry(
        marketplace=marketplace,
        category=category,
        brand=brand,
        price_bucket=price_bucket,
        sent_at=_now(),
    )


def test_price_bucket_categorizes_by_threshold():
    assert price_bucket(100) == "low"
    assert price_bucket(499) == "low"
    assert price_bucket(500) == "mid"
    assert price_bucket(2999) == "mid"
    assert price_bucket(3000) == "high"
    assert price_bucket(50000) == "high"


def test_scorer_returns_empty_when_no_candidates():
    scorer = DiversityScorer()
    result = scorer.rank([], history=[])
    assert result == []


def test_scorer_with_no_history_keeps_base_score():
    scorer = DiversityScorer()
    items = [_item(id=1, category="electronica"), _item(id=2, category="hogar")]
    result = scorer.rank(items, history=[])
    assert len(result) == 2
    # Sin historial, todos parten con score 1.0 (sin penalización ni bonus)
    assert all(c.score == pytest.approx(1.0) for c in result)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/test_diversity_scorer.py -v`
Expected: FAIL con "No module named 'ofertas_hunter.dispatching.diversity_scorer'"

- [ ] **Step 3: Implement DiversityScorer skeleton**

```python
# src/ofertas_hunter/dispatching/diversity_scorer.py
"""Scoring determinístico de diversidad para candidatos del outbox.

Asigna a cada candidato un score que penaliza repetición de
categoría / marca / marketplace / rango de precio respecto al historial
reciente, y bonifica categorías ausentes.

Output: lista ordenada de mayor a menor score, top N candidatos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..models import OutboxItem


PRICE_BUCKET_LOW_MAX = 500.0
PRICE_BUCKET_MID_MAX = 3000.0


def price_bucket(price: Optional[float]) -> str:
    """Clasifica un precio en low / mid / high.

    - low:  < 500 MXN
    - mid:  [500, 3000)
    - high: >= 3000
    """
    if price is None:
        return "mid"
    p = float(price)
    if p < PRICE_BUCKET_LOW_MAX:
        return "low"
    if p < PRICE_BUCKET_MID_MAX:
        return "mid"
    return "high"


@dataclass(frozen=True)
class HistoryEntry:
    marketplace: str
    category: Optional[str]
    brand: Optional[str]
    price_bucket: str
    sent_at: datetime


@dataclass
class ScoredCandidate:
    item: OutboxItem
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)


class DiversityScorer:
    """Calcula score de diversidad para candidatos.

    Reglas (multiplicadores sumativos sobre base 1.0):
    - Misma categoría aparece N veces en historial: × 0.5^N
    - Misma marca aparece N veces: × 0.7^N
    - Mismo marketplace que el último: × 0.7
    - Mismo bucket de precio que el último: × 0.85
    - Categoría AUSENTE en historial: × 1.5
    """

    def __init__(self, *, history_size: int = 10, top_n: int = 10) -> None:
        self.history_size = history_size
        self.top_n = top_n

    def rank(
        self,
        candidates: list[OutboxItem],
        history: list[HistoryEntry],
    ) -> list[ScoredCandidate]:
        if not candidates:
            return []
        scored: list[ScoredCandidate] = []
        for item in candidates:
            score, breakdown = self._score_item(item, history)
            scored.append(ScoredCandidate(item=item, score=score, breakdown=breakdown))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[: self.top_n]

    def _score_item(
        self,
        item: OutboxItem,
        history: list[HistoryEntry],
    ) -> tuple[float, dict[str, float]]:
        payload = item.message_payload or {}
        cat = payload.get("category")
        brand = payload.get("brand")
        marketplace = payload.get("marketplace") or "unknown"
        bucket = price_bucket(payload.get("current_price"))

        score = 1.0
        bd: dict[str, float] = {}

        if history:
            # Penalización por categoría
            if cat is not None:
                cat_count = sum(1 for h in history if h.category == cat)
                if cat_count > 0:
                    factor = 0.5 ** cat_count
                    score *= factor
                    bd["category_penalty"] = factor
            # Penalización por marca
            if brand is not None:
                brand_count = sum(1 for h in history if h.brand == brand)
                if brand_count > 0:
                    factor = 0.7 ** brand_count
                    score *= factor
                    bd["brand_penalty"] = factor
            # Penalización por marketplace si fue el último
            last = history[0]
            if last.marketplace == marketplace:
                score *= 0.7
                bd["marketplace_alternation"] = 0.7
            # Penalización por bucket de precio si fue el último
            if last.price_bucket == bucket:
                score *= 0.85
                bd["price_bucket_alternation"] = 0.85
            # Bonus por categoría ausente
            if cat is not None:
                seen_categories = {h.category for h in history if h.category}
                if cat not in seen_categories:
                    score *= 1.5
                    bd["category_absent_bonus"] = 1.5

        return score, bd
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/test_diversity_scorer.py -v`
Expected: PASS los 3 tests

- [ ] **Step 5: Add penalty/bonus tests**

```python
# Append to tests/unit/dispatching/test_diversity_scorer.py

def test_scorer_penalizes_repeated_category():
    """Categoría que aparece 2 veces en historial recibe ×0.25."""
    scorer = DiversityScorer()
    items = [
        _item(id=1, category="mascotas"),
        _item(id=2, category="cocina"),
    ]
    history = [
        _hist(marketplace="ml", category="mascotas"),
        _hist(marketplace="ml", category="mascotas"),
    ]
    result = scorer.rank(items, history=history)
    by_id = {c.item.id: c for c in result}
    # mascotas tuvo 2 apariciones → 0.5^2 = 0.25
    # cocina ausente del historial → ×1.5
    assert by_id[1].score < by_id[2].score
    assert "category_penalty" in by_id[1].breakdown
    assert by_id[1].breakdown["category_penalty"] == pytest.approx(0.25)


def test_scorer_bonifica_categoria_ausente():
    scorer = DiversityScorer()
    items = [
        _item(id=1, category="electronica"),
        _item(id=2, category="hogar"),
    ]
    history = [_hist(marketplace="amazon", category="electronica")]
    result = scorer.rank(items, history=history)
    by_id = {c.item.id: c for c in result}
    # hogar no está en historial → bonus ×1.5
    assert by_id[2].breakdown.get("category_absent_bonus") == pytest.approx(1.5)
    assert by_id[2].score > by_id[1].score


def test_scorer_alterna_marketplace():
    scorer = DiversityScorer()
    items = [
        _item(id=1, marketplace="amazon", category="x"),
        _item(id=2, marketplace="mercadolibre", category="x"),
    ]
    history = [_hist(marketplace="amazon", category="otro")]
    result = scorer.rank(items, history=history)
    by_id = {c.item.id: c for c in result}
    # ml no fue el último, sin penalización
    # amazon fue el último → ×0.7
    assert by_id[1].breakdown.get("marketplace_alternation") == pytest.approx(0.7)
    assert by_id[2].score > by_id[1].score


def test_scorer_top_n_limita_resultado():
    scorer = DiversityScorer(top_n=3)
    items = [_item(id=i, category=f"cat{i}") for i in range(10)]
    result = scorer.rank(items, history=[])
    assert len(result) == 3


def test_scorer_orden_estable_cuando_scores_iguales():
    """Items con mismo score mantienen orden de entrada (sort estable)."""
    scorer = DiversityScorer()
    items = [_item(id=1), _item(id=2), _item(id=3)]
    result = scorer.rank(items, history=[])
    assert [c.item.id for c in result] == [1, 2, 3]
```

- [ ] **Step 6: Run all scorer tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/test_diversity_scorer.py -v`
Expected: PASS los 7 tests

- [ ] **Step 7: Commit**

```bash
git add src/ofertas_hunter/dispatching/diversity_scorer.py tests/unit/dispatching/test_diversity_scorer.py
git commit -m "feat(dispatching): add DiversityScorer for outbox candidate ranking"
```

---

## Task 2: KiroCliClient — wrapper async de subprocess

**Files:**
- Create: `src/ofertas_hunter/intelligence/kiro_cli_client.py`
- Create: `src/ofertas_hunter/intelligence/__init__.py` (si no existe)
- Test: `tests/unit/intelligence/test_kiro_cli_client.py`
- Test: `tests/unit/intelligence/__init__.py` (si no existe)

- [ ] **Step 1: Verify intelligence package exists**

Run: `.\.venv\Scripts\python.exe -c "import ofertas_hunter.intelligence; print('ok')"`
Expected: PASS o crear `src/ofertas_hunter/intelligence/__init__.py` vacío + `tests/unit/intelligence/__init__.py` vacío.

- [ ] **Step 2: Write failing test (resolve binary path)**

```python
# tests/unit/intelligence/test_kiro_cli_client.py
"""Tests del KiroCliClient (wrapper async sobre subprocess kiro-cli)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ofertas_hunter.intelligence.kiro_cli_client import (
    KiroCliClient,
    KiroCliConfig,
    resolve_kiro_cli_path,
)


def test_resolve_uses_env_var_when_set(monkeypatch, tmp_path):
    fake = tmp_path / "kiro-cli"
    fake.write_text("")
    fake.chmod(0o755)
    monkeypatch.setenv("KIRO_CLI", str(fake))
    assert resolve_kiro_cli_path() == str(fake)


def test_resolve_falls_back_to_string_when_nothing_found(monkeypatch):
    monkeypatch.delenv("KIRO_CLI", raising=False)
    with patch("shutil.which", return_value=None), \
         patch("pathlib.Path.exists", return_value=False):
        result = resolve_kiro_cli_path()
        assert result == "kiro-cli"
```

- [ ] **Step 3: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/intelligence/test_kiro_cli_client.py -v`
Expected: FAIL "No module named ..."

- [ ] **Step 4: Implement skeleton**

```python
# src/ofertas_hunter/intelligence/kiro_cli_client.py
"""Cliente async para invocar kiro-cli via subprocess.

Diseño:
- `--classic --no-interactive` para que termine al completar la respuesta.
- Captura stdout, parsea JSON con regex tolerante.
- Timeout duro configurable (default 30s).
- Retorna `None` ante cualquier fallo (no lanza excepciones — el caller
  maneja el fallback).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


# Regex para extraer un objeto JSON del stdout (tolerante a markdown,
# texto introductorio, etc.). Captura desde la primera `{` hasta la
# última `}` balanceada del bloque más grande.
_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\"chosen_id\"[^{}]*\}", re.DOTALL)


def resolve_kiro_cli_path() -> str:
    """Determina la ruta al binario kiro-cli.

    Orden:
    1. `KIRO_CLI` env var.
    2. `~/.local/bin/kiro-cli` (Linux/Mac).
    3. `%LOCALAPPDATA%\\Kiro-Cli\\kiro-cli.exe` (Windows).
    4. `shutil.which("kiro-cli")`.
    5. Fallback a string `"kiro-cli"` (subprocess fallará limpio).
    """
    override = os.environ.get("KIRO_CLI")
    if override and Path(override).exists():
        return override

    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Kiro-Cli" / "kiro-cli.exe",
            Path.home() / "AppData" / "Local" / "Kiro-Cli" / "kiro-cli.exe",
        ]
    else:
        candidates = [
            Path.home() / ".local" / "bin" / "kiro-cli",
            Path("/usr/local/bin/kiro-cli"),
            Path("/usr/bin/kiro-cli"),
        ]
    for c in candidates:
        if c.exists():
            return str(c)
    on_path = shutil.which("kiro-cli")
    if on_path:
        return on_path
    return "kiro-cli"


@dataclass
class KiroCliConfig:
    binary_path: str = ""  # vacío → auto-detect
    classic_mode: bool = True
    timeout_seconds: float = 30.0


class KiroCliClient:
    """Cliente async sobre kiro-cli."""

    def __init__(self, config: Optional[KiroCliConfig] = None) -> None:
        self.config = config or KiroCliConfig()

    async def ask_json(
        self,
        prompt: str,
        *,
        agent: Optional[str] = None,
    ) -> Optional[dict]:
        """Invoca kiro-cli con `prompt`. Retorna dict parseado del JSON
        de stdout, o None si falla.
        """
        binary = self.config.binary_path or resolve_kiro_cli_path()
        args = [binary]
        if self.config.classic_mode:
            args.append("--classic")
        args.extend(["chat", "--no-interactive"])
        if agent:
            args.extend(["--agent", agent])
        args.extend(["--trust-all-tools", prompt])

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("kiro-cli no encontrado en %s", binary)
            return None
        except Exception:
            logger.exception("kiro-cli: error al spawnar subprocess")
            return None

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            logger.warning(
                "kiro-cli: timeout (%.1fs) — matando subprocess",
                self.config.timeout_seconds,
            )
            return None
        except Exception:
            logger.exception("kiro-cli: error en communicate")
            return None

        if proc.returncode != 0:
            stderr_text = (stderr or b"").decode("utf-8", errors="replace")[:500]
            logger.warning(
                "kiro-cli: exit code %d, stderr: %s",
                proc.returncode,
                stderr_text,
            )
            return None

        text = (stdout or b"").decode("utf-8", errors="replace")
        return _parse_json_response(text)


def _parse_json_response(text: str) -> Optional[dict]:
    """Extrae un objeto JSON del stdout.

    Estrategia:
    1. Intentar parsear el texto completo como JSON.
    2. Buscar bloque `{... "chosen_id" ...}` con regex.
    3. Si nada funciona, retornar None.
    """
    if not text:
        return None
    text_stripped = text.strip()
    # Intento 1: stdout es JSON puro
    try:
        data = json.loads(text_stripped)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    # Intento 2: extraer bloque
    match = _JSON_BLOCK_RE.search(text_stripped)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    return None
```

- [ ] **Step 5: Run resolve tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/intelligence/test_kiro_cli_client.py -v`
Expected: PASS los 2 tests de resolve

- [ ] **Step 6: Add subprocess + parsing tests**

```python
# Append to tests/unit/intelligence/test_kiro_cli_client.py


@pytest.mark.asyncio
async def test_ask_json_parses_pure_json_stdout():
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli"))

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(b'{"chosen_id": 42, "reason": "ok"}', b"")
    )

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await client.ask_json("test")
    assert result == {"chosen_id": 42, "reason": "ok"}


@pytest.mark.asyncio
async def test_ask_json_extracts_json_block_from_noisy_stdout():
    """Algunas versiones de kiro-cli devuelven texto + JSON; debe extraer."""
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli"))

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(
            b'Thinking...\nResult:\n{"chosen_id": 7, "reason": "ok"}\n\nDone.',
            b"",
        )
    )
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await client.ask_json("test")
    assert result == {"chosen_id": 7, "reason": "ok"}


@pytest.mark.asyncio
async def test_ask_json_returns_none_on_timeout():
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli", timeout_seconds=0.1))

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        result = await client.ask_json("test")
    assert result is None


@pytest.mark.asyncio
async def test_ask_json_returns_none_on_nonzero_exit():
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli"))

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.communicate = AsyncMock(return_value=(b"", b"some error"))

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await client.ask_json("test")
    assert result is None


@pytest.mark.asyncio
async def test_ask_json_returns_none_on_unparseable_stdout():
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli"))

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"hola sin json", b""))

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await client.ask_json("test")
    assert result is None


@pytest.mark.asyncio
async def test_ask_json_returns_none_when_binary_not_found():
    client = KiroCliClient(KiroCliConfig(binary_path="/no/existe/kiro-cli"))

    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
        result = await client.ask_json("test")
    assert result is None
```

- [ ] **Step 7: Run all kiro_cli tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/intelligence/test_kiro_cli_client.py -v`
Expected: PASS los 8 tests

- [ ] **Step 8: Commit**

```bash
git add src/ofertas_hunter/intelligence/kiro_cli_client.py src/ofertas_hunter/intelligence/__init__.py tests/unit/intelligence/
git commit -m "feat(intelligence): add KiroCliClient async wrapper"
```

---

## Task 3: DiversityCurator — orquesta scorer + LLM + fallback

**Files:**
- Create: `src/ofertas_hunter/dispatching/diversity_curator.py`
- Test: `tests/unit/dispatching/test_diversity_curator.py`

- [ ] **Step 1: Write failing test (sin candidatos)**

```python
# tests/unit/dispatching/test_diversity_curator.py
"""Tests del DiversityCurator — orquestación scorer + LLM + fallback.

Cubre criterios A-G del spec:
A. Solo determinístico (use_llm=false) → publica top1 score.
B. LLM elige válido → publica el id elegido.
C. LLM elige inválido (id fuera de top10) → fallback a top1.
D. LLM timeout → fallback a top1.
E. LLM no instalado → arranca y degrada a top1.
F. Sin candidatos → retorna None.
G. Un solo candidato → skip LLM, retorna directo.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from ofertas_hunter.db import init_db
from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.diversity_curator import DiversityCurator
from ofertas_hunter.dispatching.diversity_scorer import DiversityScorer
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _now():
    return datetime.now(timezone.utc)


def _make_outbox_with(items: list[OutboxItem]) -> InMemoryOutbox:
    ob = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    for it in items:
        ob.enqueue(it)
    return ob


def _item(*, id: int = 0, category: str = "cat", marketplace: str = "amazon",
          item_id: str = "ASIN1") -> OutboxItem:
    return OutboxItem(
        id=id,
        offer_id=id or 1,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": f"Item {id}",
            "category": category,
            "marketplace": marketplace,
            "item_id": item_id,
            "current_price": 1000.0,
            "discount_percent": 50.0,
            "image_url": "https://x/img",
            "url": "https://x",
        },
        enqueued_at=_now(),
        attempts=0,
        state=OutboxState.PENDING.value,
    )


# A. Sin LLM → fallback determinístico


@pytest.mark.asyncio
async def test_curator_F_returns_none_when_no_candidates(db):
    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=None,
    )
    outbox = _make_outbox_with([])
    picked = await curator.pick(outbox, last_normal_publication_at=None, now=_now())
    assert picked is None


@pytest.mark.asyncio
async def test_curator_G_single_candidate_returns_directly(db):
    """Si hay un solo candidato, no llama al LLM."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value=None)

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    only = _item(id=1)
    outbox = _make_outbox_with([only])
    picked = await curator.pick(outbox, last_normal_publication_at=None, now=_now())
    assert picked is not None
    assert picked.id == 1
    llm.ask_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_curator_A_no_llm_picks_top_score(db):
    """Sin LLM, devuelve el top1 del scorer."""
    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=None,
    )
    items = [
        _item(id=1, category="electronica"),
        _item(id=2, category="hogar"),
    ]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, last_normal_publication_at=None, now=_now())
    # Sin historial todos parten igual → orden estable: gana el primero
    assert picked is not None
    assert picked.id in (1, 2)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/test_diversity_curator.py -v`
Expected: FAIL "No module named ..."

- [ ] **Step 3: Implement DiversityCurator**

```python
# src/ofertas_hunter/dispatching/diversity_curator.py
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
        # Atajo: 1 candidato → retornar directo
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

        # Llamada al LLM
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
                if isinstance(raw_id, (int, float)):
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
        history: list[HistoryEntry] = []
        for r in rows:
            try:
                payload = json.loads(
                    r["message_payload_json"] if "message_payload_json" in r.keys()
                    else r[1]
                )
            except Exception:
                continue
            sent_at_str = r["sent_at"] if "sent_at" in r.keys() else r[0]
            try:
                sent_at = datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
            except Exception:
                sent_at = datetime.now(timezone.utc)
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
        chosen_id: int,
        fallback_used: bool,
        reason: str,
        candidates_count: int,
        latency_ms: int,
    ) -> None:
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
```

- [ ] **Step 4: Run tests for A/F/G**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/test_diversity_curator.py -v`
Expected: PASS los 3 tests

- [ ] **Step 5: Add LLM decision tests (B/C/D/E)**

```python
# Append to tests/unit/dispatching/test_diversity_curator.py


@pytest.mark.asyncio
async def test_curator_B_llm_chooses_valid_id(db):
    """LLM responde con un chosen_id que está en top10 → publica ese."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value={"chosen_id": 7, "reason": "ok"})

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [
        _item(id=5, category="a"),
        _item(id=7, category="b"),
        _item(id=9, category="c"),
    ]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id == 7
    llm.ask_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_curator_C_llm_chooses_invalid_id_falls_back(db):
    """LLM responde con id fuera del top10 → fallback al top1."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value={"chosen_id": 999, "reason": "x"})

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [_item(id=1), _item(id=2), _item(id=3)]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id in {1, 2, 3}
    # Verificar que se emitió event con fallback_used=True
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision'"
    ).fetchall()
    assert len(rows) == 1
    import json as _json
    p = _json.loads(rows[0]["payload_json"])
    assert p["fallback_used"] is True
    assert "999" in p["reason"]


@pytest.mark.asyncio
async def test_curator_D_llm_timeout_falls_back(db):
    """LLM retorna None (timeout/error) → fallback al top1."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value=None)

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [_item(id=10), _item(id=20)]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None
    assert picked.id in {10, 20}
    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision'"
    ).fetchall()
    p = pytest.importorskip("json").loads(rows[0]["payload_json"])
    assert p["fallback_used"] is True


@pytest.mark.asyncio
async def test_curator_D_llm_raises_falls_back(db):
    """Si llm_client.ask_json lanza excepción → fallback."""
    llm = AsyncMock()
    llm.ask_json = AsyncMock(side_effect=RuntimeError("boom"))

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [_item(id=1), _item(id=2)]
    outbox = _make_outbox_with(items)
    picked = await curator.pick(outbox, None, _now())
    assert picked is not None


@pytest.mark.asyncio
async def test_curator_emits_event_on_success(db):
    llm = AsyncMock()
    llm.ask_json = AsyncMock(return_value={"chosen_id": 2, "reason": "diverso"})

    curator = DiversityCurator(
        db=db, scorer=DiversityScorer(), llm_client=llm,
    )
    items = [_item(id=1), _item(id=2)]
    outbox = _make_outbox_with(items)
    await curator.pick(outbox, None, _now())

    rows = db.execute(
        "SELECT payload_json FROM runtime_events WHERE kind='diversity_curator_decision'"
    ).fetchall()
    assert len(rows) == 1
    import json as _json
    p = _json.loads(rows[0]["payload_json"])
    assert p["chosen_id"] == 2
    assert p["fallback_used"] is False
    assert p["reason"] == "diverso"
```

- [ ] **Step 6: Run all curator tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/test_diversity_curator.py -v`
Expected: PASS los 8 tests (3 base + 5 LLM)

- [ ] **Step 7: Commit**

```bash
git add src/ofertas_hunter/dispatching/diversity_curator.py tests/unit/dispatching/test_diversity_curator.py
git commit -m "feat(dispatching): add DiversityCurator orchestrating scorer + LLM + fallback"
```

---

## Task 4: Modificar OutboxDispatcher para aceptar `item_selector`

**Files:**
- Modify: `src/ofertas_hunter/dispatching/dispatcher.py`
- Test: `tests/unit/dispatching/test_dispatcher_with_selector.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/dispatching/test_dispatcher_with_selector.py
"""Tests de integración: OutboxDispatcher + ItemSelector."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from ofertas_hunter.dispatching.cooldown import CooldownPolicy
from ofertas_hunter.dispatching.dispatcher import OutboxDispatcher
from ofertas_hunter.dispatching.outbox import InMemoryOutbox, OutboxConfig
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from ofertas_hunter.publishing.whatsapp_publisher import PublishOutcome


def _now():
    return datetime.now(timezone.utc)


def _item(id: int) -> OutboxItem:
    return OutboxItem(
        id=id,
        offer_id=id,
        type=OutboxType.NORMAL.value,
        message_payload={
            "title": f"Item {id}",
            "current_price": 100.0,
            "url": f"https://x/{id}",
            "image_url": "https://x/img",
        },
        enqueued_at=_now(),
        attempts=0,
        state=OutboxState.PENDING.value,
    )


class _DummyPublisher:
    def __init__(self):
        self.calls = []

    async def publish(self, item):
        self.calls.append(item.id)
        return PublishOutcome(
            success=True, dry_run=False, formatted=None, evolution_response=None,
        )


@pytest.mark.asyncio
async def test_dispatcher_uses_item_selector_when_provided():
    outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    items = [outbox.enqueue(_item(i)) for i in (1, 2, 3)]

    publisher = _DummyPublisher()

    chosen = items[1]  # forzamos que el selector elija el id=2

    async def selector(outbox, last_pub, now):
        return chosen

    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        item_selector=selector,
    )
    await dispatcher.tick()
    assert publisher.calls == [2]


@pytest.mark.asyncio
async def test_dispatcher_falls_back_to_pick_random_when_no_selector():
    outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    outbox.enqueue(_item(1))

    publisher = _DummyPublisher()

    dispatcher = OutboxDispatcher(
        outbox=outbox,
        publisher=publisher,
        item_selector=None,
    )
    await dispatcher.tick()
    assert publisher.calls == [1]


@pytest.mark.asyncio
async def test_dispatcher_handles_selector_returning_none():
    outbox = InMemoryOutbox(OutboxConfig(cooldown=CooldownPolicy(0)))
    outbox.enqueue(_item(1))

    publisher = _DummyPublisher()

    async def selector_none(outbox, last_pub, now):
        return None

    dispatcher = OutboxDispatcher(
        outbox=outbox, publisher=publisher, item_selector=selector_none,
    )
    outcome = await dispatcher.tick()
    assert outcome is None
    assert publisher.calls == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/test_dispatcher_with_selector.py -v`
Expected: FAIL "OutboxDispatcher.__init__() got an unexpected keyword argument 'item_selector'"

- [ ] **Step 3: Add `item_selector` parameter to OutboxDispatcher**

In `src/ofertas_hunter/dispatching/dispatcher.py`, add after the existing type aliases (near `DuplicateChecker`):

```python
ItemSelector = Callable[
    [InMemoryOutbox, Optional[datetime], datetime],
    Awaitable[Optional[OutboxItem]],
]
"""Callback async para elegir el siguiente item del outbox.

Cuando se inyecta, reemplaza a `outbox.pick_random_eligible` en
`OutboxDispatcher.tick`. Retornar `None` indica que no hay nada elegible
y el dispatcher se duerme.
"""
```

In `OutboxDispatcher.__init__`, add the parameter (after `duplicate_checker`):

```python
        item_selector: Optional[ItemSelector] = None,
```

And in the body (after `self._is_duplicate = duplicate_checker`):

```python
        self._item_selector = item_selector
```

In `OutboxDispatcher.tick`, replace the `picked = self.outbox.pick_random_eligible(...)` block:

```python
            if self._item_selector is not None:
                try:
                    picked = await self._item_selector(
                        self.outbox, self._last_normal_publication_at, now
                    )
                except Exception:
                    logger.exception("item_selector raised; falling back to random")
                    picked = self.outbox.pick_random_eligible(
                        last_normal_publication_at=self._last_normal_publication_at,
                        now=now,
                    )
            else:
                picked = self.outbox.pick_random_eligible(
                    last_normal_publication_at=self._last_normal_publication_at,
                    now=now,
                )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/test_dispatcher_with_selector.py -v`
Expected: PASS los 3 tests

- [ ] **Step 5: Run all dispatcher tests to verify no regression**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/dispatching/ -v`
Expected: PASS todos (incluyendo los previos)

- [ ] **Step 6: Commit**

```bash
git add src/ofertas_hunter/dispatching/dispatcher.py tests/unit/dispatching/test_dispatcher_with_selector.py
git commit -m "feat(dispatching): support optional item_selector in OutboxDispatcher"
```

---

## Task 5: Settings + factory compartida

**Files:**
- Modify: `src/ofertas_hunter/config.py`
- Create: `src/ofertas_hunter/dispatching/curator_factory.py`

- [ ] **Step 1: Add settings**

In `src/ofertas_hunter/config.py`, near other dispatching/scheduler settings, add:

```python
    # Diversity Curator (selector inteligente del outbox)
    diversity_curator_enabled: bool = True
    diversity_curator_use_llm: bool = True
    diversity_curator_history_size: int = 10
    diversity_curator_candidate_limit: int = 10
    diversity_curator_llm_timeout_seconds: int = 30
    diversity_curator_kiro_cli_path: Optional[str] = None  # auto-detect si vacío
```

- [ ] **Step 2: Create curator factory**

```python
# src/ofertas_hunter/dispatching/curator_factory.py
"""Factory compartida para construir el DiversityCurator.

Lo usan tanto `ServerContext.get_dispatcher` (modos [1]/[2]) como
`Orchestrator._build_dispatcher_factory` (modo [3]) para no duplicar la
lógica de construcción.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from ..intelligence.kiro_cli_client import KiroCliClient, KiroCliConfig
from .diversity_curator import DiversityCurator
from .diversity_scorer import DiversityScorer


logger = logging.getLogger(__name__)


def build_diversity_curator(
    *,
    db: sqlite3.Connection,
    settings: Any,
) -> Optional[DiversityCurator]:
    """Construye un `DiversityCurator` según los settings.

    Retorna `None` si `diversity_curator_enabled=False`.
    """
    if not getattr(settings, "diversity_curator_enabled", False):
        return None

    history_size = int(getattr(settings, "diversity_curator_history_size", 10))
    candidate_limit = int(getattr(settings, "diversity_curator_candidate_limit", 10))

    scorer = DiversityScorer(history_size=history_size, top_n=candidate_limit)

    llm_client: Optional[KiroCliClient] = None
    if getattr(settings, "diversity_curator_use_llm", True):
        binary = getattr(settings, "diversity_curator_kiro_cli_path", None) or ""
        timeout = float(getattr(settings, "diversity_curator_llm_timeout_seconds", 30))
        llm_client = KiroCliClient(
            config=KiroCliConfig(
                binary_path=binary,
                classic_mode=True,
                timeout_seconds=timeout,
            )
        )

    curator = DiversityCurator(
        db=db,
        scorer=scorer,
        llm_client=llm_client,
        history_size=history_size,
        candidate_limit=candidate_limit,
    )
    logger.info(
        "DiversityCurator activo (use_llm=%s, history=%d, top=%d)",
        bool(llm_client),
        history_size,
        candidate_limit,
    )
    return curator
```

- [ ] **Step 3: Verify imports work**

Run: `.\.venv\Scripts\python.exe -c "from ofertas_hunter.dispatching.curator_factory import build_diversity_curator; print('ok')"`
Expected: PASS

- [ ] **Step 4: Run all tests for regression**

Run: `.\.venv\Scripts\python.exe -m pytest --no-header -q`
Expected: PASS todos los previos (no se rompió nada)

- [ ] **Step 5: Commit**

```bash
git add src/ofertas_hunter/config.py src/ofertas_hunter/dispatching/curator_factory.py
git commit -m "feat(config): add diversity_curator settings and shared factory"
```

---

## Task 6: Integración en ServerContext.get_dispatcher (modos [1]/[2])

**Files:**
- Modify: `src/ofertas_hunter/mcp/context.py`

- [ ] **Step 1: Modify `get_dispatcher`**

In `src/ofertas_hunter/mcp/context.py`, locate `get_dispatcher` method. Add the curator import and wiring:

After the existing imports inside `get_dispatcher`:

```python
        from ..dispatching.curator_factory import build_diversity_curator
```

Build the curator BEFORE the `OutboxDispatcher(...)` call:

```python
        curator = build_diversity_curator(db=self.db, settings=self.settings)
        item_selector = curator.pick if curator else None
```

Add `item_selector=item_selector` to the `OutboxDispatcher(...)` constructor call.

- [ ] **Step 2: Verify no regression in mcp tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/mcp/ -v`
Expected: PASS todos

- [ ] **Step 3: Commit**

```bash
git add src/ofertas_hunter/mcp/context.py
git commit -m "feat(mcp): wire DiversityCurator into ServerContext dispatcher"
```

---

## Task 7: Integración en Orchestrator (modo [3])

**Files:**
- Modify: `src/ofertas_hunter/orchestrator.py`

- [ ] **Step 1: Modify `_build_dispatcher_factory`**

In `src/ofertas_hunter/orchestrator.py`, locate the `_build_dispatcher_factory` method (or wherever the `OutboxDispatcher` is constructed for mode 3). Add the import inside the method:

```python
        from .dispatching.curator_factory import build_diversity_curator
```

Build the curator and pass to the dispatcher:

```python
            curator = build_diversity_curator(db=self.db, settings=s)
            dispatcher = OutboxDispatcher(
                ...,
                item_selector=curator.pick if curator else None,
                ...,
            )
```

(Mantener todos los argumentos existentes; sólo añadir `item_selector`.)

- [ ] **Step 2: Verify no regression in orchestrator-related tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_orchestrator.py -v`
Expected: PASS todos

- [ ] **Step 3: Commit**

```bash
git add src/ofertas_hunter/orchestrator.py
git commit -m "feat(orchestrator): wire DiversityCurator into mode 3 dispatcher"
```

---

## Task 8: Verificación final

- [ ] **Step 1: Run full test suite**

Run: `.\.venv\Scripts\python.exe -m pytest --no-header -q`
Expected: PASS todos (684 + 4 nuevos tests = ~720+, sin regresiones)

- [ ] **Step 2: Verify imports from `__main__`**

Run: `.\.venv\Scripts\python.exe -c "from ofertas_hunter.dispatching.curator_factory import build_diversity_curator; from ofertas_hunter.config import get_settings; c = build_diversity_curator(db=None, settings=get_settings()); print('built:', c is not None)"`
Expected: print con `built: True` (o False si `DIVERSITY_CURATOR_ENABLED=false` en `.env`)

- [ ] **Step 3: Sync to VPS**

Run: `.\scripts\vps_sync.ps1`
Expected: scp de los archivos nuevos/modificados.

- [ ] **Step 4: Verify on VPS**

Run: `ssh -i "$env:USERPROFILE\.ssh\gcp_ofertas_bot" agaetranahoy@34.59.242.95 "cd /opt/deal-agent/ofertas_hunter && .venv/bin/python -m pytest tests/unit/dispatching/test_diversity_scorer.py tests/unit/dispatching/test_diversity_curator.py tests/unit/intelligence/test_kiro_cli_client.py tests/unit/dispatching/test_dispatcher_with_selector.py -v 2>&1 | tail -20"`
Expected: PASS los ~24 tests del feature en el VPS

- [ ] **Step 5: Update changes.md**

Append to `changes.md` a la sección del top:

```markdown
## 2026-05-28 — Diversity Curator Agent

Reemplaza la elección aleatoria del dispatcher por un agente IA que
optimiza la variedad de productos publicados al grupo de WhatsApp.

- Nuevo `DiversityScorer` (categoría/marca/marketplace/precio penalties).
- Nuevo `KiroCliClient` (subprocess async, --classic --no-interactive).
- Nuevo `DiversityCurator` (orquesta scorer + LLM + fallback).
- `OutboxDispatcher` acepta `item_selector` opcional (zero-regression).
- Integrado en modos [1]/[2] (ServerContext) y [3] (Orchestrator).
- Settings: `DIVERSITY_CURATOR_ENABLED`, `DIVERSITY_CURATOR_USE_LLM`, etc.
- 24 tests nuevos cubriendo criterios A-G del spec.

Spec: `docs/superpowers/specs/2026-05-28-diversity-curator-agent-design.md`
Plan: `docs/superpowers/plans/2026-05-28-diversity-curator-agent.md`
```

- [ ] **Step 6: Final commit**

```bash
git add changes.md
git commit -m "docs: log diversity curator agent feature in changes.md"
```

---

## Self-Review Checklist (post-plan)

✅ **Spec coverage:**
- §4 Arquitectura → Tasks 3, 4
- §5.1 DiversityScorer → Task 1
- §5.2 KiroCliClient → Task 2
- §5.3 DiversityCurator → Task 3
- §5.4 OutboxDispatcher cambios → Task 4
- §6 Prompt template → Task 3 step 3 (`_build_prompt`)
- §7 Persistencia (`runtime_events`) → Task 3 step 3 (`_emit_decision`)
- §8 Configuración → Task 5
- §9 Integración → Tasks 6, 7
- §10 Testing tabla → Tasks 1, 2, 3, 4
- §11 Manejo errores → cubierto en Task 3 (fallbacks) y Task 4 (selector raises)
- Acceptance A-G → Task 3 tests

✅ **Placeholders:** ningún TBD/TODO. Code blocks completos.

✅ **Type consistency:** `ItemSelector` definido en Task 4 = signature usada en Task 3 (`pick(outbox, last_pub, now) -> Optional[OutboxItem]`). `KiroCliClient.ask_json` mismo signature en Tasks 2 y 3.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-28-diversity-curator-agent.md`.**

**Two execution options:**

1. **Subagent-Driven (recommended)** — Yo despacho un sub-agente fresco por cada task, reviso entre tasks, iteración rápida.
2. **Inline Execution** — Ejecuto todas las tasks en esta sesión con checkpoints para revisar.

¿Cuál prefieres?
