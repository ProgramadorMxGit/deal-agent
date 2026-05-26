"""Configuración global de pytest."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Asegurar que `src/` esté en sys.path para los imports del paquete.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


PRICE_ERROR_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "price_errors"


@pytest.fixture
def price_error_fixtures_dir() -> Path:
    return PRICE_ERROR_FIXTURES


@pytest.fixture
def load_price_error_example():
    def _load(letter: str) -> dict:
        path = PRICE_ERROR_FIXTURES / f"example_{letter.lower()}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    return _load
