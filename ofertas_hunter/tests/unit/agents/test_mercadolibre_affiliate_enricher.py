"""Tests del MercadoLibreAffiliateEnricher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from ofertas_hunter.agents.mercadolibre_affiliate_enricher import (
    EnrichmentReport,
    MercadoLibreAffiliateEnricher,
)
from ofertas_hunter.db import connect, init_db
from ofertas_hunter.marketplaces.mercadolibre_affiliate import (
    AffiliateExtractor,
    AffiliateInfo,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAffiliateExtractor:
    def __init__(self, *, affiliate_url: Optional[str] = "https://meli.la/abc",
                 affiliate_product_id: Optional[str] = "FAKE-123",
                 commission_text: Optional[str] = "COMISIÓN 9%",
                 error: Optional[str] = None):
        self.affiliate_url = affiliate_url
        self.affiliate_product_id = affiliate_product_id
        self.commission_text = commission_text
        self.error = error
        self.calls: list[str] = []

    async def extract(self, url: str) -> AffiliateInfo:
        self.calls.append(url)
        if self.error:
            return AffiliateInfo(success=False, error=self.error)
        return AffiliateInfo(
            affiliate_url=self.affiliate_url,
            affiliate_product_id=self.affiliate_product_id,
            commission_text=self.commission_text,
            success=True,
        )

    async def aclose(self) -> None:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_outbox(conn, items: list[dict]) -> list[int]:
    """Inserta items mínimos en outbox para tests. Devuelve los IDs."""
    ids = []
    for item in items:
        # 1) Insertar producto y oferta dummy para satisfacer FKs.
        url_canonical = item.get("payload", {}).get("canonical_url") or f"https://x.com/{len(ids)}"
        cur = conn.execute(
            "INSERT INTO products (marketplace, url_canonical, title, condition, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, 'new', ?, ?)",
            (
                item["payload"].get("marketplace") or "mercadolibre",
                url_canonical,
                item["payload"].get("title", "Test"),
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
            "INSERT INTO outbox (offer_id, type, enqueued_at, attempts, state, "
            "message_payload_json) VALUES (?, ?, ?, 0, ?, ?)",
            (
                offer_id,
                item.get("type", "normal"),
                "2026-05-25T00:00:00.000Z",
                item.get("state", "pending"),
                json.dumps(item["payload"], ensure_ascii=False),
            ),
        )
        ids.append(cur.lastrowid)
    return ids


def _ml_payload(*, source: str = "mercadolibre_hunter",
                with_affiliate: bool = False,
                marketplace: str = "mercadolibre",
                canonical: str = "https://articulo.mercadolibre.com.mx/MLM98765432") -> dict:
    payload = {
        "title": "Sony WH-1000XM5",
        "current_price": 3499,
        "previous_price": 8999,
        "discount_percent": 61,
        "image_url": "https://x.com/img.jpg",
        "marketplace": marketplace,
        "source": source,
        "canonical_url": canonical,
        "url": canonical,
    }
    if with_affiliate:
        payload["affiliate_url"] = "https://meli.la/already-set"
        payload["affiliate_product_id"] = "OLD-123"
        payload["commission_text"] = "COMISIÓN 9%"
    return payload


# ---------------------------------------------------------------------------
# Tests obligatorios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ml_enrich_affiliates_skips_telegram_source(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    ids = _setup_outbox(
        conn,
        [
            {"payload": _ml_payload(source="telegram",
                                    canonical="https://articulo.mercadolibre.com.mx/MLM11111111")},
            {"payload": _ml_payload(source="mercadolibre_hunter",
                                    canonical="https://articulo.mercadolibre.com.mx/MLM22222222")},
        ],
    )

    extractor = FakeAffiliateExtractor()
    enricher = MercadoLibreAffiliateEnricher(conn, extractor)
    report = await enricher.run(limit=10)

    # Sólo el del hunter propio debe llamarse al extractor.
    assert len(extractor.calls) == 1
    assert "MLM22222222" in extractor.calls[0]

    # El item Telegram queda intacto.
    row = conn.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?", (ids[0],)
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload["source"] == "telegram"
    assert payload.get("affiliate_url") is None
    assert "affiliate_status" not in payload  # no se tocó

    # El otro sí se enriqueció.
    row = conn.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?", (ids[1],)
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload["affiliate_url"] == "https://meli.la/abc"
    assert payload["affiliate_status"] == "ok"

    assert report.enriched == 1
    assert report.skipped == 0  # los telegram no entran ni a candidatos
    assert report.total_candidates == 1
    conn.close()


@pytest.mark.asyncio
async def test_ml_enrich_affiliates_skips_items_with_existing_affiliate_url(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    ids = _setup_outbox(
        conn,
        [
            {"payload": _ml_payload(with_affiliate=True,
                                    canonical="https://articulo.mercadolibre.com.mx/MLM33333333")},
            {"payload": _ml_payload(with_affiliate=False,
                                    canonical="https://articulo.mercadolibre.com.mx/MLM44444444")},
        ],
    )

    extractor = FakeAffiliateExtractor()
    enricher = MercadoLibreAffiliateEnricher(conn, extractor)
    report = await enricher.run(limit=10)

    # Sólo el item sin afiliado se procesa.
    assert len(extractor.calls) == 1
    assert "MLM44444444" in extractor.calls[0]
    assert report.enriched == 1
    assert report.total_candidates == 1

    # El item con afiliado preexistente conserva el original.
    row = conn.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?", (ids[0],)
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload["affiliate_url"] == "https://meli.la/already-set"
    conn.close()


@pytest.mark.asyncio
async def test_ml_enrich_affiliates_updates_outbox_payload(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    ids = _setup_outbox(
        conn,
        [{"payload": _ml_payload(canonical="https://articulo.mercadolibre.com.mx/MLM55555555")}],
    )
    extractor = FakeAffiliateExtractor()
    enricher = MercadoLibreAffiliateEnricher(conn, extractor)
    report = await enricher.run(limit=10)

    row = conn.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?", (ids[0],)
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload["affiliate_url"] == "https://meli.la/abc"
    assert payload["affiliate_product_id"] == "FAKE-123"
    assert payload["commission_text"] == "COMISIÓN 9%"
    # `url` (publish) ahora es affiliate.
    assert payload["url"] == "https://meli.la/abc"
    # `canonical_url` se conserva.
    assert payload["canonical_url"] == "https://articulo.mercadolibre.com.mx/MLM55555555"
    # Marca de status.
    assert payload["affiliate_status"] == "ok"
    assert payload["affiliate_error"] is None
    assert "affiliate_enriched_at" in payload

    # Se actualizó también la tabla products.
    prod_row = conn.execute(
        "SELECT affiliate_link, affiliate_product_id, commission_text "
        "FROM products WHERE url_canonical = ?",
        ("https://articulo.mercadolibre.com.mx/MLM55555555",),
    ).fetchone()
    assert prod_row["affiliate_link"] == "https://meli.la/abc"
    assert prod_row["affiliate_product_id"] == "FAKE-123"
    assert prod_row["commission_text"] == "COMISIÓN 9%"

    assert report.enriched == 1
    conn.close()


@pytest.mark.asyncio
async def test_ml_enrich_affiliates_records_failure(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    ids = _setup_outbox(
        conn,
        [{"payload": _ml_payload(canonical="https://articulo.mercadolibre.com.mx/MLM66666666")}],
    )

    extractor = FakeAffiliateExtractor(error="modal_textareas_empty")
    enricher = MercadoLibreAffiliateEnricher(conn, extractor)
    report = await enricher.run(limit=10)

    row = conn.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?", (ids[0],)
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload.get("affiliate_url") is None  # no se tocó
    assert payload["affiliate_status"] == "failed"
    assert payload["affiliate_error"] == "modal_textareas_empty"
    assert "affiliate_enriched_at" in payload

    # url sigue siendo canonical (no se modifica)
    assert payload["url"] == "https://articulo.mercadolibre.com.mx/MLM66666666"

    assert report.enriched == 0
    assert report.failed == 1
    conn.close()


@pytest.mark.asyncio
async def test_ml_enrich_affiliates_marks_pending_when_no_extractor(tmp_path):
    """Sin extractor (Playwright no instalado) → status=pending para reintento."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    ids = _setup_outbox(
        conn,
        [{"payload": _ml_payload(canonical="https://articulo.mercadolibre.com.mx/MLM77777777")}],
    )
    enricher = MercadoLibreAffiliateEnricher(conn, extractor=None)
    report = await enricher.run(limit=10)

    row = conn.execute(
        "SELECT message_payload_json FROM outbox WHERE id = ?", (ids[0],)
    ).fetchone()
    payload = json.loads(row["message_payload_json"])
    assert payload["affiliate_status"] == "pending"
    assert payload["affiliate_error"] == "no_extractor"
    assert payload.get("affiliate_url") is None
    assert report.failed == 1
    conn.close()


@pytest.mark.asyncio
async def test_ml_enrich_affiliates_respects_limit(tmp_path):
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    payloads = [
        {"payload": _ml_payload(canonical=f"https://articulo.mercadolibre.com.mx/MLM{i:08d}")}
        for i in range(5)
    ]
    _setup_outbox(conn, payloads)

    extractor = FakeAffiliateExtractor()
    enricher = MercadoLibreAffiliateEnricher(conn, extractor)
    report = await enricher.run(limit=2)
    assert report.total_candidates == 2
    assert report.enriched == 2
    assert len(extractor.calls) == 2
    conn.close()


@pytest.mark.asyncio
async def test_ml_enrich_affiliates_skips_non_mercadolibre(tmp_path):
    """Items de otros marketplaces nunca se tocan."""
    db_path = tmp_path / "x.db"
    init_db(db_path)
    conn = connect(db_path)

    ids = _setup_outbox(
        conn,
        [
            {
                "payload": _ml_payload(
                    marketplace="amazon",
                    canonical="https://www.amazon.com.mx/dp/B0EXAMPLEK",
                )
            }
        ],
    )

    extractor = FakeAffiliateExtractor()
    enricher = MercadoLibreAffiliateEnricher(conn, extractor)
    report = await enricher.run(limit=10)

    assert extractor.calls == []
    assert report.total_candidates == 0
    conn.close()
