"""Diagnóstico rápido: por qué ML está pausado."""
import sqlite3, json

conn = sqlite3.connect('data/ofertas_hunter.db')
conn.row_factory = sqlite3.Row

print('=== ÚLTIMOS marketplace_paused (ML) ===')
for r in conn.execute(
    "SELECT created_at, severity, payload_json FROM runtime_events "
    "WHERE kind='marketplace_paused' AND payload_json LIKE '%mercadolibre%' "
    "ORDER BY id DESC LIMIT 5"
):
    p = json.loads(r['payload_json'])
    print(r['created_at'], '|', p.get('reason'), '| ttl=', p.get('ttl_seconds'))

print()
print('=== ÚLTIMOS eventos ML (cualquier kind) ===')
for r in conn.execute(
    "SELECT created_at, kind, severity, payload_json FROM runtime_events "
    "WHERE kind LIKE '%ml%' OR kind LIKE 'mercadolibre%' OR payload_json LIKE '%mercadolibre%' "
    "ORDER BY id DESC LIMIT 12"
):
    print(r['created_at'], '|', r['kind'], '|', r['severity'])
    print('    ', r['payload_json'][:240])

print()
print('=== marketplace_pauses table ===')
try:
    for r in conn.execute(
        "SELECT * FROM marketplace_pauses WHERE marketplace='mercadolibre' "
        "ORDER BY id DESC LIMIT 5"
    ):
        print(dict(r))
except sqlite3.OperationalError as e:
    print('table missing:', e)
    print('available tables:')
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print('   ', r[0])

print()
print('=== ML session state ===')
try:
    for r in conn.execute(
        "SELECT * FROM ml_session_state ORDER BY id DESC LIMIT 3"
    ):
        print(dict(r))
except sqlite3.OperationalError:
    pass
