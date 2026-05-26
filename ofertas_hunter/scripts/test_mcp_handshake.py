#!/usr/bin/env python3
"""Smoke test del MCP server por stdio.

Lanza `python -m ofertas_hunter mcp-serve --no-lock` como subproceso, hace
handshake JSON-RPC, lista tools, y llama get_status. Si todo funciona,
imprime la lista de 16 tools.

Uso:
    .\.venv\Scripts\python.exe scripts\test_mcp_handshake.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    proc = await asyncio.create_subprocess_exec(
        str(ROOT / ".venv" / "Scripts" / "python.exe"),
        "-m", "ofertas_hunter", "mcp-serve", "--no-lock",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT),
    )

    async def send(req: dict) -> None:
        line = json.dumps(req, ensure_ascii=False) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()

    async def recv() -> dict:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
        if not line:
            raise RuntimeError("EOF en stdout del MCP server")
        return json.loads(line.decode("utf-8"))

    try:
        # 1. initialize
        await send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0.1"}
            }
        })
        resp = await recv()
        print("[1] initialize ->", json.dumps(resp.get("result", resp), indent=2)[:300])

        # 2. notification: initialized
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3. list tools
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = await recv()
        tools = resp.get("result", {}).get("tools", [])
        print(f"\n[2] tools/list -> {len(tools)} tools registradas:")
        for t in tools:
            print(f"    - {t['name']}: {t.get('description', '')[:80]}")

        # 4. call get_status
        await send({
            "jsonrpc": "2.0", "id": 3,
            "method": "tools/call",
            "params": {"name": "get_status", "arguments": {}}
        })
        resp = await recv()
        content = resp.get("result", {}).get("content", [])
        text = content[0].get("text", "") if content else json.dumps(resp)
        print(f"\n[3] get_status ->")
        print("    " + text.replace("\n", "\n    "))

        # Listo - cerramos
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), timeout=10)
        print(f"\n[OK] handshake completo. exit={proc.returncode}")
        return 0
    except Exception as exc:
        print(f"\n[FAIL] {type(exc).__name__}: {exc}")
        try:
            proc.kill()
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
            if stderr:
                print("\n--- stderr del MCP server ---")
                print(stderr[-2000:])
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
