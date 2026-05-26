"""Smoke tests para la dependencia MCP SDK.

Garantiza que `mcp>=1.27.1` esté disponible y que los símbolos clave
(`Server`, `stdio_server`, `Tool`) puedan importarse sin error. Estos imports
deben funcionar en CUALQUIER entorno donde se haya hecho `pip install -e .`.
"""

from __future__ import annotations


def test_mcp_module_importable() -> None:
    """`mcp.server` y `mcp.server.stdio` se importan sin error."""
    import mcp.server  # noqa: F401
    import mcp.server.stdio  # noqa: F401


def test_mcp_types_tool_available() -> None:
    """`mcp.types.Tool` resuelve y se puede instanciar mínimamente."""
    from mcp.types import Tool

    tool = Tool(
        name="probe",
        description="probe",
        inputSchema={"type": "object", "additionalProperties": False},
    )
    assert tool.name == "probe"


def test_jsonschema_available() -> None:
    """`jsonschema.validate` y `ValidationError` están disponibles."""
    import jsonschema  # noqa: F401
    from jsonschema import ValidationError, validate  # noqa: F401

    # validación trivial: pasa sin levantar
    validate({"x": 1}, {"type": "object"})
