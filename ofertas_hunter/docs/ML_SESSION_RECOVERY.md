# ML Session Recovery (auto-reload de cookies via WhatsApp)

Sistema que detecta cuando expiran las cookies de Mercado Libre y permite
al admin enviarlas vía WhatsApp para que el bot las recargue **sin
reiniciar el servicio**.

> Status: Fase 1 (en repo local). Activación opt-in por config.

---

## TL;DR

```bash
# .env del bot
ML_SESSION_ALERT_ENABLED=true
ML_SESSION_ADMIN_NUMBERS=528338498692
ML_SESSION_ALERT_COOLDOWN_SECONDS=1800
ML_SESSION_INBOUND_ENABLED=true
ML_SESSION_INBOUND_HOST=127.0.0.1
ML_SESSION_INBOUND_PORT=9099
ML_SESSION_INBOUND_SECRET=<elige_un_token_random_largo>
ML_SESSION_COOKIE_BACKUP_COUNT=5
```

En Evolution API, configurar el webhook de mensajes entrantes apuntando a:

```
http://localhost:9099/wa/inbound
Header: X-Webhook-Secret: <el_mismo_token>
```

Cuando expiren las cookies ML, el bot:

1. Detecta el `cookie_expiry` (ya existente).
2. Manda un WhatsApp al `+528338498692` pidiendo el JSON.
3. Espera respuesta del admin.
4. Cuando llega un POST con el JSON, valida + escribe + hot-reload.
5. Confirma al admin con un mensaje.
6. ML retoma operaciones inmediatamente.

---

## Arquitectura

```
┌────────────────────────────────────────────────────────────────────┐
│                       ofertas_hunter (vivo)                        │
│                                                                    │
│  ┌──────────────────┐    ┌────────────────┐    ┌─────────────────┐ │
│  │ MLSessionMonitor │    │ MLInboundServer│    │ MLCookieReloader│ │
│  │ (poll runtime_ev)│    │ (HTTP :9099)   │    │ (rota + reload) │ │
│  └────────┬─────────┘    └────────┬───────┘    └────────┬────────┘ │
│           │                       │                     │          │
│           │ cookie_expiry         │ POST /wa/inbound    │          │
│           ▼                       ▼                     ▼          │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  EvolutionClient.send_text(admin, msg)  /  ctx.reload_ml_*  │  │
│  └─────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
                  │                          ▲
                  │ WhatsApp                 │ WhatsApp (JSON)
                  ▼                          │
              ┌──────────────────────────────────┐
              │  Admin (+528338498692)           │
              │  (tu celular)                    │
              └──────────────────────────────────┘
```

### Componentes

#### 1. `MLSessionMonitor`

- Lee `runtime_events` cada N segundos.
- Detecta `cookie_expiry` nuevo (post último `ml_session_admin_alerted`).
- Manda WhatsApp al admin via `EvolutionClient`.
- Aplica cooldown: si alertó hace <30 min, emite `ml_session_alert_skipped_cooldown` y se calla.
- **Tie-breaker** por `id` autoincremental de SQLite (no por timestamp,
  que tiene sub-segundo poco fiable).

#### 2. `MLInboundServer`

- HTTP server stdlib en thread separado, escucha en `127.0.0.1:9099`.
- Endpoint único: `POST /wa/inbound`.
- Auth: header `X-Webhook-Secret` debe matchear.
- Filtros:
  - Sólo acepta POSTs con `from` (extraído de payload Evolution) que esté en `admin_numbers`.
  - Soporta 3 formatos de payload Evolution (plain, v2 keyed, v2 wrapped en `data`).
  - Rechaza no-JSON (texto libre del admin se ignora con HTTP 200).
- Si pasa, llama a `MLCookieValidator` y luego al callback de reload.
- Manda WhatsApp de confirmación o error al admin (best-effort).

#### 3. `MLCookieValidator`

- Verifica que el JSON sea lista de objetos.
- Cada objeto debe tener `name`, `value`, `domain` (mín).
- Al menos un cookie debe ser de `mercadolibre.com.*` (subdominios OK).
- Tope: 200 cookies (sospechoso si más).

#### 4. `MLCookieReloader`

- Hace backup del `secrets/mercadolibre_cookies.json` actual con timestamp microsecond-precise.
- Conserva últimos N backups (default 5).
- Escribe el JSON nuevo (con indent=2, UTF-8).
- Llama `await reload_callback(cookies)` (callback es `ctx.reload_ml_cookies`).
- Emite `ml_cookies_reloaded` (info) o `ml_cookies_reload_failed` (error).

#### 5. `ServerContext.reload_ml_cookies(cookies)`

- Llama `ctx.clear_cookies()` en el browser ML actual (limpia las viejas).
- Resetea flag `_ml_session_loaded=False`.
- Llama `_ensure_ml_cookies(browser)` que relee del disco e inyecta.
- Resetea flag `_paused` del `MercadoLibreHunterAgent` cacheado.

---

## Flujo end-to-end

```
T0  ML hunter intenta navegar → login_redirect detectado
    └─→ runtime_events: cookie_expiry (severity=critical)

T1  MLSessionMonitor.tick() detecta cookie_expiry nuevo
    └─→ EvolutionClient.send_text(+528338498692, "🔐 sesión expirada...")
    └─→ runtime_events: ml_session_admin_alerted

T2  Admin abre Cookie-Editor en el browser, exporta JSON, lo pega en WhatsApp

T3  Evolution API recibe el mensaje, hace POST /wa/inbound al bot
    └─→ MLInboundServer auth ✓
    └─→ extract_admin_payload: from=528338498692, text=<json>
    └─→ MLCookieValidator: ok, 87 cookies
    └─→ MLCookieReloader.apply(cookies)
        ├─ backup secrets/cookies_backups/ml_cookies_<ts>.json
        ├─ write secrets/mercadolibre_cookies.json (nuevo)
        └─→ ctx.reload_ml_cookies(cookies)
            ├─ ctx.clear_cookies() en Playwright
            ├─ reset _ml_session_loaded=False
            ├─ _ensure_ml_cookies(browser): relee + add_cookies
            └─ ml_hunter._paused = False
    └─→ EvolutionClient.send_text(+528338498692, "✅ aceptadas...")
    └─→ runtime_events: ml_cookies_reloaded

T4  Próximo ciclo del ML hunter usa cookies frescas. Bot retoma operaciones.
```

---

## Eventos en `runtime_events`

| kind                                     | severity | cuándo                                        |
|-----------------------------------------|----------|-----------------------------------------------|
| `cookie_expiry`                         | warning  | (ya existente) detectado por hunter ML       |
| `ml_session_admin_alerted`              | info     | WhatsApp enviado al admin                    |
| `ml_session_alert_skipped_cooldown`     | info     | hay nuevo expiry pero cooldown activo        |
| `ml_session_inbound_unauthorized`       | warning  | POST sin/secret incorrecto                   |
| `ml_session_inbound_non_admin_ignored`  | info     | mensaje de número no-admin                   |
| `ml_session_inbound_non_json_ignored`   | info     | admin mandó texto que no es JSON             |
| `ml_cookies_received`                   | info     | POST válido del admin recibido               |
| `ml_cookies_validated`                  | info/warn | resultado de la validación del JSON         |
| `ml_cookies_reloaded`                   | info     | hot-reload exitoso                          |
| `ml_cookies_reload_failed`              | error    | hot-reload falló                            |

---

## Configuración

| Setting (.env)                          | Default                     | Descripción                                        |
|-----------------------------------------|-----------------------------|----------------------------------------------------|
| `ML_SESSION_ALERT_ENABLED`              | `true`                      | Habilita el monitor + envío de alertas             |
| `ML_SESSION_ADMIN_NUMBERS`              | (vacío)                     | CSV de números admin (sin `+`, sólo dígitos)       |
| `ML_SESSION_ALERT_COOLDOWN_SECONDS`     | `1800` (30 min)             | Cooldown anti-spam entre alertas                   |
| `ML_SESSION_INBOUND_ENABLED`            | `true`                      | Levanta el HTTP server entrante                    |
| `ML_SESSION_INBOUND_HOST`               | `127.0.0.1`                 | Host bindeado (recomendado loopback)               |
| `ML_SESSION_INBOUND_PORT`               | `9099`                      | Puerto del HTTP server                             |
| `ML_SESSION_INBOUND_SECRET`             | (vacío)                     | Token compartido. Si vacío, NO valida (peligroso). |
| `ML_SESSION_COOKIE_BACKUP_COUNT`        | `5`                         | Cuántos backups históricos mantener                |

### Generar un secret seguro

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Configuración Evolution API

Usa el panel admin de Evolution API:

1. Webhook URL: `http://localhost:9099/wa/inbound`
2. Headers: agrega `X-Webhook-Secret: <token_del_paso_anterior>`
3. Eventos suscritos: al menos `messages.upsert` o `MESSAGES_UPSERT`.
4. Si Evolution corre en otro server, usa la IP/host adecuado y abre el firewall del puerto 9099 SOLO para esa IP (no public-facing).

### Test manual de Evolution → bot

```bash
curl -X POST http://localhost:9099/wa/health
# => {"status":"ok"}

curl -X POST http://localhost:9099/wa/inbound \
  -H 'X-Webhook-Secret: tu_token' \
  -H 'Content-Type: application/json' \
  -d '{"from":"528338498692","text":"hola"}'
# => {"status":"ignored_not_json"}
```

---

## Cómo el admin envía las cookies

### Opción A: Cookie-Editor extension (Chrome/Brave)

1. Logueado en `mercadolibre.com.mx` en tu navegador.
2. Click en la extensión Cookie-Editor.
3. Botón **Export** → formato **JSON**.
4. Copia el JSON.
5. Pégalo en WhatsApp en el chat con el bot.

### Opción B: DevTools manual

1. F12 → Application → Cookies → `https://www.mercadolibre.com.mx`.
2. Exporta como JSON via la consola:
   ```javascript
   copy(JSON.stringify(document.cookie.split(';').map(c=>{
     const [n,v] = c.trim().split('=');
     return {name:n,value:v,domain:'.mercadolibre.com.mx',path:'/'};
   })))
   ```
   Esto copia al clipboard. Pégalo en WhatsApp.

(Opción A es mucho más completa porque incluye `httpOnly`, `secure`, `expires`, `sameSite`.)

---

## Tests

- `tests/unit/session/test_ml_session_recovery.py`: 23 tests (validador,
  monitor con cooldown, mensajes, reloader con backups).
- `tests/unit/session/test_ml_session_inbound.py`: 12 tests (auth,
  filtros admin, payload formats, end-to-end webhook).
- `tests/unit/session/test_ml_session_runtime.py`: 3 tests (E2E
  expiry → alert → POST → reload → unpause).

**Total: 38 tests. Suite global: 622 passed.**

---

## Pendientes (Fases siguientes)

- Integrar el `MLSessionRecoveryRuntime` en el lifecycle del
  `Orchestrator` y `orquestador_ia.py` (para que se arranque/cierre
  con el bot).
- Invocar `monitor.tick()` cada 30s desde el loop principal.
- Configurar Evolution API en VPS para apuntar al webhook.
- Deploy en VPS.
