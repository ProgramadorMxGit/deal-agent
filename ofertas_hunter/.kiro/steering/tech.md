---
inclusion: fileMatch
fileMatchPattern: "*"
---

# Tecnologia: ofertas_hunter

## Stack

- **Python 3.11+** con `asyncio`, `dataclasses`, type hints.
- **SQLite WAL** (`data/ofertas_hunter.db`) como fuente de verdad. Modo WAL permite
  lectura concurrente entre `mcp-serve` y `run`.
- **Playwright headless** para scraping (Chromium). Singleton `BrowserWorker` por
  proceso para no saturar.
- **Telethon** para Telegram (sesion en `secrets/telegram.session`, usuario @YonDev).
- **Evolution API** (HTTP) para WhatsApp via Baileys. Instancia `mi-instancia` en
  `http://162.251.147.177:8080`.
- **MCP** (Model Context Protocol) sobre stdio. Servidor expone 16 tools en
  `python -m ofertas_hunter mcp-serve`.
- **kiro-cli** como cliente MCP / orquestador IA. Modelo Claude Sonnet 4.5.
- **subagentes kiro-cli**: `ofertas-orquestador`, `ofertas-amazon`, `ofertas-ml`,
  `ofertas-qa`, `ofertas-telegram`.

## Estructura de paquete

- `src/ofertas_hunter/` — paquete principal.
  - `agents/` — agentes Python (DiscoveryAgent, AmazonHunterAgent, MercadoLibreHunterAgent, OutboxDispatcher, TelegramListenerAgent).
  - `browser/` — wrappers Playwright.
  - `db/` — schema, conexion, migraciones.
  - `dispatching/` — outbox + cooldown + dispatcher.
  - `extraction/` — parsers HTML.
  - `marketplaces/` — extractores especificos (afiliados ML, etc).
  - `mcp/` — servidor MCP, tools, safety, audit.
  - `publishing/` — Evolution client + WhatsApp publisher + formatter.
  - `runtime/` — scheduler + watchdog + loops.
  - `scoring/` — PriceErrorScorer.
  - `session/` — gestion de cookies (Mercado Libre).
  - `telegram/` — listener + parser de mensajes.

## Comandos clave

- `python -m ofertas_hunter run` — daemon completo (fallback sin IA).
- `python -m ofertas_hunter mcp-serve` — arranca servidor MCP por stdio.
- `python -m ofertas_hunter dispatch --once` — un tick del dispatcher.
- `python -m ofertas_hunter status` — estado actual del bot.
- `kiro-cli chat --agent ofertas-orquestador` — orquestador IA con tools MCP.

## Lockfile

- `data/mcp_serve.lock` previene concurrencia entre `run` y `mcp-serve` contra la
  misma DB. `--no-lock` permite tests in-process.

## Seguridad

- Secretos en `.env` (no commiteado). Cookies ML en `secrets/mercadolibre_cookies.json`.
- El audit log (`runtime_events kind=mcp_tool_called`) sanitiza cookies, api_keys,
  tokens y trunca strings >500 chars.
- Modo seguro por defecto: `PUBLISHING_DRY_RUN=true`. Activar real solo tras
  validacion manual.

## Tests

- `pytest -q` desde la raiz. 477+ tests verde antes del merge.
- Property-based tests con Hypothesis para Hard_Rules.

## Deploy

- `deploy/` contiene unidades systemd (Linux) y servicio Windows.
- `start.ps1` lanzador con menu [1-4].
- `scripts/orquestador_ia.py` daemon Python con 3 loops paralelos (Amazon, ML, Dispatcher).
- `scripts/install_kiro_agents.ps1` instala los 5 agentes kiro-cli.

## Limitaciones conocidas

- `kiro-cli --no-interactive` NO carga servidores MCP (solo `execute_cmd`). MCP
  requiere sesion interactiva.
- En consola Windows legacy (cp1252), emojis Unicode fallan. Los scripts Python
  usan iconos ASCII (`[AMZ]`, `[ML]`, `[DSP]`, `[IA]`).
