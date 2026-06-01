# Amazon Affiliate Enforcement + Verified Discount Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Guarantee that no Amazon item is published without (a) a valid affiliate link and (b) a real, verified previous price; and stop inventing discounts.

**Architecture:** Defense in depth at three layers — (1) a hard publish-time gate in `WhatsAppPublisher` that is the final chokepoint, (2) stricter extraction in the legacy price parser that refuses unverifiable previous prices, (3) an automatic affiliate-enrichment phase in the orchestrator plus a backfill CLI. The publish gate alone satisfies the safety acceptance criteria; the other layers reduce how many items reach the gate blocked.

**Tech Stack:** Python 3.11/3.12, pytest, pytest-asyncio, BeautifulSoup/Playwright, SQLite.

---

## Key facts (verified in code)

- Publish chokepoint: `src/ofertas_hunter/publishing/whatsapp_publisher.py::WhatsAppPublisher.publish`.
- `PublishOutcome.discard_reason` → dispatcher marks outbox `discarded` (see `dispatcher._publish_item`). No retry. No schema change needed.
- Legacy fake-discount origin: `src/ofertas_hunter/agents/legacy_amazon/price_parser.py::_extract_original_price`.
- Legacy enqueue payload: `src/ofertas_hunter/agents/legacy_amazon/adapter.py::_enqueue_outbox` (no affiliate, `url=canonical`).
- Affiliate enricher (exists, not wired into loop): `src/ofertas_hunter/agents/amazon_affiliate_enricher.py`.
- Orchestrator loop in production: `scripts/orquestador_ia.py` (runs hunt_amazon / hunt_mercadolibre / process_telegram / dispatch_outbox).
- Valid affiliate link = contains `amzn.to/` OR `tag=`.

---

## Phase 1 — Amazon publish-time hard gate (CRITICAL, self-contained)

Satisfies acceptance criteria 1, 2, 3, 4, 5, 7, 8. Ship this first.

### Task 1.1: Affiliate validity helper

**Files:**
- Modify: `src/ofertas_hunter/publishing/whatsapp_publisher.py`
- Test: `tests/unit/publishing/test_amazon_publish_gate.py`

- [ ] **Step 1: Write failing tests**

```python
from ofertas_hunter.publishing.whatsapp_publisher import _is_valid_affiliate_url

def test_amzn_short_link_is_valid():
    assert _is_valid_affiliate_url("https://amzn.to/4abc") is True

def test_tag_param_is_valid():
    assert _is_valid_affiliate_url("https://www.amazon.com.mx/dp/B0X?tag=ofertones03-20") is True

def test_plain_dp_url_is_invalid():
    assert _is_valid_affiliate_url("https://www.amazon.com.mx/dp/B0DXMMNHYV") is False

def test_empty_is_invalid():
    assert _is_valid_affiliate_url("") is False
    assert _is_valid_affiliate_url(None) is False
```

- [ ] **Step 2:** Run `pytest tests/unit/publishing/test_amazon_publish_gate.py -q` → FAIL (import error).

- [ ] **Step 3: Implement helper** in `whatsapp_publisher.py` (module level):

```python
def _is_valid_affiliate_url(url: Optional[str]) -> bool:
    if not url or not isinstance(url, str):
        return False
    return ("amzn.to/" in url) or ("tag=" in url)
```

- [ ] **Step 4:** Run tests → PASS.

- [ ] **Step 5:** Commit `feat(publish): add amazon affiliate url validator`.

### Task 1.2: Amazon gate method (affiliate + verified old price)

**Files:**
- Modify: `src/ofertas_hunter/publishing/whatsapp_publisher.py`
- Modify: `src/ofertas_hunter/config.py` (add `amazon_affiliate_required_for_publish: bool = True`, `amazon_min_discount_percent: float = 50.0`)
- Test: `tests/unit/publishing/test_amazon_publish_gate.py`

- [ ] **Step 1: Write failing tests** (publisher-level, dry-run client). Cases:
  - Amazon NORMAL item, no affiliate → `discard_reason == "amazon_missing_affiliate"`, success False.
  - Amazon NORMAL item with `amzn.to` url but `previous_price=None` / `old_price_verified` falsy → `discard_reason == "amazon_no_verified_old_price"`.
  - Amazon NORMAL item with valid affiliate + `old_price_verified=True` + valid previous>current → publishes (dry_run success) and the published URL is the affiliate url.
  - The $350 sudadera fixture payload (affiliate present, `previous_price=1422.61`, `old_price_verified=False`) → `discard_reason == "amazon_no_verified_old_price"`.
  - Amazon item where `discount_percent` disagrees with computed (old/current) by >2 pts → `discard_reason == "amazon_discount_mismatch"`.

```python
# representative case
@pytest.mark.asyncio
async def test_amazon_blocks_unverified_old_price(dry_run_client):
    publisher = WhatsAppPublisher(client=dry_run_client, target_group_id="g@g.us",
                                  enabled=True, amazon_affiliate_required=True)
    item = OutboxItem(offer_id=1, type=OutboxType.NORMAL.value, message_payload={
        "title": "Sudadera", "image_url": "http://x/i.jpg",
        "current_price": 350, "previous_price": 1422.61, "discount_percent": 75,
        "old_price_verified": False, "marketplace": "amazon",
        "affiliate_url": "https://amzn.to/4abc", "url": "https://amzn.to/4abc",
    })
    out = await publisher.publish(item)
    assert out.success is False
    assert out.discard_reason == "amazon_no_verified_old_price"
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement.** Add ctor param `amazon_affiliate_required: bool = True` and `amazon_min_discount_percent: float = 50.0`. Add `_amazon_gate(item) -> Optional[PublishOutcome]` called at the top of `publish()` (after target check, before ML gate):

```python
def _amazon_gate(self, item):
    payload = item.message_payload or {}
    if (payload.get("marketplace") or "").lower() != "amazon":
        return None
    # 1. affiliate required
    if self.amazon_affiliate_required:
        aff = payload.get("affiliate_url")
        if not _is_valid_affiliate_url(aff):
            return self._amazon_block(item, "amazon_missing_affiliate")
    # 2. only enforce old-price rules for NORMAL (discount) offers
    if item.type == OutboxType.NORMAL.value:
        if not payload.get("old_price_verified"):
            return self._amazon_block(item, "amazon_no_verified_old_price")
        cur = payload.get("current_price"); prev = payload.get("previous_price")
        try:
            cur = float(cur); prev = float(prev)
        except (TypeError, ValueError):
            return self._amazon_block(item, "amazon_no_verified_old_price")
        if prev <= cur:
            return self._amazon_block(item, "amazon_no_verified_old_price")
        computed = round((prev - cur) / prev * 100)
        declared = payload.get("discount_percent")
        if declared is not None and abs(float(declared) - computed) > 2:
            return self._amazon_block(item, "amazon_discount_mismatch")
        if computed < self.amazon_min_discount_percent:
            return self._amazon_block(item, "amazon_below_min_discount")
    return None

def _amazon_block(self, item, reason):
    logger.warning("amazon gate blocked id=%s reason=%s", item.id, reason)
    return PublishOutcome(success=False, dry_run=self.client.dry_run,
                          formatted=None, evolution_response=None,
                          error=reason, discard_reason=reason)
```

  Wire `gate = self._amazon_gate(item); if gate: return gate` into `publish()`.

- [ ] **Step 4:** Run → PASS. Run full `tests/unit/publishing/` → green.

- [ ] **Step 5:** Wire ctor flag at the 3 construction sites (`__main__.py`, `orchestrator.py`, `mcp/context.py`) from `settings.amazon_affiliate_required_for_publish` and `settings.amazon_min_discount_percent`. Commit.

### Task 1.3: Formatter never shows unverified "Antes"

**Files:**
- Modify: `src/ofertas_hunter/publishing/whatsapp_publisher.py::_format_message`
- Test: same file as above

- [ ] **Step 1:** Failing test: Amazon item reaching `_format_message` without `old_price_verified` must NOT derive `previous_price` from `discount_percent` (the existing ML-style derivation). For Amazon, missing verified previous → `FormatterError`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** In `_format_message`, guard the `previous_price` derivation block with `if marketplace != "amazon"`. For Amazon, require a real `previous_price` and `old_price_verified`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit.

---

## Phase 2 — Strict Amazon previous-price extraction (reduce blocked volume)

### Task 2.1: Verified old-price extractor in legacy parser

**Files:**
- Modify: `src/ofertas_hunter/agents/legacy_amazon/price_parser.py`
- Test: `tests/unit/agents/legacy_amazon/test_price_parser_old_price.py`
- Fixtures: `tests/fixtures/amazon/*.html`

- [ ] **Step 1: Write failing tests with HTML fixtures** (pure function `extract_verified_old_price(html) -> tuple[Optional[float], Optional[str]]` returning `(old_price, source)`):
  - `no_strike_350.html`: only current `$350.00`, no strike → `(None, None)`.
  - `valid_strike.html`: visible `.basisPrice .a-text-price` strike > current → `(value, "basis_price_strike")`.
  - `variants_higher_prices.html`: selected product no strike, variant swatches with higher prices → `(None, None)` (must NOT pick variant prices).
  - `monthly_msi.html`: `$35.52 x 12 meses` present → never used as old/current.
  - `other_sellers.html`: other-sellers block has higher price → `(None, None)`.

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** `extract_verified_old_price`: only accept a strike price that lives inside the main price block (`#corePriceDisplay_desktop_feature_div`, `#apex_offerDisplay_desktop`, `#corePrice_desktop`) AND is marked list/was/basis (`.basisPrice .a-price.a-text-price .a-offscreen`, `#priceblock_was_price`, elements with explicit "Precio de lista"/"Precio recomendado" label). Reject anything under variant (`#twister`, `#tp-inline-twister-dim-values-container`, `[id*='variation']`), other-sellers (`#aod-offer`, `#mbc`), or monthly/MSI text (`detect_monthly_payment`). Return source tag.

- [ ] **Step 4:** Run → PASS.

- [ ] **Step 5:** Commit.

### Task 2.2: Wire verified old-price into extraction + adapter payload

**Files:**
- Modify: `price_parser.py::extract_product_data_from_page` (set `price_original` only from `extract_verified_old_price`; add `old_price_source`, `old_price_verified`).
- Modify: `legacy_amazon/adapter.py::_build_extracted_product` and `_enqueue_outbox` (carry `old_price_verified`, `old_price_source`, `discount_percent_verified`; if not verified → `previous_price=None`, `discount_percent=None`, and `_decide_outbox_type` must not emit NORMAL).
- Modify: `adapter.py::_decide_outbox_type` — NORMAL requires `extracted.old_price_verified and discount>=min`.
- Test: `tests/unit/agents/test_legacy_amazon_adapter.py` (extend).

- [ ] Steps 1-5 mirror TDD: failing test that the $350 fixture flows through the adapter to a discard with reason `no_verified_old_price` (not an enqueue), then implement, then green, then commit.

### Task 2.3: Payload traceability fields

**Files:** `adapter.py::_enqueue_outbox`, `amazon_hunter_agent.py` enqueue.
- [ ] Add to payload: `old_price`, `old_price_source`, `old_price_verified`, `discount_percent`, `discount_percent_verified`, `affiliate_url`, `affiliate_valid`, `validation_errors`, `reject_reason`. TDD per field presence. Commit.

---

## Phase 3 — Automatic affiliate enrichment in the loop

### Task 3.1: Enrich phase in orchestrator

**Files:**
- Modify: `scripts/orquestador_ia.py` (new `loop_amazon_affiliate_enrich` or call before `dispatch_outbox`).
- Modify: `src/ofertas_hunter/config.py` (`amazon_affiliate_enrich_limit: int = 5`, `amazon_affiliate_enrich_timeout_seconds: float = 90.0`).
- Reuse: `AmazonAffiliateEnricher`.

- [ ] Add a guarded step: before each `dispatch_outbox`, run enricher over Amazon `pending` items lacking valid affiliate, with `asyncio.wait_for(timeout)`, emitting `affiliate_enrich_started`/`affiliate_enriched`/`affiliate_enrich_failed`/`affiliate_missing_blocked` runtime events. Errors must not crash the loop (try/except + log).
- [ ] Manual/integration validation (no unit harness for the script): documented dry run.
- [ ] Commit.

### Task 3.2: MCP tool wrapper (optional, enables in-loop call cleanly)

**Files:** `src/ofertas_hunter/mcp/tools/action_tools.py` — add `enrich_amazon_affiliates` ToolSpec calling the enricher via `ctx`.
- [ ] TDD against the MCP server dispatch (fake enricher), assert event counts. Commit.

---

## Phase 4 — Backfill / cleanup CLI

### Task 4.1: `amazon-sanitize-outbox` command

**Files:**
- Modify: `src/ofertas_hunter/__main__.py` (new subcommand) reusing enricher + a pure validator from Phase 1/2.
- Test: `tests/unit/cli/test_amazon_sanitize.py` (logic extracted into a pure function `classify_amazon_pending(payload) -> ("ok"|"needs_affiliate"|"no_verified_old_price"|"has_affiliate")`).

- [ ] Behavior: scan Amazon `pending`; try enrich those missing affiliate; items still without valid affiliate → `discarded` reason `needs_affiliate`; items without `old_price_verified` → `discarded` reason `no_verified_old_price`. Print summary: total, enriched, failed, blocked_affiliate, blocked_old_price, already_ok.
- [ ] TDD the classifier, then wire CLI. Commit.

---

## Phase 5 — Integration + validation

### Task 5.1: Dispatcher integration tests

**Files:** `tests/integration/dispatching/test_amazon_publish_guardrails.py`
- [ ] Amazon no affiliate → not published. Amazon affiliate-valid but unverified old price → not published. Amazon affiliate-valid + verified discount → published using `affiliate_url` not `url`. (Use dry-run client + SqliteOutbox.)
- [ ] Commit.

### Task 5.2: VPS validation (manual, documented)
- [ ] `python -m ofertas_hunter amazon-enrich-affiliates --limit 2` on VPS; confirm `affiliate_url` generated and SiteStripe session alive.
- [ ] SQL audit (counts of pending Amazon: valid affiliate / blocked-affiliate / blocked-old-price).
- [ ] Confirm dispatcher emits no Amazon publish with bare `amazon.com.mx/dp/...`.

---

## Self-review notes
- Acceptance 1,2,3,4,5,7,8 are satisfied by Phase 1 alone (the gate). Phases 2-4 reduce blocked volume and add provenance.
- `_is_valid_affiliate_url` name used consistently across tasks.
- No new outbox state introduced; uses existing `discard_reason` → `discarded`.
- The $350 sudadera case is covered by Task 1.2 (gate) AND Task 2.1/2.2 (extractor), giving belt-and-suspenders.
