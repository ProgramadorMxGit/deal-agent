"""Inspecciona el HTML guardado en data/debug/amazon_test.html para
encontrar productos con descuento."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/debug/amazon_test.html")
    html = path.read_text(encoding="utf-8", errors="ignore")
    print(f"HTML: {len(html)} bytes")

    # 1) Buscar todos los textos visibles que parezcan "-NN%"
    discounts = re.findall(r"-(\d{2,3})\s*%", html)
    print(f"\nDescuentos encontrados (texto): {sorted(set(int(d) for d in discounts), reverse=True)[:20]}")

    # 2) Buscar todos los ASINs únicos
    asins = sorted(set(re.findall(r'/dp/([A-Z0-9]{10})', html)))
    print(f"\nASINs únicos en la página: {len(asins)}")
    print("Primeros 20:", asins[:20])

    # 3) Para cada -NN% con NN >= 30, encontrar el ASIN más cercano hacia atrás
    print("\nProductos con descuento >=30%:")
    seen = set()
    for m in re.finditer(r"-(\d{2,3})\s*%", html):
        try:
            d = int(m.group(1))
        except ValueError:
            continue
        if d < 30:
            continue
        # Buscar ASIN dentro de los 8000 chars anteriores y 4000 posteriores
        chunk_before = html[max(0, m.start() - 8000) : m.start()]
        chunk_after = html[m.end() : min(len(html), m.end() + 4000)]
        all_asins = re.findall(r'/dp/([A-Z0-9]{10})', chunk_before)
        next_asins = re.findall(r'/dp/([A-Z0-9]{10})', chunk_after)
        candidates = (all_asins[-1:] if all_asins else []) + next_asins[:1]
        for asin in candidates:
            if asin in seen:
                continue
            seen.add(asin)
            # Buscar título cerca
            big_chunk = html[max(0, m.start() - 8000) : min(len(html), m.end() + 4000)]
            title_m = re.search(
                rf'/dp/{asin}[^>]*>(?:[^<]|<(?!/?[ai]\b))*?<span[^>]*>([^<]{{15,150}})</span>',
                big_chunk,
                re.DOTALL,
            )
            if not title_m:
                title_m = re.search(
                    r'aria-label="([^"]{15,150})"[^>]*href="/[^"]*' + asin,
                    big_chunk,
                )
            title = title_m.group(1).strip() if title_m else ""
            print(f"  -{d:>3}%  {asin}  {title[:60]}")
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
