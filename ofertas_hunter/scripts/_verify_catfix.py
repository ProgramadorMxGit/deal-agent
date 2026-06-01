#!/usr/bin/env python3
"""Read-only: verificacion post-fix de category_normalized.
NO modifica nada."""
import sqlite3, json, collections, sys
sys.path.insert(0, "src")
from ofertas_hunter.dispatching.diversity_metadata import infer_offer_category

DB = "data/ofertas_hunter.db"

def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    c.execute("PRAGMA busy_timeout=60000"); c.row_factory = sqlite3.Row
    return c

def sec(t): print("\n"+"="*70+"\n"+t+"\n"+"="*70)

def main():
    c = conn()

    sec("1. PENDING/DEFERRED: cobertura de los 6 campos en payload almacenado")
    fields = ("category_raw","category_normalized","category_source",
              "category_confidence","brand_normalized","product_family")
    for state in ("pending","deferred"):
        rows = c.execute("SELECT message_payload_json FROM outbox WHERE state=?",(state,)).fetchall()
        cov = {f:0 for f in fields}
        catc = collections.Counter()
        n=0
        for r in rows:
            try: p=json.loads(r["message_payload_json"])
            except Exception: continue
            n+=1
            for f in fields:
                if f in p: cov[f]+=1
            catc[str(p.get("category_normalized"))]+=1
        print(f"\n--- {state} (n={n}) ---")
        for f in fields:
            print(f"   {f:24} {cov[f]}/{n}")
        print(f"   categorias: {dict(catc)}")

    sec("2. 'otro' + basura residual en pending/deferred (objetivo: minimo)")
    rows = c.execute("SELECT state, message_payload_json FROM outbox WHERE state IN ('pending','deferred')").fetchall()
    otro=0; total=0
    for r in rows:
        try: p=json.loads(r["message_payload_json"])
        except Exception: continue
        total+=1
        if p.get("category_normalized")=="otro": otro+=1
    print(f"   otro: {otro}/{total}")

    sec("3. Ultimo pool_deficit_plan (categorias reales que ve el planner)")
    row = c.execute("SELECT created_at,payload_json FROM runtime_events WHERE kind='pool_deficit_plan' ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        p=json.loads(row["payload_json"])
        print(f"   [{row['created_at']}] pending_total={p.get('pending_total')}")
        print(f"   category_counts={p.get('category_counts')}")
        print(f"   saturated={p.get('saturated_categories')}  deficit={p.get('deficit_categories')}")

    sec("4. Items recien encolados (id alto) - traen los 6 campos?")
    rows = c.execute("SELECT id,state,enqueued_at,message_payload_json FROM outbox ORDER BY id DESC LIMIT 8").fetchall()
    for r in rows:
        try: p=json.loads(r["message_payload_json"])
        except Exception: continue
        has6 = all(f in p for f in fields)
        print(f"   id={r['id']} state={r['state']:10} 6campos={has6} "
              f"cat={str(p.get('category_normalized')):20} src={p.get('category_source')} "
              f"enq={str(r['enqueued_at'])[:19]}")

    c.close()

if __name__ == "__main__":
    main()
