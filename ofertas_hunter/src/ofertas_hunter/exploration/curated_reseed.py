"""Re-seeding periódico de seeds curadas + priorización de deal pages.

Problema: auto_seed solo siembra cuando el frontier está vacío. El frontier ML
tiene ~24k URLs, así que las seeds curadas (deal pages, /ofertas, búsquedas
≥50%) nunca vuelven a entrar. Además las deal pages cambian durante el día.

Este módulo:
- Reinyecta las seeds curadas cada N minutos con score alto (recurrente).
- Baja el score de URLs viejas del frontier que llevan mucho sin producir
  (decay), para que no monopolicen el crawler.

Todo es aditivo y read-mostly sobre frontier (INSERT OR IGNORE / UPDATE score).
NO toca outbox/published/gates.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

EVENT_RESEED = "curated_seed_reseeded"
EVENT_REBALANCE = "frontier_priority_rebalanced"

# Marcadores de seed "curada" de alto valor (deal pages / búsquedas ≥50%).
_CURATED_MARKERS = (
    "ofertas/", "Descuento_50-100", "deals-collection",
    "pct-off-with-tax", "/deals?", "goldbox",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_curated_seed(url: str) -> bool:
    return any(m in url for m in _CURATED_MARKERS)


def load_curated_seeds(repo_root: Path, marketplaces) -> list[str]:
    """Lee las seeds curadas de config/seeds/<mkt>.json."""
    out: list[str] = []
    for mkt in marketplaces:
        path = repo_root / "config" / "seeds" / f"{mkt}.json"
        if not path.exists():
            continue
        try:
            urls = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("curated_reseed: error leyendo %s", path)
            continue
        out.extend(u for u in urls if isinstance(u, str) and is_curated_seed(u))
    return out


def reseed_curated(
    db,
    *,
    repo_root: Path,
    marketplaces,
    min_score: float = 20.0,
    every_minutes: int = 60,
    max_per_cycle: int = 100,
    emit_event: bool = True,
) -> dict:
    """Reinyecta seeds curadas al frontier si pasó `every_minutes` desde la última.

    Sube el score de las que ya existan (a min_score) y agrega las que falten.
    Respeta visited_urls (no re-mete las recién visitadas salvo decaídas).
    """
    # rate-limit por evento previo
    try:
        row = db.execute(
            "SELECT created_at FROM runtime_events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (EVENT_RESEED,),
        ).fetchone()
        if row is not None:
            last = row["created_at"] if hasattr(row, "keys") else row[0]
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if (_now() - last_dt).total_seconds() < every_minutes * 60:
                    return {"reseeded": 0, "skipped": "rate_limited"}
            except Exception:
                pass
    except Exception:
        logger.exception("curated_reseed: check last event falló")

    seeds = load_curated_seeds(repo_root, marketplaces)[:max_per_cycle]
    if not seeds:
        return {"reseeded": 0, "skipped": "no_curated_seeds"}

    from .url_classifier import classify

    added = 0
    bumped = 0
    now_iso = _iso(_now())
    for url in seeds:
        ci = classify(url)
        if ci.kind in ("unknown", ""):
            continue
        # ¿ya está en frontier? -> subir score
        existing = db.execute(
            "SELECT id, score FROM frontier WHERE url_canonical=?", (ci.url,)
        ).fetchone()
        if existing is not None:
            cur_score = existing["score"] if hasattr(existing, "keys") else existing[1]
            if cur_score < min_score:
                db.execute(
                    "UPDATE frontier SET score=? WHERE url_canonical=?",
                    (min_score, ci.url),
                )
                bumped += 1
            continue
        # no está -> insertar (aunque haya sido visitada: las deal pages cambian)
        try:
            db.execute(
                "INSERT OR IGNORE INTO frontier "
                "(marketplace, url_canonical, url_type, score, added_at, retries) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (ci.marketplace, ci.url, ci.kind, min_score, now_iso),
            )
            added += 1
        except Exception:
            logger.exception("curated_reseed: insert falló %s", url)

    if emit_event and (added or bumped):
        _emit(db, EVENT_RESEED, {
            "marketplaces": list(marketplaces),
            "added": added, "bumped": bumped, "total_curated": len(seeds),
            "new_score": min_score, "reason": "periodic_reseed",
        })
    try:
        db.commit()
    except Exception:
        pass
    return {"reseeded": added, "bumped": bumped, "total_curated": len(seeds)}


def decay_old_backlog(
    db,
    *,
    decay_hours: int = 24,
    decay_factor: float = 0.25,
    max_rows: int = 2000,
    emit_event: bool = True,
) -> dict:
    """Baja el score de URLs viejas del frontier (añadidas hace > decay_hours)
    que NO son curadas, para que no monopolicen el crawler frente a deal pages.
    """
    cutoff = _iso(datetime.fromtimestamp(_now().timestamp() - decay_hours * 3600, tz=timezone.utc))
    try:
        # solo decae las de score "normal" (>0) y no curadas (heurística: no /ofertas etc.)
        cur = db.execute(
            "UPDATE frontier SET score = score * ? "
            "WHERE added_at < ? AND score > 0.5 "
            "AND url_canonical NOT LIKE '%ofertas/%' "
            "AND url_canonical NOT LIKE '%Descuento_50-100%' "
            "AND url_canonical NOT LIKE '%deals-collection%' "
            "AND url_canonical NOT LIKE '%pct-off%' "
            "AND id IN (SELECT id FROM frontier WHERE added_at < ? AND score > 0.5 LIMIT ?)",
            (decay_factor, cutoff, cutoff, max_rows),
        )
        n = cur.rowcount
        db.commit()
    except Exception:
        logger.exception("decay_old_backlog falló")
        return {"decayed": 0}
    if emit_event and n:
        _emit(db, EVENT_REBALANCE, {
            "decayed_urls": n, "decay_hours": decay_hours,
            "decay_factor": decay_factor, "reason": "old_backlog_decay",
        })
    return {"decayed": n}


def _emit(db, kind: str, payload: dict) -> None:
    try:
        db.execute(
            "INSERT INTO runtime_events (kind, severity, payload_json, created_at) VALUES (?,?,?,?)",
            (kind, "info", json.dumps(payload, ensure_ascii=False), _iso(_now())),
        )
    except Exception:
        logger.exception("curated_reseed: emit %s falló", kind)


__all__ = [
    "is_curated_seed", "load_curated_seeds", "reseed_curated",
    "decay_old_backlog", "EVENT_RESEED", "EVENT_REBALANCE",
]
