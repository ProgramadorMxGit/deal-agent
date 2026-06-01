#!/usr/bin/env python3
"""Audita dom_snapshots de las deal pages para ver qué DOM devolvieron (READ-ONLY)."""
import sqlite3
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

print("=== dom_snapshots por context/reason ===")
for r in c.execute("SELECT context, reason, COUNT(*) n FROM dom_snapshots GROUP BY context, reason ORDER BY n DESC LIMIT 15"):
    print(f"  {r['n']:>4}  ctx={r['context']} reason={r['reason']}")

print("\n=== snapshots de URLs de ofertas/deals (las nuevas seeds) ===")
rows = c.execute(
    "SELECT id, marketplace, context, url, reason, LENGTH(content) clen, captured_at "
    "FROM dom_snapshots WHERE url LIKE '%ofertas/%' OR url LIKE '%/deals%' OR url LIKE '%Descuento_50%' "
    "OR url LIKE '%pct-off%' ORDER BY id DESC LIMIT 12"
).fetchall()
print(f"  encontrados: {len(rows)}")
for r in rows:
    print(f"  id={r['id']} [{r['marketplace']}] reason={r['reason']} len={r['clen']} {r['url'][:60]}")

# si hay alguno, muestrear clases de card en el HTML
if rows:
    sample = c.execute("SELECT content FROM dom_snapshots WHERE id=?", (rows[0]["id"],)).fetchone()
    html = sample["content"] or ""
    print(f"\n=== muestra HTML del snapshot id={rows[0]['id']} (len={len(html)}) ===")
    import re
    # contar selectores de card conocidos
    for sel in ["poly-card", "ui-search-layout__item", "ui-search-result__wrapper",
                "andes-card", "ui-search-link", "poly-component__title",
                "/p/MLM", "/MLM", "promotion-item", "ui-recommendations",
                "andes-money-amount", "data-component-type", "s-search-result", "/dp/"]:
        print(f"  '{sel}': {html.count(sel)}")
    # primeras clases de <a> con MLM o dp
    links = re.findall(r'href="([^"]*(?:/p/MLM|/MLM\d|/dp/)[^"]*)"', html)
    print(f"  product-like links en HTML: {len(links)}")
    for l in links[:5]:
        print("    ", l[:80])
