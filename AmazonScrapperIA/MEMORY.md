# MEMORY — Sistema de Ofertas Automatizado

> Documento de referencia completo. Última actualización: 25 Mayo 2026.

---

## Acceso VPS

| Campo | Valor |
|---|---|
| IP | `104.225.140.187` |
| Usuario | `root` |
| Contraseña | `G7f#2vP9xLq8!Z` |
| Proveedor | XIP Cloud (panel en My Cloud) |
| Zona | US-TX Texas |
| RAM | 4096 MB |
| CPU | 2 cores |
| Disco | 30 GB |

**Conexión SSH desde Windows:**
```python
# Usar paramiko (no OpenSSH — requiere input interactivo)
import paramiko
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("104.225.140.187", username="root", password="G7f#2vP9xLq8!Z", timeout=30)
```

---

## Arquitectura General

```
┌─────────────────────────────────────────────────────────────────────┐
│  VPS 104.225.140.187                                                │
│                                                                     │
│  ┌──────────────────────────────┐                                   │
│  │  bot-diversidad-global       │  Crawlea ML con Playwright        │
│  │  /opt/bot-diversidad-global  │  ~19 ofertas/hora                 │
│  │  → data/offers.json          │  Sesión ML + afiliado activa      │
│  └──────────────┬───────────────┘                                   │
│                 │ (archivo compartido)                              │
│  ┌──────────────▼───────────────┐                                   │
│  │  ofertas-diversidad-bridge   │  Lee offers.json cada 60s         │
│  │  (servicio systemd)          │  Importa al hub_outbox.json       │
│  └──────────────┬───────────────┘                                   │
│                 │ (hub_outbox.json)                                 │
│  ┌──────────────▼───────────────┐                                   │
│  │  ofertas-detector            │  Evalúa con Claude AI             │
│  │  /opt/ofertas-detector-vps   │  Revalida descuento en vivo       │
│  │                              │  Despacha al grupo WhatsApp       │
│  │  → WhatsApp grupo            │  1 oferta cada 5 min (día)        │
│  │                              │  1 oferta cada 2 horas (noche)    │
│  └──────────────────────────────┘                                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Servicios Systemd

### 1. `bot-diversidad-global.service`
- **Ruta:** `/opt/bot-diversidad-global/`
- **Qué hace:** Crawlea MercadoLibre MX con Playwright, detecta productos con ≥50% descuento, extrae link de afiliado haciendo clic en el botón "Compartir", evalúa con Claude (kiro-cli), guarda en `data/offers.json`
- **Cookies:** Lee de `/opt/ofertas-detector-vps/data/mercadolibre/browser_cookies.json`
- **Reiniciar:** `systemctl restart bot-diversidad-global`
- **Logs:** `journalctl -u bot-diversidad-global -f`

### 2. `ofertas-diversidad-bridge.service`
- **Qué hace:** Lee `offers.json` del bot cada 60 segundos, importa nuevas ofertas al `hub_outbox.json` del ofertas-detector
- **Ruta script:** `/opt/ofertas-detector-vps/scripts/run_diversidad_global_bridge.py`
- **Variable clave:** `OFERTAS_DIVERSIDAD_SOURCE_OFFERS_PATH=/opt/bot-diversidad-global/data/offers.json`
- **Reiniciar:** `systemctl restart ofertas-diversidad-bridge`

### 3. `ofertas-detector.service`
- **Qué hace:** Evalúa items del outbox con Claude AI, revalida descuentos en vivo, despacha al grupo de WhatsApp
- **Ruta:** `/opt/ofertas-detector-vps/`
- **Colectores secundarios:** DESACTIVADOS (`mercadolibre.enabled=false` en `config/defaults.yaml`) — solo usa lo que llega del bridge
- **Reiniciar:** `systemctl restart ofertas-detector`
- **Logs:** `journalctl -u ofertas-detector -f`

---

## Evolution API (WhatsApp)

| Campo | Valor |
|---|---|
| Base URL | `http://162.251.147.177:8080` |
| API Key | `dev-evolution-api-key` |
| Instancia envío | `mi-instancia` |
| Instancia recepción | `whatsapp-8338498692-v2` |
| Admin WhatsApp | `5218338498692` |
| Admin LID | `25877295435783@lid` |
| Grupo destino | `120363426569734715@g.us` |

---

## Reglas de Despacho

| Horario | Cooldown |
|---|---|
| Día (07:00 - 23:00 Monterrey) | 5 minutos entre mensajes |
| Noche (23:00 - 07:00 Monterrey) | 2 horas entre mensajes |

**Zona horaria:** `America/Mexico_City` (UTC-6)

---

## Revalidación de Descuento Antes de Despachar

Antes de enviar cada oferta al grupo, el sistema verifica que el descuento siga siendo ≥50% en vivo.

**Cómo funciona:**
1. Toma el `link` de afiliado (`meli.la/...`) del candidato
2. Hace HTTP GET sin cookies (la página de perfil de afiliado es pública)
3. Extrae el badge `"65% OFF"` del HTML
4. Si descuento < 50% → descarta el item
5. Si ok=False (error) → deja pasar el item (no bloquea la cola)

**Script:** `/opt/ofertas-detector-vps/scripts/revalidate_discount.py`

**Ventaja clave:** La URL `meli.la/...` redirige a la página pública del perfil de afiliado que muestra el descuento sin necesitar cookies de sesión.

**Selectores HTML usados:**
```html
<!-- Badge de descuento (más confiable) -->
<span class="andes-money-amount__discount poly-price__disc--pill">65% OFF</span>

<!-- Precio anterior -->
aria-label="Antes: 1390 pesos mexicanos"

<!-- Precio actual -->
aria-label="Ahora: 486 pesos mexicanos"
```

---

## Cookies de Sesión ML

### Problema histórico
Las cookies exportadas manualmente con Cookie-Editor expiran en ~30 minutos porque `NSESSIONID_qrtsid` tiene vida corta y ML la invalida cuando detecta IP diferente (tu browser en México vs VPS en Texas).

### Solución implementada
**Cron de exportación automática** — cada 10 minutos exporta las cookies del perfil persistente de Chromium del `ofertas-detector` al archivo `browser_cookies.json`:

```bash
# Cron del usuario ofertasrdp
*/10 * * * * python3 /opt/ofertas-detector-vps/scripts/export_profile_cookies.py >> /var/log/cookie_export.log 2>&1
```

**Script:** `/opt/ofertas-detector-vps/scripts/export_profile_cookies.py`
- Lee el SQLite de Chromium: `/opt/ofertas-detector-vps/data/mercadolibre/browser_profile_runtime/Default/Cookies`
- Exporta al archivo: `/opt/ofertas-detector-vps/data/mercadolibre/browser_cookies.json`

### Cuándo renovar manualmente
Si el bot detecta redirecciones a `account-verification` en páginas de producto:
1. El bot te manda un WhatsApp con instrucciones
2. Exportas las cookies con Cookie-Editor desde tu browser
3. Respondes el WhatsApp con el JSON
4. El `CookieInboxWatcher` las aplica en <10 segundos automáticamente
5. O puedes subir el archivo directamente: `C:\Users\Programador Mx\Desktop\vps_\cookies.json` → `/opt/ofertas-detector-vps/data/mercadolibre/browser_cookies.json`

---

## Archivos Clave en el VPS

| Archivo | Descripción |
|---|---|
| `/opt/bot-diversidad-global/data/offers.json` | Ofertas encontradas por el bot (316+ items) |
| `/opt/bot-diversidad-global/data/state.json` | Frontier, visitados, stats del crawler |
| `/opt/ofertas-detector-vps/data/mercadolibre/hub_outbox.json` | Cola de procesamiento (outbox) |
| `/opt/ofertas-detector-vps/data/state/operational_state.json` | Cola de despacho, historial |
| `/opt/ofertas-detector-vps/data/mercadolibre/browser_cookies.json` | Cookies ML activas |
| `/opt/ofertas-detector-vps/data/mercadolibre/browser_profile_runtime/` | Perfil Chromium persistente |
| `/opt/ofertas-detector-vps/config/defaults.yaml` | Configuración del dispatcher |
| `/opt/ofertas-detector-vps/scripts/revalidate_discount.py` | Revalidación de descuento |
| `/opt/ofertas-detector-vps/scripts/export_profile_cookies.py` | Exportador de cookies del perfil |
| `/opt/ofertas-detector-vps/main.py` | Proceso principal del dispatcher |

---

## Archivos Locales de Utilidad

Todos en `C:\Users\Programador Mx\Desktop\vps_\`:

| Archivo | Uso |
|---|---|
| `vps_cmd.py` | Ejecutar comandos en VPS via paramiko |
| `check_queue.py` | Ver cola de despacho actual |
| `check_offers_today.py` | Ver ofertas encontradas hoy |
| `check_new_since_last.py` | Ver nuevas ofertas desde última revisión |
| `full_report.py` | Reporte completo del sistema |
| `upload_cookies.py` | Subir cookies.json al VPS |
| `check_dispatch_timing2.js` | Analizar timing de despachos |
| `cookies.json` | Cookies ML más recientes (exportar con Cookie-Editor) |

---

## Modificaciones Importantes al Código

### `main.py` (ofertas-detector)
1. **Bypass validación API para diversidad_global** — los IDs de 8 dígitos (`MLM-54097641`) son product group IDs que la API pública no reconoce. Se bypasean y se tratan como pre-validados.
2. **Revalidación batch con meli.la** — antes de despachar, verifica descuento en vivo via HTTP sin cookies.
3. **Eliminado `inventory_expired`** — los items del outbox ya no expiran por tiempo.
4. **`resend_after_hours` reducido** — de 72h a 24h para items de diversidad.

### `browser_worker.py` (bot-diversidad-global)
- Usa `chromium.launch()` con cookies inyectadas desde `browser_cookies.json`
- El cron actualiza las cookies cada 10 min desde el perfil persistente

### `utils.py` (bot-diversidad-global)
- `listado.mercadolibre.com.mx` bloqueado — devuelve HTTP 403 desde la IP del VPS
- Solo se crawlea `www.mercadolibre.com.mx`

### `core.py` (ofertas-detector)
- `is_plausible_meli_item_id` — cambiado de 9+ dígitos a 7+ dígitos para aceptar IDs de 8 dígitos

---

## Flujo Completo de una Oferta

```
1. bot-diversidad-global navega ML con Playwright
   → detecta producto con botón "Compartir" y ≥50% descuento
   → hace clic en Compartir → extrae link meli.la
   → Claude evalúa: buena/dudosa/sospechosa
   → guarda en data/offers.json

2. ofertas-diversidad-bridge (cada 60s)
   → lee offers.json
   → importa al hub_outbox.json (step_current="diversidad_global")

3. ofertas-detector (outbox worker, cada 5s)
   → toma items del outbox
   → BYPASS validación API (IDs de 8 dígitos)
   → evalúa con Claude AI
   → si pasa → agrega a pending_production_candidates

4. ofertas-detector (dispatch, cada 5 min día / 2h noche)
   → toma primer candidato de la cola
   → revalida descuento via meli.la (HTTP, sin cookies)
   → si descuento ≥50% → envía al grupo WhatsApp
   → si descuento <50% → descarta, pasa al siguiente
```

---

## Sistemas de Aprendizaje Autónomo

### MemoryStore
- Persiste historial de evaluaciones en `data/memory.json`
- Permite feedback loop para mejorar consistencia

### DegradationMonitor
- Detecta cuando los selectores CSS de ML dejan de funcionar
- Umbral: 50% de fallos en ventana de 30 páginas

### DomHealer
- Cuando detecta degradación, llama a Claude para proponer selectores CSS actualizados
- Parchea `price_parser.py` automáticamente
- Ejecuta `systemctl restart bot-diversidad-global`

### CookieInboxWatcher
- Hace polling a Evolution API cada 10 segundos cuando las cookies expiran
- Acepta JSON de cookies enviado por WhatsApp desde el número admin
- Aplica cookies en <10 segundos y reinicia el servicio

---

## Exploración Infinita de ML

- **Seeds:** 126 URLs cubriendo todas las categorías de ML MX (`config/seeds.json`)
- **Auto-paginación:** hasta 15 páginas por listado
- **Re-seed automático:** cuando el frontier cae por debajo de 50 URLs
- **URLs bloqueadas:** `listado.mercadolibre.com.mx` (403), páginas de cuenta, login, ayuda

---

## Estadísticas al 25 Mayo 2026

| Métrica | Valor |
|---|---|
| Ofertas en offers.json | 328 |
| Páginas visitadas (total) | 6,100+ |
| Productos evaluados (total) | 3,746+ |
| Velocidad | ~19 ofertas/hora |
| Items en cola de despacho | ~23 |
| Items enviados al grupo | 28+ |
| Outbox pendientes de evaluación | ~270 |

---

## Comandos Útiles de Diagnóstico

```bash
# Ver estado de todos los servicios
systemctl status bot-diversidad-global ofertas-diversidad-bridge ofertas-detector

# Ver cola de despacho
python3 -c "import json; op=json.load(open('/opt/ofertas-detector-vps/data/state/operational_state.json')); q=op.get('pending_production_candidates',[]); print(f'Cola: {len(q)}')"

# Ver últimos despachos
journalctl -u ofertas-detector --no-pager -n 20 | grep "Production dispatch"

# Ver actividad del bot
journalctl -u bot-diversidad-global --no-pager -n 20 | grep -E "(label=|affiliate|expiry)"

# Exportar cookies manualmente
python3 /opt/ofertas-detector-vps/scripts/export_profile_cookies.py

# Reiniciar todo
systemctl restart bot-diversidad-global ofertas-diversidad-bridge ofertas-detector
```

---

## Notas Importantes

1. **No usar `launch_persistent_context` en el bot** — el perfil de Chromium solo admite un proceso a la vez. El `ofertas-detector` ya no usa Playwright pero si algún día lo vuelve a usar, habrá conflicto.

2. **`listado.mercadolibre.com.mx` da 403** desde la IP del VPS — solo usar `www.mercadolibre.com.mx`.

3. **IDs de 8 dígitos son product group IDs** — no item IDs. La API pública de ML no los reconoce. El pipeline los bypasea.

4. **La revalidación usa meli.la** — no necesita cookies. La página de perfil de afiliado es pública y muestra el descuento en el badge HTML.

5. **El cron de cookies corre cada 10 min** — si el VPS se reinicia, el cron se restaura automáticamente porque está en el crontab del usuario `ofertasrdp`.

6. **Horario noche Monterrey = UTC-6** — 23:00 MX = 05:00 UTC del día siguiente.
