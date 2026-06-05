"""Tests del refresco de cookies del ScreenshotCapturer.

Bug raíz (5 jun 2026): el capturer inyectaba cookies UNA sola vez al arrancar
y nunca las refrescaba. Las cookies de Mercado Libre rotan constantemente (el
session manager las reescribe en disco y hace hot-reload en el browser del
hunter), pero el capturer es un singleton aparte que nadie refrescaba. Tras la
primera rotación de ML después del arranque del capturer, sus cookies quedaban
viejas → el PDP de catálogo renderizaba deslogueado (sin `h1.ui-pdp-title`) →
captura None → 100% fallback a imagen pública SOLO en ML (Amazon, con cookies
estáticas, seguía al 100%).

Estos tests verifican que el capturer re-inyecta cookies cuando el archivo de
cookies cambia en disco (detección por mtime), usando un contexto falso (sin
navegador real).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from ofertas_hunter.publishing.screenshot_capturer import ScreenshotCapturer


class _FakeContext:
    """Contexto Playwright falso: registra add_cookies/clear_cookies."""

    def __init__(self) -> None:
        self.add_calls: list[list[dict]] = []
        self.clear_calls = 0

    async def add_cookies(self, cookies):
        self.add_calls.append(list(cookies))

    async def clear_cookies(self):
        self.clear_calls += 1


def _write_ml_cookies(path: Path, n: int) -> None:
    data = [
        {
            "name": f"c{i}",
            "value": "x",
            "domain": ".mercadolibre.com.mx",
            "path": "/",
        }
        for i in range(n)
    ]
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_capturer(tmp_path: Path) -> tuple[ScreenshotCapturer, Path]:
    ml = tmp_path / "ml.json"
    _write_ml_cookies(ml, 3)
    # Archivo Amazon vacío (array []) para aislar del secrets real del host:
    # load() devuelve [] → no se llama add_cookies por Amazon.
    az = tmp_path / "amazon.json"
    az.write_text("[]", encoding="utf-8")
    cap = ScreenshotCapturer(
        mercadolibre_cookies_path=str(ml),
        mercadolibre_cookies_fallback_path=None,
        amazon_cookies_path=str(az),
    )
    return cap, ml


def test_inject_cookies_records_mtimes(tmp_path):
    """Tras inyectar, el capturer recuerda el mtime del archivo de cookies."""
    cap, ml = _make_capturer(tmp_path)
    cap._context = _FakeContext()

    asyncio.run(cap._inject_cookies())

    assert str(ml) in cap._cookie_mtimes_seen


def test_no_refresh_when_files_unchanged(tmp_path):
    """Sin cambios en disco, no se re-inyecta (no clear, no add extra)."""
    cap, ml = _make_capturer(tmp_path)
    ctx = _FakeContext()
    cap._context = ctx

    asyncio.run(cap._inject_cookies())
    assert len(ctx.add_calls) == 1

    refreshed = asyncio.run(cap._maybe_refresh_cookies())
    assert refreshed is False
    assert ctx.clear_calls == 0
    assert len(ctx.add_calls) == 1


def test_refresh_reinjects_when_cookie_file_changes(tmp_path):
    """Si el archivo de cookies cambia (rotación ML), se re-inyecta."""
    cap, ml = _make_capturer(tmp_path)
    ctx = _FakeContext()
    cap._context = ctx

    asyncio.run(cap._inject_cookies())
    assert len(ctx.add_calls) == 1

    # Rotación: reescribir el archivo con un mtime más nuevo.
    _write_ml_cookies(ml, 5)
    new_mtime = time.time() + 5
    os.utime(ml, (new_mtime, new_mtime))

    refreshed = asyncio.run(cap._maybe_refresh_cookies())
    assert refreshed is True
    assert ctx.clear_calls == 1
    assert len(ctx.add_calls) == 2
