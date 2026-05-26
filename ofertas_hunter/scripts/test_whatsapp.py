#!/usr/bin/env python
"""Prueba manual de Evolution API → WhatsApp.

Modos:

    # Dry-run (default): no llama a la red, sólo formatea y muestra el payload.
    python scripts/test_whatsapp.py

    # Real: requiere PUBLISHING_DRY_RUN=false en .env y --send.
    python scripts/test_whatsapp.py --send

    # Texto custom:
    python scripts/test_whatsapp.py --text "ping" --to 5218338498692

    # Imagen:
    python scripts/test_whatsapp.py --media https://m.media-amazon.com/images/I/61sjXQq8nVL._AC_SL1500_.jpg --caption "JBL Tune 510BT"

El script respeta las variables de entorno EVOLUTION_*, OFERTAS_WHATSAPP_GROUP_ID
y WHATSAPP_TARGET_GROUP_ID. Si no se especifica --to, usa el grupo configurado.

USO RECOMENDADO ANTES DE ACTIVAR ENVÍO REAL:
    1. Ejecuta sin --send para verificar que el payload se ve correcto.
    2. Verifica .env tiene EVOLUTION_BASE_URL, EVOLUTION_API_KEY,
       EVOLUTION_INSTANCE y OFERTAS_WHATSAPP_GROUP_ID llenos.
    3. Ejecuta con --send una sola vez con --to a tu propio teléfono
       (no al grupo) para validar.
    4. Solo entonces edita .env: PUBLISHING_ENABLED=true y PUBLISHING_DRY_RUN=false.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Permitir ejecución directa: python scripts/test_whatsapp.py
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ofertas_hunter.config import get_settings
from ofertas_hunter.logging_setup import configure_logging
from ofertas_hunter.publishing.evolution_client import EvolutionClient


async def _run(args: argparse.Namespace) -> int:
    s = get_settings()
    configure_logging(s.log_level)

    # Determinar dry-run final: --send fuerza envío real, --dry-run lo desactiva.
    if args.send:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = s.publishing_dry_run

    target = args.to or s.whatsapp_group
    if not target:
        print("ERROR: especificá --to o configurá OFERTAS_WHATSAPP_GROUP_ID en .env")
        return 2

    client = EvolutionClient(
        base_url=s.evolution_base_url,
        api_key=s.evolution_api_key,
        instance=s.evolution_instance,
        dry_run=dry_run,
    )
    print(f"--- test_whatsapp ---")
    print(f"  base_url:  {s.evolution_base_url or '(unset)'}")
    print(f"  instance:  {s.evolution_instance or '(unset)'}")
    print(f"  to:        {target}")
    print(f"  dry_run:   {dry_run}")
    print(f"  enabled:   {s.publishing_enabled}")
    print()

    if not dry_run and not client.configured:
        print(
            "ERROR: dry_run=False pero faltan EVOLUTION_BASE_URL / EVOLUTION_API_KEY / "
            "EVOLUTION_INSTANCE en .env"
        )
        await client.aclose()
        return 2

    async with client:
        if args.media:
            resp = await client.send_media(target, args.media, caption=args.caption or "")
        else:
            resp = await client.send_text(target, args.text)

    print(f"  success:    {resp.success}")
    print(f"  status:     {resp.status_code}")
    print(f"  error:      {resp.error}")
    if dry_run:
        print(f"  payload:    {json.dumps(resp.raw.get('payload'), ensure_ascii=False)[:400]}")
    else:
        print(f"  response:   {json.dumps(resp.raw, ensure_ascii=False)[:400]}")
    return 0 if resp.success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Prueba manual de Evolution API")
    parser.add_argument(
        "--text",
        default="🧪 ofertas_hunter — test_whatsapp ping",
        help="Texto a enviar (ignorado si --media está set).",
    )
    parser.add_argument("--to", default=None, help="JID del grupo o teléfono destino.")
    parser.add_argument("--media", default=None, help="URL/path/base64 de la imagen.")
    parser.add_argument("--caption", default=None)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--send",
        action="store_true",
        help="Envía de verdad (requiere credenciales válidas en .env).",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Fuerza dry-run aun si .env tiene PUBLISHING_DRY_RUN=false.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
