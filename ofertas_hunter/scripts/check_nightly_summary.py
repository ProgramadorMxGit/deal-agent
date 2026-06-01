#!/usr/bin/env python3
"""Audita el último nightly_maintenance_summary. Read-only.

Confirma que el mantenimiento nocturno dejó el servicio arriba y sano:
  success, service_initial_state, service_stop_result, vacuum_done,
  integrity_check, service_start_result, service_final_state,
  reset_failed_done, start_retried, errors.

Uso: .venv/bin/python scripts/check_nightly_summary.py
"""
import sqlite3, json, sys

DB = "data/ofertas_hunter.db"

def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    row = c.execute(
        "SELECT created_at, payload_json FROM runtime_events "
        "WHERE kind='nightly_maintenance_summary' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        print("No hay nightly_maintenance_summary todavía.")
        return
    p = json.loads(row[1])
    print(f"last_maintenance: {row[0]}")
    for k in ("success", "service_initial_state", "service_stop_result",
              "vacuum_done", "integrity_check", "service_start_result",
              "service_final_state", "reset_failed_done", "start_retried",
              "rows_deleted", "errors", "warnings"):
        print(f"  {k:24} = {p.get(k)}")
    # Veredicto claro
    ok = p.get("success") and p.get("service_final_state") == "active" and not p.get("errors")
    print("\nVEREDICTO:", "OK — bot arriba y sano" if ok else "REVISAR — ver errors/service_final_state")
    # Eventos granulares de la última corrida
    print("\nEventos granulares recientes:")
    for r in c.execute(
        "SELECT kind, COUNT(*) FROM runtime_events WHERE kind LIKE 'nightly_service%' "
        "AND created_at >= datetime('now','-1 day') GROUP BY kind ORDER BY kind"
    ):
        print(f"  {r[0]:38} {r[1]}")
    c.close()

if __name__ == "__main__":
    main()
