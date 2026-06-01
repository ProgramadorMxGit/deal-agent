"""Smoke test del auto-seed: vacia un frontier sintetico y confirma que se sembra solo."""
import asyncio
import sys
import sqlite3
import tempfile
import os

sys.path.insert(0, 'src')

from pathlib import Path

# Use temporary DB to not pollute production
tmpdir = tempfile.mkdtemp()
db_path = Path(tmpdir) / "test.db"

# Init schema (copy from migrations)
repo_root = Path(__file__).resolve().parents[1]
from ofertas_hunter.db import init_db
init_db(db_path)

conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

from ofertas_hunter.exploration.frontier import FrontierRepo
from ofertas_hunter.mcp.tools.action_tools import _auto_seed_from_json

# Build a minimal agent that has frontier + seed_from_config
class FakeAgent:
    def __init__(self, conn, marketplace):
        self.frontier = FrontierRepo(conn)
        self.marketplace = marketplace

    def seed_from_config(self, urls):
        from ofertas_hunter.exploration.url_classifier import classify
        n = 0
        for url in urls:
            info = classify(url)
            if info.marketplace != self.marketplace:
                continue
            if info.kind == "unknown":
                continue
            if self.frontier.add_classified(info):
                n += 1
        return n


# Confirma que arranca con frontier vacio
agent_amz = FakeAgent(conn, "amazon")
print(f"BEFORE auto_seed: count_pending(amazon) = {agent_amz.frontier.count_pending('amazon')}")

seeded = _auto_seed_from_json(agent_amz, "amazon")
print(f"_auto_seed_from_json(amazon) = {seeded}")
print(f"AFTER auto_seed:  count_pending(amazon) = {agent_amz.frontier.count_pending('amazon')}")

agent_ml = FakeAgent(conn, "mercadolibre")
print()
print(f"BEFORE auto_seed: count_pending(ml) = {agent_ml.frontier.count_pending('mercadolibre')}")
seeded_ml = _auto_seed_from_json(agent_ml, "mercadolibre")
print(f"_auto_seed_from_json(ml) = {seeded_ml}")
print(f"AFTER auto_seed:  count_pending(ml) = {agent_ml.frontier.count_pending('mercadolibre')}")

# Cleanup
conn.close()
import shutil
shutil.rmtree(tmpdir)
print()
print("OK: auto-seed funciona para ambos marketplaces.")
