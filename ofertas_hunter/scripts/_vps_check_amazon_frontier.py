"""¿Por qué Amazon frontier está vacío?"""
import sqlite3
from datetime import datetime, timedelta, timezone

conn = sqlite3.connect('data/ofertas_hunter.db')
conn.row_factory = sqlite3.Row

print("=== Frontier por marketplace + url_type ===")
for r in conn.execute(
    "SELECT marketplace, url_type, COUNT(*) as n FROM frontier "
    "GROUP BY marketplace, url_type ORDER BY marketplace, n DESC"
):
    print(f"  {r['marketplace']:14s} | {r['url_type']:14s} | {r['n']}")

print()
print("=== Frontier Amazon (top 5 por score) ===")
for r in conn.execute(
    "SELECT id, url_type, score, retries, url_canonical FROM frontier "
    "WHERE marketplace='amazon' ORDER BY score DESC LIMIT 5"
):
    print(f"  id={r['id']} type={r['url_type']} score={r['score']} retries={r['retries']}")
    print(f"     {r['url_canonical'][:120]}")

print()
print("=== Visited Amazon URLs (cuántas se procesaron) ===")
n = conn.execute("SELECT COUNT(*) FROM visited_urls WHERE marketplace='amazon'").fetchone()[0]
print(f"  visited_urls amazon: {n}")
n = conn.execute("SELECT COUNT(*) FROM visited_urls WHERE marketplace='mercadolibre'").fetchone()[0]
print(f"  visited_urls ml:     {n}")

print()
print("=== Eventos discover_seeds Amazon (todos los tiempos, top 5 recientes) ===")
for r in conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_tool_called' "
    "AND payload_json LIKE '%discover_seeds%' AND payload_json LIKE '%amazon%' "
    "AND payload_json LIKE '%phase\":\"after%' "
    "ORDER BY id DESC LIMIT 5"
):
    print(r['created_at'])
    print(' ', r['payload_json'][:500])
    print()

print()
print("=== Eventos hunt_amazon últimos 5 ===")
for r in conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_tool_called' "
    "AND payload_json LIKE '%hunt_amazon%' AND payload_json LIKE '%phase\":\"after%' "
    "ORDER BY id DESC LIMIT 5"
):
    print(r['created_at'])
    print(' ', r['payload_json'][:500])
    print()

print()
print("=== Frontier added_at recent (Amazon) ===")
for r in conn.execute(
    "SELECT id, added_at, url_type, retries, url_canonical FROM frontier "
    "WHERE marketplace='amazon' ORDER BY added_at DESC LIMIT 5"
):
    print(f"  id={r['id']} added_at={r['added_at']} type={r['url_type']} retries={r['retries']}")
    print(f"     {r['url_canonical'][:120]}")
