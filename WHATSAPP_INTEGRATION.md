# WhatsApp Integration — Ofertas Engine

Guía completa para enviar mensajes al grupo de WhatsApp. Cubre tanto el **legacy monolith** (`_legacy/detector_monolith_legacy/main.py`) como el **nuevo engine** (`ofertas-engine`).

---

## Proveedor: Evolution API (self-hosted)

Ambos sistemas usan [Evolution API](https://doc.evolution-api.com/) como puente HTTP hacia WhatsApp. Evolution API corre en el VPS y expone endpoints REST.

---

## Credenciales y configuración

### Legacy monolith (`main.py`)

Las credenciales están hardcodeadas como constantes en `main.py` y también en `runtime_secrets.json`:

```python
# main.py — constantes de conexión
EVOLUTION_BASE_URL        = "http://162.251.147.177:8080"
EVOLUTION_API_KEY         = "dev-evolution-api-key"
EVOLUTION_INSTANCE        = "mi-instancia"

# Grupo destino
WHAPI_TARGET_GROUP_NAME       = "Mejores ofertas 🔥"
WHAPI_TARGET_GROUP_FALLBACK_ID = "120363426569734715@g.us"

# Teléfono para alertas de límite y renovación de cookies
WHAPI_LIMIT_ALERT_PHONE  = "5218338498692"
COOKIE_RENEWAL_PHONE     = "5218338498692"
```

Tokens de WhatsApp en `runtime_secrets.json` (campo `whapi_tokens`, legado de cuando se usaba Whapi.cloud):
```
dcRFOaxJiiWC1U5rMIP0JS4Dwe90T7wP
LuJ5kTJ5py5BCVXXs51hzw7yM7WEUmfm
Go1GaHASRZeq1YAnHrI4MXxE7H6mkXSe
```
> Estos tokens ya no se usan activamente — el sistema migró a Evolution API self-hosted.

### Nuevo engine (`ofertas-engine`)

Las credenciales van en `ofertas-engine/.env`:

```dotenv
EVOLUTION_BASE_URL=http://162.251.147.177:8080
EVOLUTION_API_KEY=dev-evolution-api-key
EVOLUTION_INSTANCE=mi-instancia
OFERTAS_WHATSAPP_GROUP_ID=120363426569734715@g.us
```

---

## Grupo destino

| Campo | Valor |
|---|---|
| Nombre | `Mejores ofertas 🔥` |
| JID (Group ID) | `120363426569734715@g.us` |

---

## Cómo funciona el envío (legacy)

La clase `WhapiClient` en `main.py` (línea ~5344) encapsula toda la lógica:

```python
class WhapiClient:
    # Inicialización — usa las constantes EVOLUTION_* del módulo
    def __init__(self, state): ...

    # Enviar texto a cualquier JID (grupo o teléfono)
    def send_text(self, to: str, body: str) -> bool:
        payload = {"number": to, "text": body}
        response = self._post("/message/sendText", payload, timeout=25)
        # HTTP 200 o 201 = éxito

    # Enviar imagen con caption
    def send_image(self, to, media, caption="", mime_type=None) -> bool:
        # media puede ser URL pública, data-url o base64 puro
        payload = {"number": to, "mediatype": "image", "mimetype": ...,
                   "media": raw_base64, "fileName": "oferta.jpg", "caption": caption}
        response = self._post("/message/sendMedia", payload, timeout=40)

    # Atajos para el grupo principal
    def send_group_message(self, body: str) -> bool:
        return self.send_text(WHAPI_TARGET_GROUP_FALLBACK_ID, body)

    def send_group_image(self, image_path: str, caption: str = "") -> bool:
        # Codifica el archivo como base64 y llama send_image al grupo

    # Alerta al teléfono personal (no al grupo)
    def send_limit_alert(self, body: str) -> bool:
        return self.send_text(WHAPI_LIMIT_ALERT_PHONE, body)
```

### Flujo de envío de una oferta (legacy)

```
OfferDetector.send_group_alert(parsed, result)
  │
  ├─ Si hay imagen → whapi.send_group_image(image_path, caption=body)
  │    └─ POST /message/sendMedia/{instance}
  │
  └─ Si no hay imagen → whapi.send_group_message(body)
       └─ POST /message/sendText/{instance}
            body: {"number": "120363426569734715@g.us", "text": "..."}
```

---

## Endpoints HTTP que se llaman

### Enviar texto
```
POST http://162.251.147.177:8080/message/sendText/mi-instancia
Headers:
  Content-Type: application/json
  apikey: dev-evolution-api-key

Body:
{
  "number": "120363426569734715@g.us",
  "text": "🔥 Oferta detectada\n..."
}
```

### Enviar imagen
```
POST http://162.251.147.177:8080/message/sendMedia/mi-instancia
Headers:
  Content-Type: application/json
  apikey: dev-evolution-api-key

Body:
{
  "number": "120363426569734715@g.us",
  "mediatype": "image",
  "mimetype": "image/jpeg",
  "media": "<base64 o URL>",
  "fileName": "oferta.jpg",
  "caption": "🔥 Oferta detectada\n..."
}
```

---

## Prueba manual con curl

```bash
# Texto simple al grupo
curl -X POST "http://162.251.147.177:8080/message/sendText/mi-instancia" \
  -H "Content-Type: application/json" \
  -H "apikey: dev-evolution-api-key" \
  -d '{
    "number": "120363426569734715@g.us",
    "text": "🔥 Prueba de mensaje desde ofertas-engine"
  }'

# Mensaje a teléfono personal (alertas)
curl -X POST "http://162.251.147.177:8080/message/sendText/mi-instancia" \
  -H "Content-Type: application/json" \
  -H "apikey: dev-evolution-api-key" \
  -d '{
    "number": "5218338498692",
    "text": "Prueba de alerta personal"
  }'
```

Respuesta exitosa: HTTP 200 o 201 con JSON de confirmación de Evolution API.

---

## Activar el envío real en el nuevo engine

Por defecto el nuevo engine está en **dry-run**. Para activarlo edita `ofertas-engine/config/settings.yaml`:

```yaml
publishing:
  enabled: true      # ← cambiar de false a true
  dry_run: false     # ← cambiar de true a false
  channel: whatsapp
```

---

## Archivos relevantes

| Archivo | Rol |
|---|---|
| `_legacy/detector_monolith_legacy/main.py` | Clase `WhapiClient` (línea ~5344) — lógica completa de envío |
| `_legacy/detector_monolith_legacy/runtime_secrets.json` | Credenciales del legacy (tokens, API keys) |
| `ofertas-engine/src/ofertas/publishing/whatsapp_evolution.py` | Publisher del nuevo engine |
| `ofertas-engine/src/ofertas/publishing/formatter.py` | Formatea el texto del mensaje |
| `ofertas-engine/config/settings.yaml` → `publishing.whatsapp` | Config del proveedor |
| `ofertas-engine/.env` | Variables de entorno del nuevo engine |

---

## Troubleshooting

| Síntoma | Causa probable | Solución |
|---|---|---|
| HTTP 401 | API key incorrecta | Verificar `EVOLUTION_API_KEY` / header `apikey` |
| HTTP 404 | Instancia no existe o path incorrecto | Verificar `EVOLUTION_INSTANCE` en Evolution API |
| HTTP 422 | Número/JID mal formateado | El grupo debe terminar en `@g.us`, teléfonos en formato `521XXXXXXXXXX` |
| Mensaje no llega al grupo | Group ID incorrecto | Usar `GET /group/fetchAllGroups/{instance}` para obtener el JID correcto |
| `[DRY-RUN]` en logs (nuevo engine) | `dry_run: true` en settings | Cambiar a `dry_run: false` |
| `publishing_disabled` (nuevo engine) | `enabled: false` en settings | Cambiar a `enabled: true` |
| Conexión rechazada | Evolution API caída o IP/puerto incorrecto | Verificar que el servicio corre en `162.251.147.177:8080` |
