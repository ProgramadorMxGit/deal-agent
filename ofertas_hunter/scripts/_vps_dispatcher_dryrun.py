"""Smoke test: imports OK, factory construye curator, settings cargan correctos."""
import sys
sys.path.insert(0, 'src')

from ofertas_hunter.config import get_settings
from ofertas_hunter.db import connect
from ofertas_hunter.dispatching.curator_factory import build_diversity_curator
from ofertas_hunter.dispatching.dispatcher import OutboxDispatcher

s = get_settings()
print(f"DIVERSITY_CURATOR_ENABLED={s.diversity_curator_enabled}")
print(f"DIVERSITY_CURATOR_USE_LLM={s.diversity_curator_use_llm}")
print(f"PUBLISHING_ENABLED={s.publishing_enabled}")
print(f"PUBLISHING_DRY_RUN={s.publishing_dry_run}")
print()

db = connect()
curator = build_diversity_curator(db, s)
print(f"build_diversity_curator -> {type(curator).__name__ if curator else 'None'}")
if curator is None:
    print("FAIL: curator should be built when DIVERSITY_CURATOR_ENABLED=true")
    sys.exit(1)

print(f"  scorer.history_size  = {curator.scorer.history_size}")
print(f"  scorer.top_n         = {curator.scorer.top_n}")
print(f"  llm_client           = {type(curator.llm_client).__name__ if curator.llm_client else 'None'}")
if curator.llm_client:
    print(f"  llm.binary_path      = {curator.llm_client.config.binary_path}")
    print(f"  llm.timeout_seconds  = {curator.llm_client.config.timeout_seconds}")

print()
print("OK: curator se construye correctamente.")
print("    Dispatcher recibira `curator.pick` como item_selector cuando inicies el bot.")
