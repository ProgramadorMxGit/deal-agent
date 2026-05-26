# Configurar `kiro-cli` como cliente MCP de `ofertas_hunter`

`ofertas_hunter` se puede ejecutar como servidor MCP (Model Context Protocol)
para que `kiro-cli` (con Claude Sonnet 4.6) actúe como **orquestador externo**.
El bot expone 14 tools (5 de lectura, 7 de acción, 4 de calidad) que respetan
las Hard_Rules del proyecto server-side. El comando autónomo
`python -m ofertas_hunter run` sigue funcionando sin cambios — `mcp-serve` es
un modo adicional, no un reemplazo.

## Requisitos

- Python 3.11+ con el venv del proyecto (`.venv/`).
- `pip install -e .` (instala `mcp==1.27.1` automáticamente).
- `kiro-cli` instalado y accesible en el sistema.
- Cookies ML (si quieres usar `hunt_mercadolibre`) en
  `secrets/mercadolibre_cookies.example.json` o la ruta configurada en `.env`.

## 1. Verificar que `mcp-serve` arranca

```cmd
.\.venv\Scripts\python.exe -m ofertas_hunter mcp-serve --help
```

Deberías ver el subcomando registrado con sus flags (`--no-lock`).

## 2. Configurar `kiro-cli` (`~/.kiro/settings/mcp.json`)

### Windows

```json
{
  "mcpServers": {
    "ofertas-hunter": {
      "command": "C:\\Users\\<USER>\\Desktop\\bot_autonomo_ofert\\ofertas_hunter\\.venv\\Scripts\\python.exe",
      "args": ["-m", "ofertas_hunter", "mcp-serve"],
      "cwd": "C:\\Users\\<USER>\\Desktop\\bot_autonomo_ofert\\ofertas_hunter",
      "disabled": false,
      "autoApprove": [
        "get_status",
        "get_schedule_mode",
        "get_outbox",
        "get_recent_events",
        "get_frontier_stats"
      ]
    }
  }
}
```

### Linux / macOS

```json
{
  "mcpServers": {
    "ofertas-hunter": {
      "command": "/path/to/ofertas_hunter/.venv/bin/python",
      "args": ["-m", "ofertas_hunter", "mcp-serve"],
      "cwd": "/path/to/ofertas_hunter",
      "disabled": false,
      "autoApprove": [
        "get_status",
        "get_schedule_mode",
        "get_outbox",
        "get_recent_events",
        "get_frontier_stats"
      ]
    }
  }
}
```

### Notas operativas

- **`command`**: usa el `python` del venv del proyecto, no el del sistema. Si
  apuntas al Python global perderás `mcp` y `playwright`.
- **`cwd`**: debe ser la raíz del repo para que `.env`, `config/seeds/*.json`
  y la DB se resuelvan correctamente.
- **`autoApprove`**: solamente las 5 read tools. Las action y quality tools
  exigen confirmación explícita del operador para evitar sorpresas.
- **`disabled`**: ponlo en `true` mientras editas `.env` o haces deploy para
  que `kiro-cli` no intente conectarse a un proceso muerto.

## 3. Verificar handshake

Abre `kiro-cli` y dale una instrucción mínima como:

> Llama a `get_status` del servidor `ofertas-hunter` y muéstrame el resultado.

Deberías ver el modo del scheduler, los flags de publicación y el resumen
del outbox.

## 4. Ciclo recomendado en una sesión típica

1. `get_status` (siempre primero).
2. Si frontier vacío: `discover_seeds(marketplace="amazon", limit=4)` y luego
   `discover_seeds(marketplace="mercadolibre", limit=4)`.
3. `hunt_amazon(limit=5)` y `hunt_mercadolibre(limit=5)`.
4. `dispatch_outbox(limit=3)`.
5. Para items en el filo: `request_offer_review(outbox_id)` →
   `submit_offer_review` con `approve` / `reject` / `rewrite_message`.
6. Si hay degradación (`runtime_events.severity=warning|error`):
   `pause_marketplace` con `ttl_seconds`.

## Troubleshooting

### `ERROR: ya hay otra instancia activa`

Salida con código 2. Significa que `python -m ofertas_hunter run` o
`mcp-serve` ya están corriendo. Detén la otra instancia o usa `kill <pid>`
del lockfile (`data/mcp_serve.lock`).

### `Hard_Rules` bloquean publicación

Eso es esperado — son las reglas server-side. Lee la columna *Token* en
`.kiro/steering/ofertas-hunter-mcp.md` para entender la causa y la acción
recomendada (esperar, descartar item, rechazar review, etc.).

### Tool returns `{"skipped": true, "reason": "hibernating"}`

El bot está en hibernación nocturna. Espera al próximo cambio de modo
(`get_schedule_mode` te dice cuántos segundos faltan).

### `--no-lock` (sólo para tests)

El flag `--no-lock` salta la adquisición del lockfile. Está pensado para
tests in-process. **NO lo uses en producción** — es la única protección
contra dos procesos del bot pisándose mutuamente.

### Logs

`mcp-serve` envía logs a **stderr** (stdout queda libre para el protocolo
MCP). Para ver logs en tiempo real cuando arrancas manualmente:

```cmd
.\.venv\Scripts\python.exe -m ofertas_hunter mcp-serve 2> mcp.log
```

`kiro-cli` redirige stderr a sus propios logs internos.
