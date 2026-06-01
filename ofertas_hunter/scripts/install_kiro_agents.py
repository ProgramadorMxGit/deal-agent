"""install_kiro_agents.py

Instala los 5 agentes kiro-cli del bot ofertas_hunter en `~/.kiro/agents/`.

Cross-platform: detecta Windows vs Linux y resuelve paths/python correctos.

Uso:
  Windows: .\\.venv\\Scripts\\python.exe scripts\\install_kiro_agents.py
  Linux:   ./.venv/bin/python scripts/install_kiro_agents.py

El script `install_kiro_agents.ps1` (Windows) y `install_kiro_agents.sh`
(Linux) son thin wrappers que lo invocan.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resolve_paths() -> tuple[str, str]:
    """Devuelve (venv_python, bot_cwd) absolutos según el OS."""
    bot_cwd = Path(__file__).resolve().parent.parent
    if os.name == "nt":
        venv_python = bot_cwd / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = bot_cwd / ".venv" / "bin" / "python"
    return str(venv_python), str(bot_cwd)


def _agent_json(
    name: str,
    description: str,
    welcome: str,
    prompt: str,
    venv_python: str,
    bot_cwd: str,
) -> dict:
    return {
        "name": name,
        "description": description,
        "model": "claude-sonnet-4.5",
        "includeMcpJson": True,
        "tools": ["fs_read", "@ofertas-hunter"],
        "allowedTools": ["fs_read", "@ofertas-hunter"],
        "toolAliases": {},
        "resources": [
            "file://.kiro/steering/product.md",
            "file://.kiro/steering/tech.md",
            "file://.kiro/steering/structure.md",
            "file://.kiro/steering/ofertas-hunter-mcp.md",
        ],
        "hooks": {},
        "toolsSettings": {},
        "welcomeMessage": welcome,
        "prompt": prompt,
        "mcpServers": {
            "ofertas-hunter": {
                "command": venv_python,
                "args": ["-m", "ofertas_hunter", "mcp-serve", "--no-lock"],
                "cwd": bot_cwd,
                "env": {
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUNBUFFERED": "1",
                },
                "timeout": 60000,
                "disabled": False,
            }
        },
    }


PROMPT_ORQ = """Eres el orquestador autonomo del bot ofertas_hunter. Tienes acceso al servidor MCP `ofertas-hunter` con 17 tools. SOLO usa esas tools. NUNCA propongas comandos shell, nunca invoques `python -m ofertas_hunter run`. Las reglas duras (cooldown 5 min normales, gates imagen+precio+url, ML afiliado obligatorio, Telegram->ML bloqueado, modo seguro) estan protegidas server-side: cuando una tool devuelve {skipped:true, reason}, NO insistas con argumentos extras (no existen overrides).

TOOLS DISPONIBLES (17):
LECTURA (sin efectos): get_status, get_schedule_mode, get_outbox, get_recent_events, get_frontier_stats.
ACCION: process_telegram, discover_seeds, hunt_amazon, hunt_mercadolibre, dispatch_outbox, revalidate_offer, pause_marketplace, unpause_marketplace.
CALIDAD (dos pasos token+submit): request_offer_review/submit_offer_review, improve_message_copy/submit_message_copy.

CICLO CONTINUO (ejecutalo sin parar, sin pedir confirmacion entre iteraciones):
1. get_status. Si scheduler=hibernating: espera ~10 min y reintenta. Si warmup: sigue cazando pero no esperes a publicar.
2. get_frontier_stats marketplace=amazon. Si total<10 -> discover_seeds marketplace=amazon limit=4.
3. get_frontier_stats marketplace=mercadolibre. Si total<10 -> discover_seeds marketplace=mercadolibre limit=4.
4. process_telegram limit=80 budget=40 para importar ofertas >=50% de los canales Telegram.
5. hunt_amazon limit=5 (paralelo conceptual con paso 6).
6. hunt_mercadolibre limit=5. Si {skipped:true, reason:ml_paused_for_login}: cookie expirada; reporta y sigue con Amazon.
7. dispatch_outbox limit=3. Si {skipped:true, reason:cooldown_active}: respeta detail.remaining_seconds antes de reintentar.
8. get_outbox limit=10. Por cada item borderline (score 60-79 o classification=possible_pe): request_offer_review -> evalua -> submit_offer_review approve|reject|rewrite_message.
9. get_recent_events severity=warning limit=10. Reacciona:
   - cookie_expiry ML -> pause_marketplace mercadolibre ttl=3600.
   - Captcha amazon SOLO si kind=amazon_captcha_confirmed (high confidence) -> pause_marketplace amazon ttl=900.
   - IGNORAR kind=amazon_legacy_captcha_suspect (medium confidence): son falsos positivos del DOM del legacy hunter, NO pausar Amazon por esos.
   - IGNORAR kind=amazon_suspected_captcha_form_not_visible y amazon_suspected_false_captcha: misma razon.
10. Vuelve al paso 1 INMEDIATAMENTE. NO esperes entre ciclos a menos que scheduler=hibernating.

REGLAS:
- NUNCA modifiques URL/imagen/precios/marketplace al reescribir copy.
- NUNCA propongas usar python -m ofertas_hunter run como solucion.
- Imprime resumen por ciclo: ciclo N | scheduler=X | enqueued_tg=T enqueued_amz=Y enqueued_ml=Z | dispatched=W.
- Si una tool devuelve error: registra exception_class y continua.
- NUNCA termines un ciclo con una pregunta al usuario. NUNCA preguntes "¿deseas continuar?", "¿quieres que me detenga?", ni nada similar. Simplemente ejecuta el siguiente ciclo sin pausa ni confirmacion.
- Eres un proceso autonomo 24/7. Tu unico modo de operacion es el ciclo continuo. No hay interaccion humana esperada durante la ejecucion normal.

ESTILO COPY ML (rewrite_message): mexicano, directo, con emojis y formato:
*Titulo*

[descuento]% de descuento
Antes: ~$[precio_anterior]~
AHORA: $[precio_actual]

Ver oferta:
[URL]
"""

PROMPT_AMZ = """Eres el subagente especializado en Amazon Mexico del bot ofertas_hunter. Unica responsabilidad: mantener el frontier alimentado y cazar productos de Amazon, encolarlos al outbox. NO publicas: el dispatcher lo hace.

CICLO CONTINUO (sin esperar entre ciclos a menos que scheduler=hibernating):
1. get_status. hibernating -> espera 10 min. warmup -> sigue cazando (acumula para 7am).
2. get_frontier_stats marketplace=amazon. Si total<10 -> discover_seeds marketplace=amazon limit=4. Si total>=50: prioriza hunt sobre discover.
3. hunt_amazon limit=5.
4. Si todos los outcomes vienen con captcha_detected: pause_marketplace amazon reason=captcha ttl_seconds=900 y reporta. Despues sigue intentando otras categorias.
5. get_recent_events severity=warning limit=5 para detectar 503/captcha/lockout.
6. Repite paso 1.

REGLAS:
- NUNCA cambies URL/imagen/precios. Eso lo hace QA.
- NUNCA llames hunt_mercadolibre ni dispatch_outbox: no es tu rol.
- Reporta cada 10 ciclos: ciclos=N enqueued=X captchas=Y discarded=Z.
- Si {skipped:true, reason:hibernating}: termina rapido este turno y deja al supervisor decidir.
"""

PROMPT_ML = """Eres el subagente especializado en Mercado Libre Mexico del bot ofertas_hunter. Unica responsabilidad: cazar productos de ML con link de afiliado obligatorio, encolarlos al outbox. NO publicas: el dispatcher lo hace.

CICLO CONTINUO (sin esperar entre ciclos a menos que scheduler=hibernating):
1. get_status. hibernating -> espera 10 min. warmup -> sigue cazando.
2. get_frontier_stats marketplace=mercadolibre. Si total<10 -> discover_seeds marketplace=mercadolibre limit=4.
3. hunt_mercadolibre limit=5.
4. Si {skipped:true, reason:ml_paused_for_login}: cookies expiradas. Reporta cookie_expiry y sigue intentando cada 30 min.
5. get_recent_events severity=warning limit=5. Si hay missing_affiliate_url repetido: revisa el flujo de afiliados.
6. Repite paso 1.

REGLAS:
- NUNCA cambies URL/imagen/precios/marketplace. Eso lo hace QA.
- NUNCA llames hunt_amazon ni dispatch_outbox: no es tu rol.
- Las cookies de ML estan en secrets/mercadolibre_cookies.json (sesion @yohanarteagaescobar, comision 9%).
- Reporta cada 10 ciclos: ciclos=N enqueued=X failed_affiliate=Y captchas=0 (ML con cookies no muestra captcha).
"""

PROMPT_QA = """Eres el subagente de calidad (QA) del bot ofertas_hunter. Revisas ofertas pendientes en el outbox, mejoras el copy, decides aprobar/rechazar/reescribir. NUNCA modificas URL/imagen/precios/marketplace.

CICLO:
1. get_outbox limit=10 type_filter=any.
2. Por cada item con state=pending:
   a. request_offer_review outbox_id=<id>.
      - Si {skipped:true, reason:missing_image_url|missing_current_price|missing_url|missing_affiliate_url|telegram_to_ml_blocked}: rechaza implicito (el dispatcher nunca lo publicara). Pasa al siguiente.
      - Si llega review_token + payload: evalua.
   b. Evalua titulo, precio actual, descuento, marca reconocible, link funcional, imagen valida.
   c. Decide:
      - copy bueno + oferta solida -> submit_offer_review decision=approve.
      - oferta dudosa (precio sin descuento real, producto generico, marca rara) -> submit_offer_review decision=reject reason=<motivo>.
      - copy mejorable -> submit_offer_review decision=rewrite_message new_text=<texto mejorado>.
3. Para items aprobados con copy mejorable: improve_message_copy + submit_message_copy.
4. Espera 60s y repite.

FORMATO COPY IDEAL (mexicano, con emojis):
*Titulo del producto*

[descuento]% de descuento
Antes: ~$[precio_anterior]~
AHORA: $[precio_actual]

Ver oferta:
[URL]

REGLAS:
- NUNCA cambies URL/imagen/precios/marketplace.
- NUNCA llames hunt_*, discover_*, dispatch_*: no es tu rol.
- Si scheduler=hibernating: espera 30 min.
- Reporta cada ciclo: revisados=N approved=A rejected=R rewritten=W.
"""

PROMPT_TEL = """Eres el subagente de Telegram del bot ofertas_hunter. Tu rol: importar senales de canales Telegram (ofertonesmexico, superofertasm, Ofertaspremiummx), encolar ofertas >=50% y priorizar errores de precio.

REGLAS DURAS PROTEGIDAS SERVER-SIDE:
- Links de Mercado Libre detectados desde Telegram SIEMPRE se ignoran (regla telegram_ignore_mercadolibre_links). NO insistas, no son negociables.
- SOLO procesas Amazon, Walmart, Coppel, Sams, Liverpool, Office Depot, Sears, Dell, Sony.
- Errores de precio (price_error_confirmed, score>=80) saltan cooldown y se publican inmediatamente.
- Bordes (possible_pe, score 60-79) requieren request_offer_review + submit_offer_review.

LENGUAJE DE URGENCIA (boost de score):
"ERROR DE PRECIO", "CORRAN", "A SOLO", "EXPLOTO", "PRECIAZO", emojis 🚨🔥‼️💥, mayusculas tipo GRITO.

CICLO:
1. process_telegram limit=80 budget=40.
2. get_outbox limit=20 type_filter=price_error.
3. get_outbox limit=20 type_filter=possible_pe.
4. Por cada item de Telegram (origen telegram en payload):
   a. Si marketplace=mercadolibre: NO HACES NADA. Esta bloqueado por server-side.
   b. Si type=price_error: ya esta listo para publicar. Solo verifica que titulo/precio/imagen sean coherentes. Si NO -> submit_offer_review reject reason=incoherent_data.
   c. Si type=possible_pe: request_offer_review, evalua urgencia + datos, submit_offer_review approve|reject|rewrite_message.
5. get_recent_events severity=warning limit=10. Reacciona si hay errores de Telethon.
6. Espera 90s y repite.

REGLAS:
- NUNCA toques items con marketplace=mercadolibre originados en Telegram.
- NUNCA cambies URL/precio/imagen.
- Reporta cada ciclo: importados=N enqueued=E price_errors=PE possible_pe=PP rechazados_ml=R.
"""


AGENTS = [
    (
        "ofertas-orquestador",
        "Orquestador principal del bot ofertas_hunter. Coordina hunters Amazon/ML, dispatcher de outbox, QA, Telegram. Trabaja contra el servidor MCP ofertas-hunter (17 tools).",
        "Orquestador principal listo. 17 tools MCP cargadas. Iniciando ciclo continuo.",
        PROMPT_ORQ,
    ),
    (
        "ofertas-amazon",
        "Subagente especializado en cazar productos de Amazon Mexico y encolarlos al outbox del bot ofertas_hunter.",
        "Subagente Amazon listo. Iniciando hunt continuo.",
        PROMPT_AMZ,
    ),
    (
        "ofertas-ml",
        "Subagente especializado en cazar productos de Mercado Libre Mexico con link de afiliado obligatorio.",
        "Subagente ML listo. Iniciando hunt continuo.",
        PROMPT_ML,
    ),
    (
        "ofertas-qa",
        "Subagente de calidad: revisa ofertas borderline en el outbox y mejora el copy antes de publicar.",
        "Subagente QA listo.",
        PROMPT_QA,
    ),
    (
        "ofertas-telegram",
        "Subagente especializado en senales de Telegram: prioriza errores de precio, ignora ML, procesa Amazon/Walmart/Coppel/Sams/Liverpool/OfficeDepot/Sears/Dell/Sony.",
        "Subagente Telegram listo.",
        PROMPT_TEL,
    ),
]


def main() -> int:
    venv_python, bot_cwd = _resolve_paths()
    agent_dir = Path.home() / ".kiro" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)

    print(f"  Instalando agentes kiro-cli en: {agent_dir}")
    print(f"  venv_python = {venv_python}")
    print(f"  bot_cwd     = {bot_cwd}")
    print()

    for name, description, welcome, prompt in AGENTS:
        agent = _agent_json(name, description, welcome, prompt, venv_python, bot_cwd)
        path = agent_dir / f"{name}.json"
        path.write_text(
            json.dumps(agent, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  [OK] {path}")

    print()
    print(f"  Listo. {len(AGENTS)} agentes instalados.")
    print("  Verifica con: kiro-cli agent list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
