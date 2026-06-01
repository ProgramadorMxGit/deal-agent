# Diversity Curator Agent — Design Spec

**Fecha:** 2026-05-28
**Autor:** brainstorming session
**Estado:** approved → pendiente de plan de implementación

---

## 1. Problema

El dispatcher actual (`OutboxDispatcher`) elige qué oferta publicar via
`outbox.pick_random_eligible()`: aleatorio entre todos los items
elegibles. Esto produce **ráfagas de productos similares** en el grupo
de WhatsApp:

- 5 carriolas seguidas de la misma marca.
- 3 laptops HP en 20 minutos.
- 4 mochilas transportadoras de mascota en una hora.

El usuario quiere un **curador inteligente** que decida qué publicar
con base en **diversidad** (categoría, marca, marketplace, rango de
precio), no aleatoriamente.

## 2. Objetivo

Reemplazar la elección aleatoria por una decisión razonada que evalúe:

- Qué categorías ya se publicaron en las últimas N publicaciones.
- Qué marcas ya se publicaron.
- Qué marketplaces ya se usaron.
- Qué rangos de precio ya aparecieron.

Y elija el item del outbox que **maximice la variedad percibida** por
los suscriptores del grupo.

## 3. Decisiones aprobadas (brainstorming)

| Decisión | Elegido | Alternativas descartadas |
|---|---|---|
| Lógica de decisión | **B** — LLM (kiro-cli) decide | A: determinístico puro / C: híbrido |
| Cliente LLM | **B** — kiro-cli `--classic --no-interactive` via subprocess | A: Anthropic API directa / C: ambos con fallback |
| Alcance del prompt | **A** — Top 10 candidatos pre-filtrados + historial 10 publicados | B: outbox completo / C: resumen por categoría |
| Fallback si LLM falla | **A** — scoring determinístico (top 1) | B: aleatorio / C: no publicar |

## 4. Arquitectura

```
[OutboxDispatcher.tick()]
       │
       ▼
[ItemSelector.pick(outbox, last_normal_publication_at, now)]
       │
       └─► [DiversityCurator]
              │
              ├─ 1. outbox.eligible_now(...) → todos los elegibles
              ├─ 2. DiversityScorer.rank(candidates, history) → top 10
              ├─ 3. db.recent_published(limit=10) → historial
              ├─ 4. KiroCliClient.ask_choice(prompt) → chosen_id | None
              │      ├─ ok + chosen_id ∈ top10 → retornar ese item
              │      └─ timeout/error/inválido → retornar top1 (fallback)
              └─ 5. emit runtime_event("diversity_curator_decision")
       │
       ▼
[Publisher.publish(item)]
```

El dispatcher actual cambia mínimamente: se le inyecta un
`item_selector` opcional. Si no se inyecta, mantiene el comportamiento
de `pick_random_eligible` (zero-regression).

## 5. Componentes nuevos

### 5.1 `DiversityScorer`

**Path:** `src/ofertas_hunter/dispatching/diversity_scorer.py`

**API:**

```python
@dataclass
class HistoryEntry:
    marketplace: str
    category: Optional[str]
    brand: Optional[str]
    price_bucket: str   # "low" / "mid" / "high"
    sent_at: datetime

@dataclass
class ScoredCandidate:
    item: OutboxItem
    score: float
    breakdown: dict[str, float]  # razones del score (audit)

class DiversityScorer:
    def __init__(self, *, history_size: int = 10, top_n: int = 10) -> None: ...

    def rank(
        self,
        candidates: list[OutboxItem],
        history: list[HistoryEntry],
    ) -> list[ScoredCandidate]: ...
```

**Reglas de scoring (sumativo, base 1.0):**

| Señal | Multiplicador | Justificación |
|---|---|---|
| Misma categoría aparece N veces en últimas 10 | × `0.5^N` | Penaliza fuerte repetición de categoría |
| Misma marca aparece N veces | × `0.7^N` | Penaliza menos marca (es señal débil) |
| Mismo marketplace fue el último | × 0.7 | Alterna marketplaces |
| Mismo bucket de precio fue el último | × 0.85 | Variar precios |
| Categoría AUSENTE en historial | × 1.5 | Boost a categorías nuevas |
| `is_publishable=true` | × 1.0 | Pre-condición, no afecta |

**Buckets de precio:**
- `low`: < $500 MXN
- `mid`: $500–$3,000
- `high`: > $3,000

**Output:** `top_n` candidatos ordenados de mayor a menor `score`.

### 5.2 `KiroCliClient`

**Path:** `src/ofertas_hunter/intelligence/kiro_cli_client.py`

**API:**

```python
@dataclass
class KiroCliConfig:
    binary_path: str = "kiro-cli"  # auto-detect si está vacío
    classic_mode: bool = True
    timeout_seconds: float = 30.0

class KiroCliClient:
    async def ask_json(
        self,
        prompt: str,
        *,
        agent: Optional[str] = None,
    ) -> Optional[dict]: ...
```

**Implementación:**

- `asyncio.create_subprocess_exec(binary, "--classic", "chat", ...)`.
- `--no-interactive` para que termine al recibir respuesta.
- Captura stdout/stderr con `asyncio.wait_for(timeout)`.
- Stdout esperado: bloque JSON entre la salida (extraer con regex
  `\{[^{}]*"chosen_id"[^{}]*\}` o parser tolerante).
- Si stdout no contiene JSON parseable → retorna `None`.
- Si timeout → retorna `None`.
- Si exit code ≠ 0 → retorna `None`, log stderr para debug.

**Auto-detección del binario:**

1. `os.environ["KIRO_CLI"]` si está set.
2. `~/.local/bin/kiro-cli` (Linux/Mac).
3. `%LOCALAPPDATA%\Kiro-Cli\kiro-cli.exe` (Windows).
4. `shutil.which("kiro-cli")`.
5. Fallback al string `"kiro-cli"` (subprocess fallará limpio si no existe).

### 5.3 `DiversityCurator`

**Path:** `src/ofertas_hunter/dispatching/diversity_curator.py`

**API (implementa el protocolo `ItemSelector`):**

```python
class ItemSelector(Protocol):
    async def pick(
        self,
        outbox: InMemoryOutbox,
        last_normal_publication_at: Optional[datetime],
        now: datetime,
    ) -> Optional[OutboxItem]: ...

class DiversityCurator:
    def __init__(
        self,
        *,
        db: sqlite3.Connection,
        scorer: DiversityScorer,
        llm_client: Optional[KiroCliClient] = None,
        history_size: int = 10,
        candidate_limit: int = 10,
    ) -> None: ...

    async def pick(self, outbox, last_normal_publication_at, now) -> Optional[OutboxItem]: ...
```

**Flujo de `pick`:**

1. `eligible = outbox.eligible_now(last_normal_publication_at, now)`.
2. Si `len(eligible) == 0` → retornar `None`.
3. Si `len(eligible) == 1` → retornar ese item directamente (no vale la
   pena llamar al LLM).
4. `history = self._load_history()` — lee últimas `history_size`
   `published_messages` con success=1, JOIN outbox → extrae
   marketplace/category/brand/price_bucket.
5. `top_candidates = self.scorer.rank(eligible, history)[:candidate_limit]`.
6. Si `llm_client is None` o feature flag off → retornar `top_candidates[0].item`.
7. Construir `prompt` (ver §6) con history + top_candidates.
8. `result = await self.llm_client.ask_json(prompt)` con timeout
   global de 35s (margen sobre el 30s del cliente).
9. Validar respuesta:
   - `result["chosen_id"]` debe ser `int`.
   - El id debe estar en `[c.item.id for c in top_candidates]`.
   - Si falla validación → emit warning, fallback a `top_candidates[0].item`.
10. Emit runtime_event con la decisión (chosen_id, reason, fallback_used).
11. Retornar el item elegido.

### 5.4 Cambios en `OutboxDispatcher`

**Path:** `src/ofertas_hunter/dispatching/dispatcher.py`

**Adiciones mínimas:**

```python
ItemSelector = Callable[
    [InMemoryOutbox, Optional[datetime], datetime],
    Awaitable[Optional[OutboxItem]],
]

class OutboxDispatcher:
    def __init__(
        self,
        ...,
        item_selector: Optional[ItemSelector] = None,
        ...,
    ):
        ...
        self._item_selector = item_selector
```

En `tick()` cambia sólo el `pick_random_eligible`:

```python
if self._item_selector is not None:
    picked = await self._item_selector(self.outbox, self._last_normal_publication_at, now)
else:
    picked = self.outbox.pick_random_eligible(...)  # comportamiento actual
```

Cero impacto si `item_selector=None`.

## 6. Prompt template

**Sistema (constante):**

```
Eres el curador del grupo de WhatsApp "Ofertas Reales IA".
Tu trabajo: elegir UN item del outbox para publicar ahora, optimizando
la variedad percibida por los suscriptores.

Reglas:
- No repetir categoría que ya apareció en las últimas 3 publicaciones.
- Variar marketplace (alternar Amazon ↔ Mercado Libre cuando sea posible).
- Variar marcas y rangos de precio.
- Si una categoría NO ha aparecido en el historial, prefiérela.

Responde EXCLUSIVAMENTE con JSON válido en este formato:
{"chosen_id": <int>, "reason": "<una frase corta en español>"}

NO incluyas markdown, ni explicaciones extra fuera del JSON.
```

**Usuario (dinámico, generado en runtime):**

```
HISTORIAL (últimas 10 publicaciones, más reciente arriba):
- 12:30 [mercadolibre/electrónica/HP] Laptop HP 14" $4,762 (60%)
- 12:25 [mercadolibre/mascotas/—] Carriola gato $643 (50%)
- 12:20 [amazon/electrónica/Logitech] Webcam C925 $1,696 (51%)
- 12:15 [mercadolibre/hogar/—] Cortinas blackout $298 (61%)
- 12:10 [amazon/cocina/Midea] Microondas $2,198 (75%)
[...]

CANDIDATOS DEL OUTBOX (ya filtrados por diversidad, top 10 más diversos):
1. id=1320 [amazon/cocina/MasterChef] Freidora aire 5.5L $939 (67%)
2. id=1311 [mercadolibre/mascotas/—] Bolsa transportadora $250 (50%)
3. id=1325 [mercadolibre/hogar/—] Edredón king $1,499 (52%)
4. id=1330 [amazon/electrónica/TP-Link] Router mesh $1,599 (52%)
5. id=1335 [amazon/jardín/—] Manguera 30m $450 (60%)
[...]

Elige el item que mejor diversifique el grupo. Responde con JSON.
```

**Tamaño esperado:** ~1KB prompt sistema + ~1KB usuario = ~2KB total.

## 7. Persistencia

**No requiere migrations nuevas.** Toda la información necesaria está
en tablas existentes:

- `published_messages` (sent_at, outbox_id, success).
- `outbox.message_payload_json` (marketplace, item_id, asin).
- `products` (category, brand) — JOIN via offer_id → product_id.

**Tabla de auditoría:** se reusa `runtime_events` con kind nuevo
`diversity_curator_decision`:

```json
{
  "chosen_id": 1320,
  "fallback_used": false,
  "candidates_count": 10,
  "history_size": 10,
  "reason": "Cocina ausente en historial; alterna marketplace",
  "llm_latency_ms": 1247,
  "outbox_size_total": 287
}
```

## 8. Configuración (`config.py`)

```python
# Diversity Curator
diversity_curator_enabled: bool = True
diversity_curator_use_llm: bool = True
diversity_curator_history_size: int = 10
diversity_curator_candidate_limit: int = 10
diversity_curator_llm_timeout_seconds: int = 30
diversity_curator_kiro_cli_path: Optional[str] = None  # auto-detect
```

Variables `.env` correspondientes:
- `DIVERSITY_CURATOR_ENABLED=true`
- `DIVERSITY_CURATOR_USE_LLM=true`
- etc.

## 9. Integración

### 9.1 `ServerContext.get_dispatcher()`

```python
from ..dispatching.diversity_curator import DiversityCurator
from ..dispatching.diversity_scorer import DiversityScorer
from ..intelligence.kiro_cli_client import KiroCliClient, KiroCliConfig

# Construir curator si está enabled
curator = None
if self.settings.diversity_curator_enabled:
    llm_client = None
    if self.settings.diversity_curator_use_llm:
        llm_client = KiroCliClient(
            config=KiroCliConfig(
                binary_path=self.settings.diversity_curator_kiro_cli_path or "",
                timeout_seconds=self.settings.diversity_curator_llm_timeout_seconds,
            )
        )
    curator = DiversityCurator(
        db=self.db,
        scorer=DiversityScorer(
            history_size=self.settings.diversity_curator_history_size,
            top_n=self.settings.diversity_curator_candidate_limit,
        ),
        llm_client=llm_client,
        history_size=self.settings.diversity_curator_history_size,
        candidate_limit=self.settings.diversity_curator_candidate_limit,
    )

dispatcher = OutboxDispatcher(
    ...,
    item_selector=curator.pick if curator else None,
)
```

### 9.2 `Orchestrator._build_dispatcher_factory()` (modo [3])

Misma lógica que `ServerContext.get_dispatcher()`. Refactor: extraer
el bloque de construcción del curator a una helper compartida en
`src/ofertas_hunter/dispatching/curator_factory.py` para evitar
duplicación.

## 10. Testing

| Test | Path | Cobertura |
|---|---|---|
| `test_diversity_scorer.py` | `tests/unit/dispatching/` | Penalizaciones por categoría/marca/marketplace, bonus de categoría ausente, ordenamiento estable, top_n |
| `test_kiro_cli_client.py` | `tests/unit/intelligence/` | Subprocess mock, timeout, parseo JSON, manejo stderr, exit code != 0 |
| `test_diversity_curator.py` | `tests/unit/dispatching/` | Flujo happy path, fallback timeout, fallback chosen_id inválido, fallback JSON inválido, fallback sin LLM client, sin candidatos, single candidate |
| `test_dispatcher_with_selector.py` | `tests/unit/dispatching/` | Dispatcher integra item_selector, comportamiento idéntico cuando item_selector=None |

**Tests obligatorios (acceptance):**

A. **Solo determinístico**: con `use_llm=false`, dispatcher publica
   item con mayor score de diversidad.
B. **LLM elige válido**: respuesta `{"chosen_id": X}` donde X está en
   top10, dispatcher publica X.
C. **LLM elige inválido (id fuera de top10)**: dispatcher cae a top1
   determinístico, emite warning event.
D. **LLM timeout**: dispatcher cae a top1, emite warning event.
E. **LLM no instalado**: bot arranca sin error, item_selector usa
   solo scoring determinístico.
F. **Sin candidatos**: retorna None, dispatcher idle como hoy.
G. **Un solo candidato**: skip LLM, retorna directo.

## 11. Manejo de errores

| Escenario | Comportamiento |
|---|---|
| `kiro-cli` no encontrado al arranque | Log error, instanciar `DiversityCurator` con `llm_client=None` (degrada a determinístico) |
| `kiro-cli` timeout (>30s) | `KiroCliClient.ask_json` retorna `None`, curator usa top1 fallback |
| `kiro-cli` exit code ≠ 0 | log stderr, retorna `None`, fallback |
| Stdout sin JSON parseable | retorna `None`, fallback |
| `chosen_id` no es int | retorna `None`, fallback |
| `chosen_id` fuera de top10 | log warning, fallback |
| Excepción en scorer | log exception, dispatcher cae a `pick_random_eligible` (regresión segura) |
| DB locked al leer history | retornar `None` history, scorer usa `[]` (todo es nuevo) |

Cada error emite `runtime_event` para observabilidad.

## 12. Lo que NO hace (YAGNI explícito)

- **No** re-prioriza errores de precio (siguen con prioridad absoluta
  como hoy en `pick_random_eligible`).
- **No** aprende a largo plazo (cada decisión es stateless con ventana
  de 10 publicaciones).
- **No** usa Anthropic API directa (kiro-cli como pediste).
- **No** incluye dashboard/UI para visualizar razones del LLM (sólo
  runtime_events queryeables con SQLite).
- **No** modifica el outbox ni el publisher.
- **No** afecta hunters, discovery, ML session recovery, ni nada
  upstream.

## 13. Compatibilidad

- Si `DIVERSITY_CURATOR_ENABLED=false` → comportamiento idéntico al
  actual (zero regression).
- Si `DIVERSITY_CURATOR_USE_LLM=false` pero `_ENABLED=true` → solo
  scorer determinístico, sigue diversificando sin gastar tokens.
- Si kiro-cli falla al arranque del bot → degrada automáticamente a
  determinístico, NO crashea el bot.
- Funciona en los 3 modos del lanzador ([1] kiro-cli orquestador, [2]
  subagentes, [3] daemon Python) porque vive en el dispatcher
  compartido.

## 14. Métricas a monitorear post-deploy

- `diversity_curator_decision` runtime_events: ratio LLM-usado vs
  fallback.
- LLM latency (p50/p95/p99) — esperado <5s.
- Rate de errores por categoría (timeout / json_invalid / id_invalid).
- Distribución de categorías publicadas en últimas 50 ofertas (debería
  ser más uniforme que con random).

---

**Próximo paso:** invocar skill `writing-plans` para descomponer en
tareas implementables.
