#!/usr/bin/env python
"""Wrapper conveniencia: enriquece items ML existentes con affiliate_url.

Llama al mismo servicio interno que `python -m ofertas_hunter ml-enrich-affiliates`.
No duplica lógica.

Uso:
    python scripts/enrich_affiliate_links.py
    python scripts/enrich_affiliate_links.py --limit 50
    python scripts/enrich_affiliate_links.py --no-headless
    python scripts/enrich_affiliate_links.py --no-extractor    # marca pending sin Playwright
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ofertas_hunter.__main__ import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(["ml-enrich-affiliates", *sys.argv[1:]]))
