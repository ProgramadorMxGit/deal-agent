"""Auditoría sanitizada de cada llamada MCP.

Cada invocación a una tool produce dos eventos en `runtime_events`:

- Uno `phase=before` con el resumen de los argumentos.
- Uno `phase=after` con el resumen del resultado, o `phase=error` si la tool
  levantó una excepción.

`sanitize` es responsable de:

- Truncar strings que excedan `_MAX_STRING_LEN`.
- Reemplazar valores de claves "sensibles" (cookies, api keys, tokens, etc.)
  con `"[redacted]"`.
- Recorrer recursivamente dicts y listas.

La sanitización siempre se aplica antes de persistir. Aunque el cliente envíe
una cookie completa o una api key, queda fuera de la BD.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..runtime.events import emit_runtime_event


_MAX_STRING_LEN = 500

# Claves cuyos valores nunca queremos persistir tal cual (case-insensitive).
_SECRET_KEYS = {
    "cookies",
    "cookie",
    "api_key",
    "apikey",
    "anthropic_api_key",
    "evolution_api_key",
    "telegram_api_hash",
    "session_string",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "bearer",
    "password",
    "pass",
    "secret",
}


def sanitize(value: Any) -> Any:
    """Devuelve una versión segura de `value` para persistir en runtime_events."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, sub in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                result[key] = "[redacted]"
            else:
                result[key] = sanitize(sub)
        return result
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    if isinstance(value, str) and len(value) > _MAX_STRING_LEN:
        extra = len(value) - _MAX_STRING_LEN
        return value[:_MAX_STRING_LEN] + f"...[+{extra}]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Fallback genérico: representación corta
    text = repr(value)
    if len(text) > _MAX_STRING_LEN:
        text = text[:_MAX_STRING_LEN] + "..."
    return text


# ---------------------------------------------------------------------------
# Auditoría per-call
# ---------------------------------------------------------------------------


def audit_before(conn: sqlite3.Connection, tool: str, args: Any) -> int:
    """Emite el evento `phase=before` y devuelve el id del runtime_event."""
    return emit_runtime_event(
        conn,
        kind="mcp_tool_called",
        severity="info",
        payload={
            "phase": "before",
            "tool": tool,
            "args_summary": sanitize(args),
        },
    )


def audit_after(conn: sqlite3.Connection, tool: str, result: Any) -> int:
    return emit_runtime_event(
        conn,
        kind="mcp_tool_called",
        severity="info",
        payload={
            "phase": "after",
            "tool": tool,
            "result_summary": sanitize(result),
        },
    )


def audit_error(
    conn: sqlite3.Connection,
    tool: str,
    args: Any,
    exc: BaseException,
) -> int:
    return emit_runtime_event(
        conn,
        kind="mcp_tool_called",
        severity="error",
        payload={
            "phase": "error",
            "tool": tool,
            "args_summary": sanitize(args),
            "exception_class": type(exc).__name__,
            "exception_message": sanitize(str(exc)),
        },
    )


__all__ = [
    "audit_after",
    "audit_before",
    "audit_error",
    "sanitize",
]
