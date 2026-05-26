"""Servidor MCP (Model Context Protocol) de ofertas_hunter.

Este paquete expone los recursos del bot como tools MCP consumibles por
`kiro-cli` u otro cliente MCP compatible. Las clases públicas se cargan de
forma perezosa para que el resto del bot pueda importar tipos sin arrastrar
la dependencia `mcp` en comandos como `run`, `init-db` o `status`.
"""

from __future__ import annotations

__all__ = [
    "FileLock",
    "LockConflict",
]


def __getattr__(name: str):  # pragma: no cover - cargador perezoso
    if name in {"FileLock", "LockConflict"}:
        from .lockfile import FileLock, LockConflict

        return {"FileLock": FileLock, "LockConflict": LockConflict}[name]
    raise AttributeError(f"module 'ofertas_hunter.mcp' has no attribute {name!r}")
