"""Tests del DomHealer + HeuristicSelectorRecovery + SelectorVersioner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.self_healing.dom_healer import (
    DomHealer,
    HealOutcome,
    HeuristicSelectorRecovery,
)
from ofertas_hunter.self_healing.selector_versioner import SelectorVersioner


# ---------------------------------------------------------------------------
# HeuristicSelectorRecovery
# ---------------------------------------------------------------------------


class TestHeuristicRecovery:
    def test_extracts_jsonld_price(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@context":"https://schema.org/","@type":"Product",
         "name":"Test","offers":{"@type":"Offer","price":"1234.50"}}
        </script>
        </head><body></body></html>
        """
        recovered = HeuristicSelectorRecovery().recover(html)
        assert recovered["current_price"] == 1234.50

    def test_extracts_og_image(self):
        html = '<meta property="og:image" content="https://x.com/img.jpg">'
        recovered = HeuristicSelectorRecovery().recover(html)
        assert recovered["image_url"] == "https://x.com/img.jpg"

    def test_extracts_og_title(self):
        html = '<meta property="og:title" content="iPhone 16 Pro Max">'
        recovered = HeuristicSelectorRecovery().recover(html)
        assert recovered["title"] == "iPhone 16 Pro Max"

    def test_falls_back_to_regex_price_only_when_no_jsonld(self):
        html = "<p>El precio es $1,299.00 hoy</p>"
        recovered = HeuristicSelectorRecovery().recover(html)
        # Sin JSON-LD pero con regex match
        assert "current_price_regex" in recovered
        assert recovered["current_price_regex"] == 1299.00

    def test_returns_empty_for_blank_html(self):
        assert HeuristicSelectorRecovery().recover("") == {}
        assert HeuristicSelectorRecovery().recover(None) == {}


# ---------------------------------------------------------------------------
# SelectorVersioner
# ---------------------------------------------------------------------------


class TestSelectorVersioner:
    def test_record_and_revert(self, tmp_path: Path):
        db_path = tmp_path / "x.db"
        init_db(db_path)
        conn = connect(db_path)
        v = SelectorVersioner(conn)

        version_id = v.record(
            marketplace="amazon",
            context="product_page",
            key="price_current",
            selector_value="#priceblock_dealprice",
            applied_by="heuristic",
            test_pass=True,
            fixture_path="/data/fixtures/x.html",
        )
        assert version_id > 0

        versions = v.list_for("amazon", "product_page")
        assert len(versions) == 1
        assert versions[0].test_pass is True
        assert versions[0].reverted_at is None

        v.revert(version_id, reason="false_positive")
        versions = v.list_for("amazon", "product_page")
        assert versions[0].reverted_at is not None
        assert "false_positive" in versions[0].applied_by
        conn.close()


# ---------------------------------------------------------------------------
# DomHealer
# ---------------------------------------------------------------------------


def _good_html() -> str:
    return """
<html><head>
<meta property="og:title" content="JBL Tune 510BT">
<meta property="og:image" content="https://m.media-amazon.com/images/I/jbl.jpg">
<script type="application/ld+json">
{"@type":"Product","offers":{"@type":"Offer","price":"388.00"}}
</script>
</head><body></body></html>
""".strip()


def _broken_html() -> str:
    return "<html><body><h1>Error</h1></body></html>"


class TestDomHealer:
    @pytest.mark.asyncio
    async def test_heal_records_snapshot_and_fixture_when_recovery_succeeds(self, tmp_path: Path):
        db_path = tmp_path / "x.db"
        init_db(db_path)
        conn = connect(db_path)
        fixture_dir = tmp_path / "heal_samples"

        def tester(_path: Path) -> bool:
            return True  # simula tests OK

        healer = DomHealer(conn, fixture_root=fixture_dir, tester=tester)
        outcome = healer.heal(
            marketplace="amazon",
            context="product_page",
            url="https://www.amazon.com.mx/dp/B0EXAMPLEK",
            html=_good_html(),
        )
        assert outcome.success is True
        assert outcome.snapshot_id is not None
        assert outcome.fixture_path is not None
        assert outcome.fixture_path.exists()
        assert outcome.recovered_fields["current_price"] == 388.0
        assert outcome.recovered_fields["image_url"].startswith("https://")
        assert outcome.recovered_fields["title"] == "JBL Tune 510BT"

        rows = conn.execute(
            "SELECT context, reason FROM dom_snapshots"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["context"] == "product_page"

        versions = SelectorVersioner(conn).list_for("amazon", "product_page")
        assert len(versions) == 1
        assert versions[0].test_pass is True
        assert versions[0].reverted_at is None
        conn.close()

    @pytest.mark.asyncio
    async def test_heal_returns_failure_when_no_recovery_possible(self, tmp_path: Path):
        db_path = tmp_path / "x.db"
        init_db(db_path)
        conn = connect(db_path)
        fixture_dir = tmp_path / "heal_samples"

        def tester(_path: Path) -> bool:
            return True

        healer = DomHealer(conn, fixture_root=fixture_dir, tester=tester)
        outcome = healer.heal(
            marketplace="amazon",
            context="product_page",
            url="https://www.amazon.com.mx/dp/B0BROKEN",
            html=_broken_html(),
        )
        assert outcome.success is False
        assert "no_recovered_fields" in outcome.reasons
        # Snapshot sí se guardó.
        rows = conn.execute("SELECT count(*) AS n FROM dom_snapshots").fetchone()
        assert rows["n"] == 1
        # Pero NO se registró version (recovery vacío).
        versions = SelectorVersioner(conn).list_for("amazon", "product_page")
        assert versions == []
        conn.close()

    @pytest.mark.asyncio
    async def test_heal_reverts_version_when_tests_fail(self, tmp_path: Path):
        db_path = tmp_path / "x.db"
        init_db(db_path)
        conn = connect(db_path)
        fixture_dir = tmp_path / "heal_samples"

        def tester(_path: Path) -> bool:
            return False  # tests fallan → revert

        healer = DomHealer(conn, fixture_root=fixture_dir, tester=tester)
        outcome = healer.heal(
            marketplace="amazon",
            context="product_page",
            url="https://www.amazon.com.mx/dp/B0EXAMPLEK",
            html=_good_html(),
        )
        assert outcome.success is False
        assert "tests_failed" in outcome.reasons
        # La version quedó marcada con test_pass=0 y reverted_at.
        versions = SelectorVersioner(conn).list_for("amazon", "product_page")
        assert len(versions) == 1
        assert versions[0].test_pass is False
        assert versions[0].reverted_at is not None
        conn.close()

    @pytest.mark.asyncio
    async def test_heal_handles_tester_exception(self, tmp_path: Path):
        db_path = tmp_path / "x.db"
        init_db(db_path)
        conn = connect(db_path)
        fixture_dir = tmp_path / "heal_samples"

        def tester(_path: Path) -> bool:
            raise RuntimeError("pytest crashed")

        healer = DomHealer(conn, fixture_root=fixture_dir, tester=tester)
        outcome = healer.heal(
            marketplace="ml",
            context="product_page",
            url="https://articulo.mercadolibre.com.mx/MLM12345678",
            html=_good_html(),
        )
        assert outcome.success is False
        assert any("tester_error" in r for r in outcome.reasons)
        conn.close()
