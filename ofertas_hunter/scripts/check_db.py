#!/usr/bin/env python
"""Wrapper: estado rápido de la DB."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ofertas_hunter.__main__ import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(["check-db", *sys.argv[1:]]))
