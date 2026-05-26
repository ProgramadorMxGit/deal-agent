"""Acceso a SQLite con WAL y helpers básicos.

Para Fase 2 usamos `sqlite3` síncrono envuelto en helpers. No usamos
`aiosqlite` aún para mantener simple las pruebas y evitar que un único
threadpool sature. Si hace falta async, se introducirá un pool en Fase 3.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=5000")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Abre una conexión SQLite con pragmas correctos.

    El llamador es responsable de cerrarla, o usar `connection()` como
    context manager.
    """
    target = db_path or get_settings().db_path_resolved
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(target),
        timeout=10.0,
        isolation_level=None,  # autocommit; transacciones explícitas
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


@contextmanager
def connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager que abre/cierra conexión y maneja transacción.

    Uso:

        with connection() as conn:
            conn.execute("...")
    """
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> Path:
    """Crea (o actualiza idempotentemente) el esquema desde `migrations/001_init.sql`.

    Devuelve la ruta del .db final.
    """
    target = (db_path or get_settings().db_path_resolved).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        raise FileNotFoundError(f"No se encontraron migraciones en {MIGRATIONS_DIR}")

    with connection(target) as conn:
        for path in sql_files:
            logger.info("Aplicando migración %s", path.name)
            sql = path.read_text(encoding="utf-8")
            conn.executescript(sql)

    logger.info("DB inicializada en %s", target)
    return target
