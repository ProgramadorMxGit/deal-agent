#!/usr/bin/env python3
"""Validación discovery: scroll aplicado + conversión. Read-only excepto emit
de discovery_conversion_summary (que es seguro: solo runtime_events)."""
import sqlite3, json, sys, datetime
sys.path.insert(0, "src")

DB = "data/ofertas_hunter.db"

def conn(write=False):
    if write:
        c = sqlite3.connect(DB, timeout=60)
    else:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    c.execute("PRAGMA busy_timeout=60000"); c.row_factory = sqlite3.Row
    return c

def sec(t): print("\n"+"="*68+"\n"+t+"\n"+"="*68)

def main():
    emit = "--emit" in sys.argv
    c = conn(write=False)
    now = datetime.datetime.now(datetime.timezone.utc)
    def ago(m): return (now-datetime.timedelta(minutes=m)).isoformat(timespec="milliseconds").replace("+00:00","Z")

    sec("1. deal_page_scroll_applied (ult 30 min) por marketplace")
    rows = c.execute(
        "SELECT json_extract(payload_json,'$.marketplace') mp, COUNT(*) n, "
        "AVG(json_extract(payload_json,'$.html_len')) avg_html "
        "FROM runtime_events WHERE kind='deal_page_scroll_applied' AND created_at>=? "
        "GROUP BY mp", (ago(30),)
    ).fetchall()
    if not rows: print("  (sin eventos de scroll todavia)")
    for r in rows:
        print(f"  {r['mp']:14} scrolls={r['n']:4} avg_html_len={int(r['avg_html'] or 0)}")

    sec("2. Ultimos 5 deal_page_scroll_applied")
    rows = c.execute(
        "SELECT created_at, payload_json FROM runtime_events WHERE kind='deal_page_scroll_applied' "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for r in rows:
        p=json.loads(r["payload_json"])
        print(f"  [{r['created_at'][:19]}] {p.get('marketplace')} ok={p.get('ok')} "
              f"html_len={p.get('html_len')} url={str(p.get('url'))[:48]}")

    sec("3. Productos nuevos por marketplace (ult 30 min / 1h)")
    for label,m in (("30min",30),("1h",60)):
        rows=c.execute("SELECT marketplace,COUNT(*) n FROM products WHERE first_seen_at>=? GROUP BY marketplace",(ago(m),)).fetchall()
        d={r['marketplace']:r['n'] for r in rows}
        print(f"  {label}: {d}")

    c.close()

    if emit:
        sec("4. Emitiendo discovery_conversion_summary (window 60 min)")
        from ofertas_hunter.exploration.conversion_metrics import emit_conversion_summary
        cw = conn(write=True)
        out = emit_conversion_summary(cw, window_minutes=60)
        cw.close()
        for s in out:
            print(f"  {s['marketplace']}: deal_pages={s['deal_pages_visited']} "
                  f"prod_urls={s['product_urls_extracted']} pdp={s['pdp_visited']} "
                  f"offers={s['offers_created']} disc50+={s['discount_50_plus']} "
                  f"verified_prev={s['verified_previous_price']} pending={s['pending_inserted']} "
                  f"deferred={s['deferred']} sent={s['sent']} discarded={s['discarded']}")

if __name__ == "__main__":
    main()
