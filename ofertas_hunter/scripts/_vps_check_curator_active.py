"""Verifica que el curator se construye correctamente con los settings del .env."""
import sys
sys.path.insert(0, 'src')

from ofertas_hunter.config import get_settings
from ofertas_hunter.dispatching.curator_factory import build_diversity_curator
from ofertas_hunter.db import connect

s = get_settings()
print("=== Settings DIVERSITY_CURATOR ===")
print(f"  enabled         : {s.diversity_curator_enabled}")
print(f"  use_llm         : {s.diversity_curator_use_llm}")
print(f"  history_size    : {s.diversity_curator_history_size}")
print(f"  candidate_limit : {s.diversity_curator_candidate_limit}")
print(f"  llm_timeout_s   : {s.diversity_curator_llm_timeout_seconds}")
print(f"  kiro_cli_path   : {s.diversity_curator_kiro_cli_path}")
print()

print("=== build_diversity_curator(db, settings) ===")
conn = connect()
curator = build_diversity_curator(conn, s)
if curator is None:
    print("  Resultado: None (feature deshabilitada o error)")
else:
    print(f"  Resultado: DiversityCurator instanciado")
    print(f"  scorer.history_size  : {curator.scorer.history_size}")
    print(f"  scorer.top_n         : {curator.scorer.top_n}")
    print(f"  llm_client           : {type(curator.llm_client).__name__ if curator.llm_client else 'None'}")
    if curator.llm_client is not None:
        print(f"  llm_client.binary    : {curator.llm_client.config.binary_path}")
        print(f"  llm_client.timeout   : {curator.llm_client.config.timeout_seconds}")
        print(f"  llm_client.classic   : {curator.llm_client.config.classic_mode}")
        print(f"  history_size         : {curator.history_size}")
        print(f"  candidate_limit      : {curator.candidate_limit}")
print()

# Test rápido: invocar al curator con una historia simulada para ver que el LLM responde.
print("=== Smoke test: DiversityScorer con datos sintéticos ===")
from ofertas_hunter.dispatching.diversity_scorer import HistoryEntry, price_bucket
from ofertas_hunter.models import OutboxItem, OutboxState, OutboxType
from datetime import datetime, timezone

def _item(idx, cat, mkt="amazon"):
    return OutboxItem(
        id=idx, offer_id=idx, type=OutboxType.NORMAL.value,
        message_payload={"title": f"Item {idx}", "category": cat,
                         "marketplace": mkt, "current_price": 800.0,
                         "discount_percent": 60.0, "brand": f"brand{idx}"},
        enqueued_at=datetime.now(timezone.utc), attempts=0,
        state=OutboxState.PENDING.value,
    )

items = [_item(1, "mascotas"), _item(2, "electronica"), _item(3, "hogar")]
hist = [
    HistoryEntry(marketplace="mercadolibre", category="mascotas", brand="brandA",
                 price_bucket="mid", sent_at=datetime.now(timezone.utc)),
    HistoryEntry(marketplace="mercadolibre", category="mascotas", brand="brandB",
                 price_bucket="mid", sent_at=datetime.now(timezone.utc)),
]
scored = curator.scorer.rank(items, hist) if curator else []
for sc in scored:
    print(f"  id={sc.item.id} cat={sc.item.message_payload['category']:12s} score={sc.score:.4f} bd={sc.breakdown}")
