"""Cliente async para invocar kiro-cli via subprocess.

Diseño:
- ``--classic --no-interactive`` para que termine al completar la respuesta.
- Captura stdout, parsea JSON con regex tolerante a texto introductorio.
- Timeout duro configurable (default 30s).
- Retorna ``None`` ante cualquier fallo (no lanza excepciones — el caller
  maneja el fallback).

Sin efectos secundarios al importar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


# Clave esperada en el JSON de respuesta del curator. Si el contrato del
# schema cambia (schema drift), actualizar esta constante para que el
# regex de extracción siga encontrando el bloque correcto.
EXPECTED_JSON_KEY = "chosen_id"

# Regex para extraer un objeto JSON del stdout (tolerante a markdown,
# texto introductorio, etc.). Busca el bloque más cercano que contiene
# la clave esperada (no soporta objetos anidados — suficiente para
# el contrato actual del curator).
_JSON_BLOCK_RE = re.compile(
    r"\{[^{}]*\"" + re.escape(EXPECTED_JSON_KEY) + r"\"[^{}]*\}",
    re.DOTALL,
)


def resolve_kiro_cli_path() -> str:
    """Determina la ruta al binario kiro-cli.

    Orden:
    1. ``KIRO_CLI`` env var (si la ruta existe).
    2. En Windows: ``%LOCALAPPDATA%\\Kiro-Cli\\kiro-cli.exe`` y luego
       ``~/AppData/Local/Kiro-Cli/kiro-cli.exe``.
       En Linux/Mac: ``~/.local/bin/kiro-cli``, ``/usr/local/bin/kiro-cli``,
       ``/usr/bin/kiro-cli``.
    3. ``shutil.which("kiro-cli")``.
    4. Fallback al string literal ``"kiro-cli"`` (subprocess fallará limpio
       más adelante si no existe).
    """
    override = os.environ.get("KIRO_CLI")
    if override and Path(override).exists():
        return override

    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        candidates = []
        if local_appdata:
            candidates.append(Path(local_appdata) / "Kiro-Cli" / "kiro-cli.exe")
        candidates.append(
            Path.home() / "AppData" / "Local" / "Kiro-Cli" / "kiro-cli.exe"
        )
    else:
        candidates = [
            Path.home() / ".local" / "bin" / "kiro-cli",
            Path("/usr/local/bin/kiro-cli"),
            Path("/usr/bin/kiro-cli"),
        ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    on_path = shutil.which("kiro-cli")
    if on_path:
        return on_path

    return "kiro-cli"


@dataclass
class KiroCliConfig:
    """Configuración para :class:`KiroCliClient`."""

    binary_path: str = ""  # vacío → auto-detect
    classic_mode: bool = True
    timeout_seconds: float = 30.0


class KiroCliClient:
    """Cliente async sobre el binario ``kiro-cli``.

    Uso::

        client = KiroCliClient()
        data = await client.ask_json("Choose an offer: ...")
        if data is None:
            # fallback a heurísticas locales
            ...
    """

    def __init__(self, config: Optional[KiroCliConfig] = None) -> None:
        self.config = config or KiroCliConfig()

    @staticmethod
    async def _terminate(proc: "asyncio.subprocess.Process") -> None:
        """Mata el subprocess y espera su exit.

        - Tolera ``ProcessLookupError`` (el proceso ya terminó) silenciosamente.
        - Cualquier otro error se loguea con stack trace pero no se propaga
          para no romper el flujo de timeout/cancelación.
        """
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("kiro-cli: error matando subprocess")

        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("kiro-cli: error esperando subprocess tras kill")

    async def ask_json(
        self,
        prompt: str,
        *,
        agent: Optional[str] = None,
    ) -> Optional[dict]:
        """Invoca kiro-cli con ``prompt``.

        Retorna el ``dict`` parseado del JSON de stdout, o ``None`` si
        el binario falla, devuelve no-cero, hace timeout, o no produce
        un JSON parseable.
        """
        binary = self.config.binary_path or resolve_kiro_cli_path()

        args: list[str] = [binary]
        if self.config.classic_mode:
            args.append("--classic")
        args.extend(["chat", "--no-interactive"])
        if agent is not None:
            args.extend(["--agent", agent])
        args.extend(["--trust-all-tools", prompt])

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("kiro-cli no encontrado en %s", binary)
            return None
        except Exception:
            logger.exception("kiro-cli: error al spawnar subprocess")
            return None

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._terminate(proc)
            logger.warning(
                "kiro-cli: timeout (%.1fs) — matando subprocess",
                self.config.timeout_seconds,
            )
            return None
        except asyncio.CancelledError:
            # Crucial: si el caller cancela (shutdown, watchdog, wait_for
            # exterior), debemos matar el subprocess para no dejar procesos
            # huérfanos con --trust-all-tools, y luego re-lanzar la
            # cancelación para no swallow-ear la señal.
            await self._terminate(proc)
            logger.warning(
                "kiro-cli: tarea cancelada — matando subprocess"
            )
            raise
        except Exception:
            logger.exception("kiro-cli: error en communicate")
            return None

        if proc.returncode != 0:
            stderr_text = (stderr or b"").decode("utf-8", errors="replace")[:500]
            logger.warning(
                "kiro-cli: exit code %s, stderr: %s",
                proc.returncode,
                stderr_text,
            )
            return None

        text = (stdout or b"").decode("utf-8", errors="replace")
        return _parse_json_response(text)


def _parse_json_response(text: str) -> Optional[dict]:
    """Extrae un objeto JSON del stdout.

    Estrategia:
    1. Intentar ``json.loads`` sobre el texto completo.
    2. Buscar bloque ``{... "chosen_id" ...}`` con regex.
    3. Si nada funciona, retornar ``None``.
    """
    if not text:
        return None

    text_stripped = text.strip()

    # Intento 1: stdout es JSON puro
    try:
        data = json.loads(text_stripped)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Intento 2: extraer bloque con regex
    match = _JSON_BLOCK_RE.search(text_stripped)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    return None
