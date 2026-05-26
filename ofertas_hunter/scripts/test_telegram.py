#!/usr/bin/env python
"""Prueba manual del listener de Telegram.

Modos:

    # Sólo verifica config y lista canales esperados (sin tocar Telegram).
    python scripts/test_telegram.py --check-config

    # Parsea un fixture local y muestra el candidate generado.
    python scripts/test_telegram.py --parse iphone_16_pro_max_liverpool_3899

    # Conecta a Telegram (TELEGRAM_ENABLED=true) y hace un backfill corto.
    python scripts/test_telegram.py --backfill --limit 20

    # Conecta y escucha hasta capturar 1 mensaje en vivo (--once).
    python scripts/test_telegram.py --listen --once

Si TELEGRAM_ENABLED=false, los modos --backfill / --listen no se conectan.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    parser = argparse.ArgumentParser(description="Prueba manual del listener Telegram")
    sub = parser.add_subparsers(dest="mode", required=True)

    sub.add_parser("--check-config", help="Verifica config sin tocar Telegram.").set_defaults(
        mode="check"
    )

    p_parse = sub.add_parser("parse", help="Parsea un fixture local.")
    p_parse.add_argument("fixture")
    p_parse.add_argument("--image-path", default=None)

    p_back = sub.add_parser("backfill", help="Conecta y hace backfill.")
    p_back.add_argument("--limit", type=int, default=20)

    p_listen = sub.add_parser("listen", help="Conecta y escucha en vivo.")
    p_listen.add_argument("--once", action="store_true")

    args = parser.parse_args()

    from ofertas_hunter.__main__ import (
        cmd_telegram_check_config,
        cmd_telegram_parse_sample,
        cmd_telegram_listen,
    )
    from ofertas_hunter.config import get_settings
    from ofertas_hunter.logging_setup import configure_logging

    configure_logging(get_settings().log_level)

    if args.mode == "check":
        return cmd_telegram_check_config(args)
    if args.mode == "parse":
        ns = argparse.Namespace(path=args.fixture, image_path=args.image_path)
        return cmd_telegram_parse_sample(ns)
    if args.mode == "backfill":
        ns = argparse.Namespace(command="telegram-backfill", once=True, limit=args.limit)
        return cmd_telegram_listen(ns)
    if args.mode == "listen":
        ns = argparse.Namespace(command="telegram-listen", once=args.once, limit=None)
        return cmd_telegram_listen(ns)
    return 1


if __name__ == "__main__":
    sys.exit(main())
