"""Auditoría retrospectiva de eventos/snapshots Amazon catalogados como captcha.

Implementa el comando:

    python -m ofertas_hunter audit-amazon-captcha --recent --fix

Pasos:

1. Lee `runtime_events` con `kind` ∈ {`captcha`, `amazon_captcha_confirmed`,
   `amazon_suspected_false_captcha`} de los últimos N días.
2. Lee `dom_snapshots` con `marketplace='amazon'` y `reason='fetch_failed'`.
3. Para cada snapshot disponible, ejecuta `AmazonCaptchaDetector` con el
   HTML guardado.
4. Si un evento `amazon_captcha_confirmed` corresponde a un HTML cuyo
   detector NUEVO ya no lo clasifica como `confidence=high`, registra un
   `runtime_event(kind='amazon_captcha_false_positive_reclassified')` con
   detalles.
5. Si `--fix`, además limpia los `discarded_candidates` con
   `reason='captcha_detected'` cuyo snapshot ya no es captcha (los
   reclasifica a `amazon_extraction_failed`).

No toca Mercado Libre.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .amazon_captcha_detector import AmazonCaptchaDetector


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


@dataclass
class CaptchaAuditFinding:
    snapshot_id: int
    url: str
    captured_at: str
    new_confidence: str
    new_strong_signals: tuple[str, ...]
    new_weak_signals: tuple[str, ...]
    was_marked_captcha: bool
    is_real_captcha_now: bool
    action: str  # "kept_real" | "reclassified_false_positive" | "no_change"
    reason: str


@dataclass
class CaptchaAuditReport:
    scanned_snapshots: int = 0
    real_captcha: int = 0
    reclassified_false_positives: int = 0
    findings: list[CaptchaAuditFinding] = field(default_factory=list)
    discarded_reclassified: int = 0
    runtime_events_emitted: int = 0


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def run_audit(
    conn: sqlite3.Connection,
    *,
    fix: bool = False,
    days: int = 7,
    limit: Optional[int] = None,
) -> CaptchaAuditReport:
    """Audita snapshots Amazon catalogados como captcha y reclasifica falsos
    positivos según el nuevo `AmazonCaptchaDetector`.
    """
    detector = AmazonCaptchaDetector()
    report = CaptchaAuditReport()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, days))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # Recogemos snapshots Amazon con razón fetch_failed (los que el agent
    # crea cuando detecta captcha, según el código actual).
    sql = (
        "SELECT id, url, captured_at, content "
        "FROM dom_snapshots "
        "WHERE marketplace='amazon' AND reason='fetch_failed' "
        "AND captured_at >= ? "
        "ORDER BY id DESC"
    )
    params: list = [cutoff]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    report.scanned_snapshots = len(rows)

    for row in rows:
        html = row["content"] or ""
        # Asegurar URL canonical para el detector.
        result = detector.assess(html=html, final_url=row["url"], status=200)
        if result.confidence == "high":
            report.real_captcha += 1
            finding = CaptchaAuditFinding(
                snapshot_id=row["id"],
                url=row["url"],
                captured_at=row["captured_at"],
                new_confidence=result.confidence,
                new_strong_signals=result.strong_signals,
                new_weak_signals=result.weak_signals,
                was_marked_captcha=True,
                is_real_captcha_now=True,
                action="kept_real",
                reason="real_captcha_high_confidence",
            )
        else:
            report.reclassified_false_positives += 1
            finding = CaptchaAuditFinding(
                snapshot_id=row["id"],
                url=row["url"],
                captured_at=row["captured_at"],
                new_confidence=result.confidence,
                new_strong_signals=result.strong_signals,
                new_weak_signals=result.weak_signals,
                was_marked_captcha=True,
                is_real_captcha_now=False,
                action="reclassified_false_positive",
                reason=(
                    "previously_marked_captcha_but_no_strong_signals_now"
                    if not result.strong_signals
                    else "previously_marked_captcha_but_low_confidence_now"
                ),
            )
            if fix:
                _reclassify_discarded(conn, snapshot_url=row["url"])
                report.discarded_reclassified += 1

        # Aprendizaje: emitimos runtime_event en cualquier caso.
        try:
            kind = (
                "amazon_captcha_audit_kept"
                if finding.is_real_captcha_now
                else "amazon_captcha_false_positive_reclassified"
            )
            conn.execute(
                "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    kind,
                    "info" if finding.is_real_captcha_now else "warning",
                    json.dumps(
                        {
                            "snapshot_id": finding.snapshot_id,
                            "url": finding.url,
                            "captured_at": finding.captured_at,
                            "new_confidence": finding.new_confidence,
                            "new_strong_signals": list(finding.new_strong_signals),
                            "new_weak_signals": list(finding.new_weak_signals),
                            "action": finding.action,
                            "reason": finding.reason,
                        },
                        ensure_ascii=False,
                    ),
                    _now_iso(),
                ),
            )
            report.runtime_events_emitted += 1
            if fix:
                conn.commit()
        except Exception as exc:  # pragma: no cover
            logger.warning("audit emit_runtime_event failed: %s", exc)
        report.findings.append(finding)

    return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _reclassify_discarded(conn: sqlite3.Connection, *, snapshot_url: str) -> None:
    """Reclasifica `discarded_candidates` con razón `captcha_detected` cuya
    URL coincide con la del snapshot.

    No borra el registro: actualiza la razón y deja un breadcrumb en
    `raw_payload_json`.
    """
    rows = conn.execute(
        "SELECT id, raw_payload_json FROM discarded_candidates "
        "WHERE source='amazon_hunter' AND reason='captcha_detected' "
        "AND raw_payload_json LIKE ?",
        (f'%{snapshot_url}%',),
    ).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["raw_payload_json"] or "{}")
        except Exception:
            payload = {}
        payload["audit_reclassification"] = {
            "from": "captcha_detected",
            "to": "amazon_possible_block_low_confidence",
            "at": _now_iso(),
        }
        conn.execute(
            "UPDATE discarded_candidates SET reason=?, raw_payload_json=? WHERE id=?",
            (
                "amazon_possible_block_low_confidence",
                json.dumps(payload, ensure_ascii=False),
                r["id"],
            ),
        )


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CaptchaAuditFinding", "CaptchaAuditReport", "run_audit"]
