# Telegram Integration — Ofertas Engine (Legacy)

Guía completa de la lógica de escucha de grupos/canales de Telegram en el **legacy monolith** (`_legacy/detector_monolith_legacy/main.py`). Incluye credenciales, canales monitoreados, handlers de eventos y flujo de procesamiento.

---

## Credenciales

Todas las credenciales viven en `runtime_secrets.json`:

| Clave | Valor | Uso |
|---|---|---|
| `telegram_api_id` | `22295300` | API ID de la app de Telegram (my.telegram.org) |
| `telegram_api_hash` | `611f70b50f4c98de216e7bf3c83f0b7a` | API Hash de la app |
| `telegram_eval_bot_token` | `8514430152:AAGNXcGY-lQso9O9xMV__1fpxF7hq5GXln8` | Token del bot de evaluación/laboratorio |

Variables de entorno equivalentes (alternativa a `runtime_secrets.json`):

```bash
OFERTAS_TELEGRAM_API_ID=22295300
OFERTAS_TELEGRAM_API_HASH=611f70b50f4c98de216e7bf3c83f0b7a
OFERTAS_TELEGRAM_EVAL_BOT_TOKEN=8514430152:AAGNXcGY-lQso9O9xMV__1fpxF7hq5GXln8
```

---

## Canales monitoreados

```python
TARGET_CHANNELS = [
    "ofertonesmexico",       # canal público por username
    "OFERTAS PREMIUM MX",    # canal por título exacto
    "OFERTAS RELAMPAGO",     # canal por título exacto
]
```

El sistema resuelve cada canal al arrancar: primero intenta por username, si falla busca por título exacto entre los diálogos del usuario.

### Chat IDs internos (canales sintéticos/virtuales)

```python
MERCADOLIBRE_HUB_CHAT_ID    = -901_000_101   # canal interno MercadoLibre Affiliate Hub
AMAZON_MX_CHAT_ID           = -901_000_102   # canal interno Amazon MX Search
SYNTHETIC_TRAINING_CHAT_ID  = -900123456     # canal de simulaciones sintéticas
```

---

## Canal de control (comandos de aprendizaje)

```python
LEARNING_CONTROL_CHANNEL = "me"   # "Saved Messages" del usuario autenticado
```

Desde este chat se envían comandos `/retrain`, `/why`, `/ok`, `/false`, etc.

---

## Destino del bot de evaluación

```python
TELEGRAM_EVAL_TARGET = "5054325626"   # user_id del operador que recibe las tarjetas
```

El bot envía tarjetas de evaluación con botones inline a este usuario.

---

## Sesiones de Telethon

| Sesión | Path en runtime | Uso |
|---|---|---|
| `ofertas_session` | `runtime/sessions/ofertas_session.session` | Cuenta de usuario (escucha canales) |
| `ofertas_eval_bot` | `runtime/sessions/ofertas_eval_bot.session` | Bot de evaluación |

---

## Arquitectura de escucha

```
TelegramClient (cuenta usuario)
  │
  ├─ @client.on(events.NewMessage(chats=resolved_channels))
  │    └─ on_new_message()
  │         ├─ Filtra mensajes anteriores al arranque
  │         └─ enqueue_channel_message() → channel_message_queue (asyncio.Queue)
  │
  ├─ @client.on(events.NewMessage(chats=learning_control_chat_ids))
  │    └─ on_learning_control_message()
  │         └─ handle_learning_command() — comandos /retrain, /why, /ok, etc.
  │
  └─ channel_message_worker × 3 (TELEGRAM_CHANNEL_WORKERS)
       └─ process_channel_message()
            ├─ build_parsed_from_message()   — parsea texto + descarga imagen
            ├─ detector.evaluate_offer()     — evaluación IA
            ├─ detector.resolve_offer_routing()
            └─ enqueue_production_candidate() → cola de publicación WhatsApp

TelegramClient (bot de evaluación)
  │
  ├─ @eval_bot_client.on(events.NewMessage)
  │    └─ on_eval_bot_message()
  │         ├─ handle_eval_bot_command()   — /sim_on, /sim_off, /lab_status, etc.
  │         └─ process_pending_comment_message()
  │
  └─ @eval_bot_client.on(events.CallbackQuery)
       └─ on_eval_callback()
            └─ Procesa botones inline: good_alert | false_alert | missed_deal |
               price_error_correct | price_error_false | why | comment
```

---

## Handlers registrados

### 1. `on_new_message` — mensajes en vivo de los canales

```python
@client.on(events.NewMessage(chats=resolved_channels))
async def on_new_message(event):
    # Solo procesa mensajes posteriores al arranque del listener
    if not is_live_message_from_current_run(event.message):
        return
    await enqueue_channel_message(
        message=event.message,
        chat_id=chat_id,
        channel_title=channel_title,
        process_origin="live",
    )
```

### 2. `on_learning_control_message` — comandos desde Saved Messages

```python
@client.on(events.NewMessage(chats=learning_control_chat_ids))
async def on_learning_control_message(event):
    await handle_learning_command(event)
```

Comandos disponibles:

| Comando | Acción |
|---|---|
| `/retrain` | Fuerza reentrenamiento del modelo de aprendizaje |
| `/learning_status` | Estado del sistema de aprendizaje |
| `/why <ref>` | Explica la decisión para una oferta |
| `/note <ref> \| <texto>` | Agrega nota de feedback |
| `/ok <ref>` | Marca como buena alerta |
| `/false <ref>` | Marca como falsa alerta |
| `/missed <ref>` | Marca como oferta perdida |
| `/price_ok <ref>` | Precio correcto |
| `/price_false <ref>` | Error de precio falso |
| `/probe_batch <n>` | Envía lote de simulaciones sintéticas |
| `/sim_on` | Activa simulaciones sintéticas |
| `/sim_off` | Pausa simulaciones sintéticas |

### 3. `on_eval_bot_message` — mensajes al bot de evaluación

Recibe mensajes del operador (`TELEGRAM_EVAL_TARGET = 5054325626`) y procesa comandos del laboratorio.

### 4. `on_eval_callback` — botones inline de tarjetas de evaluación

Procesa el feedback del operador sobre cada oferta evaluada. Formato del callback data: `ev|<action>|<lookup_key>`.

---

## Flujo completo de un mensaje de canal

```
Canal Telegram (ofertonesmexico / OFERTAS PREMIUM MX / OFERTAS RELAMPAGO)
  │
  │  [evento NewMessage]
  ▼
on_new_message()
  │  filtra mensajes viejos
  ▼
enqueue_channel_message()  →  channel_message_queue
  │
  ▼
channel_message_worker (×3 workers en paralelo)
  │
  ▼
process_channel_message()
  ├─ is_message_already_processed()  → skip si ya fue procesado
  ├─ maybe_skip_mercadolibre_channel_message()  → skip si es ML sin share button
  ├─ build_parsed_from_message()
  │    ├─ download_message_image()   → descarga imagen si la hay
  │    └─ parse_offer()              → extrae precios, descuento, título, fuente
  ├─ detector.evaluate_offer()       → evaluación IA (Groq/NVIDIA)
  ├─ detector.resolve_offer_routing()
  │    ├─ production_alert=True  → enqueue_production_candidate()
  │    └─ telegram_eval=True     → maybe_send_evaluation_card() al bot
  └─ state.save()
```

---

## Backfill de mensajes perdidos

Al arrancar y periódicamente (cada `MESSAGE_BACKFILL_POLL_SECONDS = 90s`) el sistema recupera mensajes que pudo haber perdido:

```python
MESSAGE_BACKFILL_LIMIT_PER_CHANNEL         = 400   # máx mensajes a consultar
MESSAGE_BACKFILL_PROCESS_BUDGET_PER_CHANNEL = 40    # máx a procesar por ciclo
```

---

## Reintentos de ofertas high-value

Ofertas que no pudieron evaluarse correctamente se guardan en cola y se reintentan:

```python
MAX_PENDING_HIGH_VALUE_RETRIES       = 6
HIGH_VALUE_RETRY_BACKOFF_MINUTES     = [5, 15, 60]
PENDING_REVIEW_POLL_SECONDS          = 15
```

---

## Alertas de runtime a Telegram

El sistema envía alertas a `Saved Messages` del usuario autenticado cuando hay errores críticos:

```python
await client.send_message("me", build_runtime_alert_text(title, details))
```

Cooldown por defecto: `RUNTIME_ALERT_COOLDOWN_SECONDS = 15 * 60` (15 minutos).

---

## Archivos relevantes

| Archivo | Contenido |
|---|---|
| `_legacy/detector_monolith_legacy/main.py` | Todo el código de Telegram (líneas ~10380–12300) |
| `_legacy/detector_monolith_legacy/runtime_secrets.json` | Credenciales reales |
| `_legacy/detector_monolith_legacy/runtime/sessions/` | Archivos `.session` de Telethon |

---

## Cómo obtener nuevas credenciales de Telegram

1. Ir a [https://my.telegram.org](https://my.telegram.org)
2. Iniciar sesión con el número de teléfono de la cuenta
3. Ir a **API development tools**
4. Crear una nueva app → obtenés `api_id` y `api_hash`
5. Guardarlos en `runtime_secrets.json` bajo `telegram_api_id` y `telegram_api_hash`

Para el bot de evaluación:
1. Hablar con [@BotFather](https://t.me/BotFather) en Telegram
2. `/newbot` → seguir instrucciones
3. Guardar el token en `runtime_secrets.json` bajo `telegram_eval_bot_token`
