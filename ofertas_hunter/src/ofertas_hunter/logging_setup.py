"""Configuración de logging estructurado."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configura un logger raíz con formato consistente.

    Output:
        - stdout siempre.
        - archivo opcional si `log_file` se pasa.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = []

    # En Windows stdout suele ser cp1252; los emojis del template (🔥, 🚨, ❌, ✅, ⚡)
    # rompen el handler. Forzamos UTF-8 cuando es posible.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    handlers.append(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        handlers.append(file_handler)

    logging.basicConfig(level=log_level, handlers=handlers, force=True)
    # Silenciar libs ruidosas
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
