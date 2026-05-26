"""Tests para el archivo de steering del MCP server."""

from __future__ import annotations

from pathlib import Path

import pytest


_STEERING_PATH = (
    Path(__file__).resolve().parents[3]
    / ".kiro"
    / "steering"
    / "ofertas-hunter-mcp.md"
)


def test_steering_file_exists() -> None:
    assert _STEERING_PATH.exists(), f"falta {_STEERING_PATH}"


def test_steering_frontmatter_has_inclusion_filematch_and_pattern_star() -> None:
    text = _STEERING_PATH.read_text(encoding="utf-8")
    head = text.split("---", 2)
    assert len(head) >= 3, "frontmatter ausente"
    front = head[1]
    assert "inclusion: fileMatch" in front
    assert 'fileMatchPattern: "*"' in front


def test_steering_has_required_section_headings() -> None:
    text = _STEERING_PATH.read_text(encoding="utf-8")
    required = [
        "## Objetivo del bot",
        "## Hard_Rules intocables",
        "## Ciclo recomendado",
        "## Cuándo NO insistir",
        "## Interpretación de runtime_events",
        "## Anti-patterns",
    ]
    for heading in required:
        assert heading in text, f"falta heading {heading!r}"


def test_steering_documents_hard_rule_tokens() -> None:
    text = _STEERING_PATH.read_text(encoding="utf-8")
    tokens = [
        "hibernating",
        "warmup",
        "paused",
        "cooldown_active",
        "missing_image_url",
        "missing_current_price",
        "missing_url",
        "missing_affiliate_url",
        "telegram_to_ml_blocked",
    ]
    for tok in tokens:
        assert tok in text, f"steering no menciona token {tok!r}"


def test_steering_documents_legacy_anti_patterns() -> None:
    text = _STEERING_PATH.read_text(encoding="utf-8")
    assert "AmazonScrapperIA" in text
    assert "scoring" in text.lower()


def test_mcp_client_setup_doc_exists_and_includes_json_example() -> None:
    docs = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "MCP_CLIENT_SETUP.md"
    )
    assert docs.exists()
    text = docs.read_text(encoding="utf-8")
    assert "mcpServers" in text
    assert "autoApprove" in text
    assert "ofertas-hunter" in text
    # Las 5 read tools deben estar en autoApprove
    for tool in (
        "get_status",
        "get_schedule_mode",
        "get_outbox",
        "get_recent_events",
        "get_frontier_stats",
    ):
        assert tool in text
