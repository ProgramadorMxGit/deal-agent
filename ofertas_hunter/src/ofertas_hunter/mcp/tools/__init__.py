"""Registry de tools MCP.

`ToolSpec` es la unidad mínima: nombre, descripción, schema, handler async,
y metadata (read-only, safety_rules). El registry es un dict por nombre.

Este módulo arrancará con un registry vacío (Fase A) y se irá poblando en
las fases B (read), C (action) y D (quality).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


HandlerFn = Callable[[Any, dict], Awaitable[dict]]


@dataclass(frozen=True)
class ToolSpec:
    """Especificación de una tool MCP."""

    name: str
    description: str
    input_schema: dict
    handler: HandlerFn
    is_read_only: bool = False
    requires_active_schedule: bool = False
    safety_rules: tuple[str, ...] = field(default_factory=tuple)

    def descriptor(self):
        """Devuelve el `mcp.types.Tool` correspondiente para anuncio del server."""
        from mcp.types import Tool

        return Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.input_schema,
        )


ToolRegistry = dict[str, ToolSpec]


def build_tool_registry(ctx: Any) -> ToolRegistry:
    """Compone el registry completo a partir de las familias.

    Por ahora vacío: las fases B/C/D irán llenándolo. Se mantiene la firma
    estable para que `MCPServer` no necesite cambios al añadir más tools.
    """
    registry: ToolRegistry = {}
    try:
        from .read_tools import build_read_tools

        for spec in build_read_tools(ctx):
            registry[spec.name] = spec
    except ImportError:
        pass
    try:
        from .action_tools import build_action_tools

        for spec in build_action_tools(ctx):
            registry[spec.name] = spec
    except ImportError:
        pass
    try:
        from .quality_tools import build_quality_tools

        for spec in build_quality_tools(ctx):
            registry[spec.name] = spec
    except ImportError:
        pass
    return registry


__all__ = [
    "HandlerFn",
    "ToolRegistry",
    "ToolSpec",
    "build_tool_registry",
]
