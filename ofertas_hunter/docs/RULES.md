# Reglas de negocio

> Reglas duras que el sistema **no** puede violar.

## 1. Oferta normal publicable

Una oferta normal se publica si y sólo si:

- Tiene **imagen** verificable y descargable.
- Tiene **link válido** (resuelve a producto vivo).
- Tiene **precio actual** confiable (extraído del DOM y verificado).
- Tiene **precio anterior** o histórico propio que justifique
  el descuento.
- Tiene **stock** o posibilidad real de compra (no "no disponible").
- El **descuento real** es ≥ 50%.

**Nunca publicar si:**
- No hay imagen.
- El precio no es verificable.
- El link redirige a una página de error o producto distinto.
- Es una variación cara cuyo título no coincide.
- Es solo una mensualidad presentada como precio total.
- Es un precio dependiente de cupón no comprobable.

## 2. Errores de precio

Ver `PRICE_ERROR_DETECTION.md` para la lógica completa de scoring.

**Reglas duras:**
- Tienen **prioridad máxima**.
- **Saltan el cooldown** global de 5 minutos.
- Pueden publicarse inmediatamente si imagen + link + precio están confirmados.
- Si la confianza es alta pero hay duda → marcar como `possible_price_error`,
  revalidar una sola vez, publicar si sigue vivo.
- Si la confianza es media → guardar como `suspicious_deal`, no publicar.

## 3. Productos sin descuento visible

- Primera vez detectado: **no publicar**.
- Guardar en `products` + `price_observations` (histórico).
- Si en futuras observaciones el precio cae fuerte (>= 30% bajo el median),
  re-evaluar como oferta o error de precio.
- Guardar incluso productos sin descuento si pertenecen a categorías de alto
  valor: `laptop`, `smartphone`, `tablet`, `apple`, `samsung`, `sony`, `dell`,
  `hp`, `lenovo`, `asus`, `msi`, `pc_components`, `consoles`, `audio_premium`.

## 4. Usados / reacondicionados

- Por defecto **no** se publican.
- Sólo se consideran si el descuento es **extremo** (>= 70%) y el score
  cualitativo lo aprueba.
- Nunca se publican como "error de precio" sin marcar la condición en el
  mensaje.
- Si hay duda sobre la condición → descartar.

## 5. Mercado Libre

- **Sólo** se procesan productos con botón "Compartir" activo (afiliado).
- El descuento debe estar visible en la página (badge o cálculo desde
  `ui-pdp-price__original-value`).
- El link de afiliado se extrae del modal (`generate_link_button`) y debe
  ser una URL `meli.la/...`.
- **Los links de Mercado Libre que vengan de Telegram se ignoran**
  por regla actual (decisión del operador).

## 6. Telegram

- **Solo escucha** (Telethon), nunca envía.
- Canales activos: los del legacy + lo que esté configurado.
- Reutiliza sesión Telethon existente si está disponible.
- Para cada mensaje:
  - parsear tienda, título, precio escrito, link original, link final,
    texto de urgencia, posible categoría, hashtags, fecha, canal,
    imagen si existe.
  - persistir como `telegram_messages`.
  - emitir candidato a `price_intelligence`.
- **No depender de feedback humano** (`/ok`, `/false`, `/retrain` están
  prohibidos como mecanismo principal).

## 7. WhatsApp

- Toda publicación va vía Evolution API.
- Toda publicación **requiere imagen**.
- **Cooldown global** de 5 minutos para ofertas normales.
- **Bypass** para errores de precio.
- Si hay varias ofertas elegibles → **selección aleatoria**, no FIFO.
- Ofertas con > 1h en outbox → **revalidar** antes de publicar.
- Dispatcher **estrictamente serializado**.
- Cada intento queda registrado en `published_messages` (éxito, fallo,
  respuesta de Evolution).

## 8. Persistencia

- **SQLite con WAL** es la única fuente de verdad.
- JSON sólo para export/debug, nunca para estado crítico.
- Al reiniciar, **continuar desde checkpoints** sin pérdida.
- Ofertas pendientes con > 1h se revalidan al arrancar.

## 9. Auto-healing

- Snapshots del DOM se guardan en `dom_snapshots`.
- Cambios de selectores se versionan en `selector_versions`.
- **Antes de aplicar** un patch de selector, los tests del fixture deben
  pasar.
- Si no puede sanar con confianza → degrada el agente afectado, no borra
  datos ni memoria.
- Patches de código se registran en `self_patches` con el diff y los tests.

## 10. Memoria

- Tablas estructuradas (ver `SCHEMA.md`).
- Compresión automática por tamaño y relevancia.
- Resúmenes en `memory_summaries`.
- Aprendizaje desde:
  - revalidaciones exitosas
  - ofertas expiradas
  - precios históricos propios
  - diferencias entre precio publicado y revalidado
  - errores de extracción
  - cambios de DOM
  - patrones de Telegram
  - categorías con anomalías repetidas

## 11. Seguridad operativa

- **No rotar credenciales automáticamente.** Sólo el operador.
- **No eliminar credenciales existentes.**
- Mover lectura de secretos a `.env`/config, sin invalidar valores
  actuales.
- Rate limits, backoff, jitter, UA rotation, límites de concurrencia.
- No evasión agresiva ni comportamiento destructivo.
- No quemar cookies de Mercado Libre.
- Si detecta bloqueo → reducir velocidad, pausar agente, registrar
  evento.

## 12. Formatos de mensaje WhatsApp

### 12.1 Oferta normal

```
*{title}*

🔥 *{discount_percent}% de descuento*
❌ Antes: ~${previous_price}~
✅ *AHORA: ${current_price}*

👉 *Ver oferta:*
{url}
```

Ejemplo:

```
*JBL Tune 510BT - Auriculares in-Ear inalámbricos con Sonido Purebass, Color Azul*

🔥 *57% de descuento*
❌ Antes: ~$899~
✅ *AHORA: $388*

👉 *Ver oferta:*
https://amzn.to/4e3yTjG
```

### 12.2 Error de precio

```
🚨 ERROR DE PRECIO 🚨

*{title}*

🔥 Precio detectado: *${current_price}*
⚡ Confianza: {confidence_label}
🏬 Tienda: {marketplace}

👉 *Ver oferta:*
{url}
```

Donde `confidence_label`:
- `very high` → score ≥ 90
- `high`      → 80 ≤ score < 90
- `medium`    → 60 ≤ score < 80 (sólo `possible_price_error` revalidado)

## 13. Reglas duras del despachador

- No publica oferta normal si han pasado < 5 minutos desde la última.
- Sí publica error de precio inmediatamente.
- No publica si imagen no resuelve.
- No publica si precio no fue validado.
- No publica si URL final no responde.
- Si revalidación falla, marca `expired` con razón y pasa al siguiente.
- Cada acción queda registrada en `published_messages` o
  `discarded_candidates`.

## 14. Restricciones globales

- No reescribir todo sin tests.
- No romper lógica funcional existente del legacy.
- No borrar archivos sin respaldo o decisión documentada.
- No depender de feedback manual humano.
- No publicar sin imagen.
- No publicar sin precio validado.
- No publicar links de Mercado Libre provenientes de Telegram.
- No quemar cookies con scraping agresivo.
- No ignorar errores silenciosamente.
- Todo descarte debe tener razón.
