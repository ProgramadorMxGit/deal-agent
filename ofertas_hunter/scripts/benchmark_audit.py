#!/usr/bin/env python3
"""benchmark_offer_audit (READ-ONLY): ¿el bot ya vio estas ofertas externas?

NO publica, NO modifica. Busca cada benchmark en products/offers/outbox/
price_observations/frontier/discarded_candidates y reporta su estado.
"""
import json, sqlite3, re, sys
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

BENCHMARKS = [
    {"mkt": "mercadolibre", "asin": None, "title_kw": ["cesto", "bambu", "ag box", "lavanderia", "ropa sucia"],
     "label": "Cesto ropa sucia bambú AG Box", "cur": 406, "prev": 999, "disc": 59,
     "link": "https://meli.la/2PD6zoH"},
    {"mkt": "amazon", "asin": "B09RCQ5NFY", "title_kw": ["cinco by lamex", "olla", "lamex"],
     "label": "Olla Cinco by Lamex 24cm roja", "cur": 624, "prev": 1249, "disc": 50,
     "link": "amazon.com.mx/dp/B09RCQ5NFY"},
    {"mkt": "amazon", "asin": "B0CWLTDQ5K", "title_kw": ["corsair", "rm750x", "fuente"],
     "label": "Corsair RM750x Shift fuente", "cur": 1539.76, "prev": 3364, "disc": 54,
     "link": "amazon.com.mx/dp/B0CWLTDQ5K"},
    {"mkt": "amazon", "asin": "B0FCQG6LNC", "title_kw": ["stanley", "quencher", "reverb"],
     "label": "Stanley Quencher Black Reverb 30oz", "cur": 469, "prev": 1099, "disc": 57,
     "link": "amazon.com.mx/dp/B0FCQG6LNC"},
]


def search(b):
    found = {"products": [], "offers": None, "outbox": [], "price_obs": 0,
             "frontier": 0, "discarded": 0, "visited": 0}
    # products: por asin/marketplace_id o por keywords en título
    if b["asin"]:
        rows = c.execute(
            "SELECT id, title, url_canonical, marketplace_id FROM products "
            "WHERE marketplace_id = ? OR url_canonical LIKE ?",
            (b["asin"], f"%{b['asin']}%")).fetchall()
    else:
        rows = []
    if not rows:
        # buscar por keywords del título
        likeclauses = " OR ".join(["LOWER(title) LIKE ?" for _ in b["title_kw"]])
        params = [f"%{k.lower()}%" for k in b["title_kw"]]
        rows = c.execute(
            f"SELECT id, title, url_canonical, marketplace_id FROM products "
            f"WHERE marketplace=? AND ({likeclauses}) LIMIT 10",
            [b["mkt"]] + params).fetchall()
    for r in rows:
        found["products"].append((r["id"], r["title"][:50], r["marketplace_id"]))
    # frontier (por asin o keyword)
    if b["asin"]:
        fr = c.execute("SELECT COUNT(*) n FROM frontier WHERE url_canonical LIKE ?", (f"%{b['asin']}%",)).fetchone()
        found["frontier"] = fr["n"]
        vr = c.execute("SELECT COUNT(*) n FROM visited_urls WHERE url_canonical LIKE ?", (f"%{b['asin']}%",)).fetchone()
        found["visited"] = vr["n"]
    # offers/outbox/price_obs por product_id
    if found["products"]:
        pids = [p[0] for p in found["products"]]
        ph = ",".join("?" for _ in pids)
        off = c.execute(f"SELECT id, classification, state, discount_percent FROM offers "
                        f"WHERE product_id IN ({ph})", pids).fetchall()
        found["offers"] = [(o["id"], o["classification"], o["state"], o["discount_percent"]) for o in off]
        po = c.execute(f"SELECT COUNT(*) n FROM price_observations WHERE product_id IN ({ph})", pids).fetchone()
        found["price_obs"] = po["n"]
        if off:
            oids = [o["id"] for o in off]
            ph2 = ",".join("?" for _ in oids)
            ob = c.execute(f"SELECT id, state, type FROM outbox WHERE offer_id IN ({ph2})", oids).fetchall()
            found["outbox"] = [(o["id"], o["state"], o["type"]) for o in ob]
    # discarded_candidates por keyword en raw payload
    likeclauses = " OR ".join(["raw_payload_json LIKE ?" for _ in b["title_kw"][:2]])
    params = [f"%{k}%" for k in b["title_kw"][:2]]
    dr = c.execute(f"SELECT reason, COUNT(*) n FROM discarded_candidates "
                   f"WHERE {likeclauses} GROUP BY reason", params).fetchall()
    found["discarded"] = [(d["reason"], d["n"]) for d in dr]
    return found


for b in BENCHMARKS:
    print(f"\n{'='*60}\n{b['label']} ({b['mkt']}, {b['disc']}% {b['cur']}/{b['prev']})")
    print(f"  link: {b['link']}")
    f = search(b)
    print(f"  products encontrados: {len(f['products'])}")
    for p in f["products"][:5]:
        print(f"     product_id={p[0]} mlid/asin={p[2]} | {p[1]}")
    print(f"  offers: {f['offers']}")
    print(f"  outbox: {f['outbox']}")
    print(f"  price_observations: {f['price_obs']}")
    print(f"  frontier(asin): {f['frontier']} | visited(asin): {f['visited']}")
    print(f"  discarded_candidates(kw): {f['discarded']}")
    # veredicto
    if not f["products"] and f["frontier"] == 0 and f["visited"] == 0:
        print("  >>> VEREDICTO: NUNCA VISTO (no en products, no en frontier, no visitado)")
    elif f["products"] and not f["outbox"]:
        print("  >>> VEREDICTO: visto/crawleado pero NO llegó a outbox")
    elif f["outbox"]:
        print("  >>> VEREDICTO: llegó a outbox")
    else:
        print("  >>> VEREDICTO: parcial (revisar)")
