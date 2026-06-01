"""Tests del AmazonAffiliateEnricher."""

from __future__ import annotations

import json

import pytest

from ofertas_hunter.agents.amazon_affiliate_enricher import (
    AmazonAffiliateEnricher,
)
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.marketplaces.amazon_affiliate import (
    AffiliateInfo,
)


class FakeAffiliateExtractor:
    def __init__(
        self,
        *,
        affiliate_url: str | None = "https://amzn.to/abc123",
        store_id: str | None = "programadormx-20",
        tracking_id: str | None = "programadormx-20",
        error: str | None = None,
    ) -> None:
        self.affiliate_url = affiliate_url
        self.store_id = store_id
        self.tracking_id = tracking_id
        self.error = error
        self.calls: list[str] = []

    async def extract(self, url: str) -> AffiliateInfo:
        self.calls.append(url)
        if self.error:
            return AffiliateInfo(success=False, error=self.error)
        return AffiliateInfo(
            affiliate_url=self.affiliate_url,
            store_id=self.store_id,
            tracking_id=self.tracking_id,
            success=bool(self.affiliate_url),
        )

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _setup_outbox(conn, items: list[dict]) -> list[int]:
    ids = []
    for item in items:
        payload = item["payload"]
        url_canonical = payload.get("canonical_url") or "https://www.amazon.com.mx/dp/B0TEST0001"
        cur = conn.execute(
            "INSERT INTO products (marketplace, url_canonical, title, condition, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, 'new', ?, ?)",
            (
                payload.get("marketplace") or "amazon",
                url_canonical,
                payload.get("title", "Test Amazon"),
                "2026-05-25T00:00:00.000Z",
                "2026-05-25T00:00:00.000Z",
            ),
        )
        product_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO offers (product_id, classification, score, reasons_json, "
            "state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                product_id,
                "normal_offer",
                70,
                "[]",
                "eligible",
                "2026-05-25T00:00:00.000Z",
                "2026-05-25T00:00:00.000Z",
            ),
        )
        offer_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO outbox (offer_id, type, enqueued_at, attempts, state, message_payload_json) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (
                offer_id,
                item.get("type", "normal"),
                "2026-05-25T00:00:00.000Z",
                item.get("state", "pending"),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        ids.append(cur.lastrowid)
    return ids


def _amazon_payload(
    *,
    source: str = "amazon_hunter",
    with_affiliate: bool = False,
    marketplace: str = "amazon",
    canonical: str = "https://www.amazon.com.mx/dp/B0EXAMPLEK",
) -> dict:
    payload = {
        "title": "JBL Tune 510BT",
        "current_price": 388,
        "previous_price": 899,
        "discount_percent": 56,
        "image_url": "https://m.media-amazon.com/images/I/example.jpg",
        "marketplace": marketplace,
        "source": source,
        "canonical_url": canonical,
        "url": canonical,
        "asin": canonical.rsplit("/", 1)[-1],
    }
    if with_affiliate:
        payload["affiliate_url"] = "https://amzn.to/existing"
        payload["affiliate_status"] = "ok"
    return payload


@pytest.mark.asyncio
async def test_amazon_enrich_affiliates_updates_outbox_payload(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    ids = _setup_outbox(
        conn,
        [{"payload": _amazon_payload(canonical="https://www.amazon.com.mx/dp/B0EXAMPLEK")}],
    )

    enricher = AmazonAffiliateEnricher(conn, FakeAffiliateExtractor())
    report = await enricher.run(limit=10)

    row = conn.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?",
        (ids[0],),
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload["affiliate_url"] == "https://amzn.to/abc123"
    assert payload["url"] == "https://amzn.to/abc123"
    assert payload["canonical_url"] == "https://www.amazon.com.mx/dp/B0EXAMPLEK"
    assert payload["affiliate_status"] == "ok"
    assert payload["affiliate_store_id"] == "programadormx-20"
    assert payload["affiliate_tracking_id"] == "programadormx-20"
    assert report.enriched == 1

    product = conn.execute(
        "SELECT affiliate_link FROM products WHERE url_canonical = ?",
        ("https://www.amazon.com.mx/dp/B0EXAMPLEK",),
    ).fetchone()
    assert product["affiliate_link"] == "https://amzn.to/abc123"
    conn.close()


@pytest.mark.asyncio
async def test_amazon_enrich_affiliates_skips_items_with_existing_affiliate(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    _setup_outbox(
        conn,
        [
            {"payload": _amazon_payload(with_affiliate=True, canonical="https://www.amazon.com.mx/dp/B0KEEP001")},
            {"payload": _amazon_payload(with_affiliate=False, canonical="https://www.amazon.com.mx/dp/B0PROC001")},
        ],
    )

    extractor = FakeAffiliateExtractor()
    enricher = AmazonAffiliateEnricher(conn, extractor)
    report = await enricher.run(limit=10)

    assert report.total_candidates == 1
    assert extractor.calls == ["https://www.amazon.com.mx/dp/B0PROC001"]
    conn.close()


@pytest.mark.asyncio
async def test_amazon_enrich_affiliates_records_failure(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)
    ids = _setup_outbox(
        conn,
        [{"payload": _amazon_payload(canonical="https://www.amazon.com.mx/dp/B0FAIL001")}],
    )

    enricher = AmazonAffiliateEnricher(
        conn,
        FakeAffiliateExtractor(error="clipboard_unavailable"),
    )
    report = await enricher.run(limit=10)

    row = conn.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?",
        (ids[0],),
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload.get("affiliate_url") is None
    assert payload["url"] == "https://www.amazon.com.mx/dp/B0FAIL001"
    assert payload["affiliate_status"] == "failed"
    assert payload["affiliate_error"] == "clipboard_unavailable"
    assert report.failed == 1
    conn.close()
