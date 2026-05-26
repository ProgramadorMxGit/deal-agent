"""Carga seeds.json y los clasifica para validar que la config es correcta.

NO toca la red ni Playwright. Útil después de editar `config/seeds/*.json`.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ofertas_hunter.exploration.url_classifier import classify  # noqa: E402


def _load(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [str(x) for x in raw if isinstance(x, str)]
    return []


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    files = [
        ("amazon", project_root / "config/seeds/amazon.json"),
        ("mercadolibre", project_root / "config/seeds/mercadolibre.json"),
    ]
    total_ok = 0
    total_bad = 0
    for marketplace, path in files:
        urls = _load(path)
        kinds = Counter()
        bad = []
        for url in urls:
            info = classify(url)
            if info.marketplace != marketplace or info.kind == "unknown":
                bad.append((url, info.marketplace, info.kind))
            else:
                kinds[info.kind] += 1
        print(f"\n--- {marketplace}: {path.name} ({len(urls)} URLs) ---")
        for kind, n in kinds.most_common():
            print(f"  {kind:<10s} {n}")
        if bad:
            print(f"  ⚠ {len(bad)} URLs no aceptables:")
            for url, mk, kind in bad[:5]:
                print(f"     - {url}  → marketplace={mk} kind={kind}")
        total_ok += sum(kinds.values())
        total_bad += len(bad)

    print(f"\n=== total OK: {total_ok}  malas: {total_bad} ===")
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
