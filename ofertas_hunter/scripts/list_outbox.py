"""Lista las ofertas en outbox / published_messages.

Uso:
    .\.venv\Scripts\python.exe scripts\list_outbox.py
    .\.venv\Scripts\python.exe scripts\list_outbox.py --state pending
    .\.venv\Scripts\python.exe scripts\list_outbox.py --type price_error --limit 50
    .\.venv\Scripts\python.exe scripts\list_outbox.py --published     # ver enviadas
"""

from __future__ import annotations

import argparse
import json
import sys

from ofertas_hunter.db import connect


def _short(s: str | None, n: int = 60) -> str:
    if not s:
        return ""
    return s[: n - 1] + "…" if len(s) > n else s


def _money(v) -> str:
    try:
        x = float(v)
        return f"${x:,.0f}" if x == int(x) else f"${x:,.2f}"
    except Exception:
        return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--state",
        choices=("pending", "sent", "failed", "discarded", "in_flight", "all"),
        default="all",
    )
    ap.add_argument(
        "--type",
        choices=("normal", "price_error", "possible_pe", "all"),
        default="all",
    )
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument(
        "--published",
        action="store_true",
        help="Lista published_messages en lugar de outbox.",
    )
    args = ap.parse_args()

    conn = connect()
    try:
        if args.published:
            sql = (
                "SELECT pm.id, pm.outbox_id, pm.offer_id, pm.sent_at, pm.success, "
                "       pm.message_text "
                "FROM published_messages pm "
                "ORDER BY pm.id DESC LIMIT ?"
            )
            rows = conn.execute(sql, (args.limit,)).fetchall()
            print(f"{'ID':>5} {'OUTBOX':>6} {'OFFER':>5} {'SENT':>1} {'WHEN':<25} TITLE")
            print("-" * 100)
            for r in rows:
                # Extraer título de message_text si está
                title = ""
                txt = r["message_text"] or ""
                first_line = txt.split("\n")[0] if txt else ""
                title = first_line.strip("* ").strip()
                print(
                    f"{r['id']:>5} {r['outbox_id']:>6} {r['offer_id']:>5} "
                    f"{'✓' if r['success'] else '✗':>1} "
                    f"{(r['sent_at'] or '')[:25]:<25} {_short(title, 60)}"
                )
            print(f"\ntotal published: {len(rows)}")
            return 0

        # Outbox
        clauses = []
        params: list = []
        if args.state != "all":
            clauses.append("o.state = ?")
            params.append(args.state)
        if args.type != "all":
            clauses.append("o.type = ?")
            params.append(args.type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = (
            "SELECT o.id, o.offer_id, o.type, o.state, o.attempts, "
            "       o.enqueued_at, o.message_payload_json, "
            "       p.title, p.marketplace, p.url_canonical "
            "FROM outbox o "
            "LEFT JOIN offers ofr ON ofr.id = o.offer_id "
            "LEFT JOIN products p ON p.id = ofr.product_id "
            f"{where} ORDER BY o.id DESC LIMIT ?"
        )
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()

        print(
            f"{'ID':>5} {'TYPE':<13} {'STATE':<10} {'MKT':<14} {'PRICE':>10}  TITLE"
        )
        print("-" * 130)
        for r in rows:
            try:
                payload = json.loads(r["message_payload_json"] or "{}")
            except Exception:
                payload = {}
            title = payload.get("title") or r["title"] or ""
            price = payload.get("current_price")
            disc = payload.get("discount_percent")
            mkt = payload.get("marketplace") or r["marketplace"] or "?"
            extra = ""
            if disc is not None:
                extra = f"  -{disc:.0f}%"
            confidence = payload.get("confidence_label")
            if confidence:
                extra += f"  [{confidence}]"
            print(
                f"{r['id']:>5} {r['type']:<13} {r['state']:<10} "
                f"{mkt[:14]:<14} {_money(price):>10}  {_short(title, 70)}{extra}"
            )

        print(f"\ntotal: {len(rows)}")

        # Snapshot por estado
        agg = conn.execute(
            "SELECT type, state, COUNT(*) AS n FROM outbox "
            "GROUP BY type, state ORDER BY type, state"
        ).fetchall()
        print("\nResumen outbox completo:")
        for r in agg:
            print(f"  {r['type']:<13} {r['state']:<10} {r['n']:>5}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
