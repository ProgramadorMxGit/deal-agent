"""MCPServer: handshake, registry y dispatch.

Pipeline de cada `dispatch(name, args)`:

    1. Tool desconocida → {"error": "unknown_tool"}.
    2. jsonschema.validate(args, spec.input_schema). Falla → audit_error +
       {"error": "validation_failed"}.
    3. audit_before(spec, args).
    4. apply_hard_rules(spec.safety_rules, args). Skip → audit_after(skip) +
       {"skipped": true, "reason": ...}.
    5. handler(ctx, args). Excepción → audit_error + {"error": "handler_failed"}.
    6. audit_after(result) + return result.

Las dependencias `mcp.server.*` se importan de forma perezosa para que el
resto del bot pueda usar `MCPServer` solo si realmente arranca el subcomando
`mcp-serve`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import jsonschema

from .audit import audit_after, audit_before, audit_error
from .context import ServerContext
from .lockfile import FileLock
from .safety import apply_hard_rules
from .tools import ToolRegistry, ToolSpec, build_tool_registry


logger = logging.getLogger(__name__)


class MCPServer:
    """Wrapper alrededor del SDK `mcp.server.Server`.

    Args:
        ctx: ServerContext compartido por todos los handlers.
        lockfile: lock cooperativo. Puede ser None en tests in-process.
        registry: si se pasa, se usa tal cual (útil para tests). Si no, se
            construye con `build_tool_registry(ctx)`.
    """

    def __init__(
        self,
        ctx: ServerContext,
        lockfile: Optional[FileLock] = None,
        *,
        registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.ctx = ctx
        self.lockfile = lockfile
        self.registry: ToolRegistry = registry if registry is not None else build_tool_registry(ctx)

    # ------------------------------------------------------------------
    # Dispatch (testeable in-process sin stdio)
    # ------------------------------------------------------------------

    async def dispatch(self, name: str, args: Optional[dict] = None) -> dict:
        """Ejecuta una tool por nombre con sus args. Devuelve siempre dict."""
        args = dict(args or {})
        spec = self.registry.get(name)
        if spec is None:
            return {"error": "unknown_tool", "tool": name}

        # 1. Schema validation
        try:
            jsonschema.validate(args, spec.input_schema)
        except jsonschema.ValidationError as exc:
            audit_error(self.ctx.db, name, args, exc)
            return {"error": "validation_failed", "detail": exc.message}

        # 2. Audit before
        try:
            audit_before(self.ctx.db, name, args)
        except Exception:
            logger.exception("audit_before falló (no fatal)")

        # 3. Hard rules (sólo si la tool no es read-only)
        if not spec.is_read_only and spec.safety_rules:
            check = await apply_hard_rules(self.ctx, spec.safety_rules, args)
            if check.skipped:
                result = {"skipped": True, "reason": check.reason}
                if check.detail:
                    result["detail"] = check.detail
                try:
                    audit_after(self.ctx.db, name, result)
                except Exception:
                    logger.exception("audit_after(skip) falló")
                return result

        # 4. Handler
        try:
            handler_result = await spec.handler(self.ctx, args)
        except Exception as exc:
            audit_error(self.ctx.db, name, args, exc)
            return {
                "error": "handler_failed",
                "exception_class": type(exc).__name__,
            }

        if not isinstance(handler_result, dict):
            handler_result = {"data": handler_result}

        # 5. Audit after
        try:
            audit_after(self.ctx.db, name, handler_result)
        except Exception:
            logger.exception("audit_after falló (no fatal)")
        return handler_result

    # ------------------------------------------------------------------
    # stdio (lazy import del SDK)
    # ------------------------------------------------------------------

    async def serve_stdio(self) -> None:
        """Conecta el servidor a stdio usando la API de bajo nivel del SDK mcp.
        Bloquea hasta EOF / Ctrl+C.
        """
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import (
            CallToolResult,
            InitializeResult,
            ListToolsResult,
            ServerCapabilities,
            TextContent,
            Tool,
        )

        server = Server("ofertas-hunter")

        @server.list_tools()
        async def _list_tools() -> list[Tool]:
            return [spec.descriptor() for spec in self.registry.values()]

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict | None = None) -> list[TextContent]:
            result = await self.dispatch(name, arguments or {})
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    async def shutdown(self) -> None:
        await self.ctx.aclose()
        if self.lockfile is not None:
            try:
                self.lockfile.release()
            except Exception:
                logger.exception("lockfile.release falló")


__all__ = ["MCPServer", "ToolSpec"]
