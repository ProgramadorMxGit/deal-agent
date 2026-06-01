import sqlite3, json, re
from collections import Counter

DB = '/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db'
db = sqlite3.connect(DB)

UNIT_RE = re.compile(r"(/|\bpor\b)\s*(unidad|pieza|pza|count|each|ppu|recuento)", re.IGNORECASE)
DIRTY_TITLE_RE = re.compile(r"guante|nitrilo|vinil|l[aá]tex|desechable|pieza|pack\s*de\s*\d", re.IGNORECASE)


def is_amz(p):
    return (p.get('marketplace') or '').lower() == 'amazon'


def vaff(u):
    return bool(u) and ('amzn.to/' in u or 'tag=' in u)


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


rows = db.execute("SELECT type,message_payload_json FROM outbox WHERE state='pending'").fetchall()
total = aff_ok = oldver = curver = lt10 = disc90 = ratio50 = unit = extreme_unver = tg = pub = 0
for typ, pj in rows:
    try:
        p = json.loads(pj)
    except Exception:
        continue
    if not is_amz(p):
        continue
    total += 1
    cur = fnum(p.get('current_price'))
    prev = fnum(p.get('previous_price'))
    disc = fnum(p.get('discount_percent'))
    a = vaff(p.get('affiliate_url'))
    ov = bool(p.get('old_price_verified'))
    cv = bool(p.get('current_price_verified'))
    if a:
        aff_ok += 1
    if ov:
        oldver += 1
    if cv:
        curver += 1
    if cur is not None and cur < 10:
        lt10 += 1
    if disc is not None and disc >= 90:
        disc90 += 1
    if cur and prev and cur > 0 and prev / cur > 50:
        ratio50 += 1
    if p.get('current_price_is_unit_price') or UNIT_RE.search(str(p.get('current_price_raw_text') or '')):
        unit += 1
    if not p.get('extreme_discount_verified'):
        extreme_unver += 1
    if (p.get('source') == 'telegram'):
        tg += 1
    # publicable bajo reglas nuevas
    if a and (typ != 'normal' or (ov and cv)):
        pub += 1

print("=== AUDITORÍA ANTES — Amazon pending ===")
print(f"  total Amazon pending:              {total}")
print(f"  con afiliado válido:               {aff_ok}")
print(f"  old_price_verified=true:           {oldver}")
print(f"  current_price_verified=true:       {curver}")
print(f"  current_price < 10:                {lt10}")
print(f"  discount_percent >= 90:            {disc90}")
print(f"  old/current > 50:                  {ratio50}")
print(f"  current_price_is_unit_price=true:  {unit}")
print(f"  extreme_discount_verified=false:   {extreme_unver}")
print(f"  Telegram Amazon:                   {tg}")
print(f"  PUBLICABLES bajo reglas nuevas:    {pub}")

db.close()
