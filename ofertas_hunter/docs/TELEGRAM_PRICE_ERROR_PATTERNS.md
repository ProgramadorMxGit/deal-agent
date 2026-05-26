# Patrones de Telegram para errores de precio

> Patrones de lenguaje y emojis que se usan como **señal**, no como verdad.
> El bot siempre intenta validar la página real antes de publicar.

## 1. Términos de urgencia / error de precio

Listas usadas por `telegram.signal_extractor.UrgencySignals`. La detección es
**case-insensitive** y permite repeticiones (`AAA+`, `OOO+`).

### 1.1 Señales fuertes (score +25)

```
ERROR DE PRECIO
ERROR DE PRECIOOO
ERROR DE PRECIO???
POSIBLE ERROR DE PRECIO
OTRO ERROR DE PRECIO
```

Implementación regex (Python):

```regex
\bERROR\s+DE\s+PRECIO+\b[\?\!]*
\bPOSIBLE\s+ERROR\s+DE\s+PRECIO\b
\bOTRO\s+ERROR\s+DE\s+PRECIO\b
```

### 1.2 Señales medias (score +10)

```
CORRAN
CORRAAAAAN
DEJA PEDIR
NO CANCELAN
EN SOLO
A SOLO
SALE EN
PRECIO MAS BAJO
```

Implementación regex:

```regex
\bCORR[A]+N\b
\bDEJA\s+PEDIR\b
\bNO\s+CANCELAN\b
\b(EN|A)\s+SOLO\b
\bSALE\s+EN\b
\bPRECIO\s+M[AÁ]S\s+BAJO\b
```

### 1.3 Señales débiles (score +5)

- Mensajes con >= 60% de mayúsculas (excluye texto entrecomillado).
- Mensajes con >= 3 emojis de urgencia.
- Mensajes con `!!!` o `???`.
- Hashtags `#error`, `#errordeprecio`, `#precio_bajo`, `#oferta_extrema`.

### 1.4 Descuentos visibles

- `91% DE DESCUENTO`, `-91%`, `91% off` → suma directa al
  `category_anomaly_score` y `discount_percent`.
- Cualquier porcentaje >= 80% → señal fuerte.
- Cualquier porcentaje >= 70% → señal media.

## 2. Emojis de urgencia

```
🚨 🔥 ‼️ ⚡ 😱 🤯 💥 🏃 ⏳ ⏰
```

Implementación: `re.compile(r"[🚨🔥‼⚡😱🤯💥🏃⏳⏰]")`. >= 3 ocurrencias en
`message.text` → bonus +5.

## 3. Tiendas mencionadas

El parser detecta menciones de tiendas en el texto para asignar `marketplace`:

| Mención (case-insensitive)                    | marketplace            |
|-----------------------------------------------|------------------------|
| `amazon`, `amzn`, `amazon mexico`             | `amazon`               |
| `mercado libre`, `meli`, `ML `                | `mercadolibre`*        |
| `walmart`                                     | `walmart`              |
| `sam's club`, `sams`, `sams club`             | `sams_club`            |
| `coppel`                                      | `coppel`               |
| `office depot`                                | `office_depot`         |
| `liverpool`                                   | `liverpool`            |
| `sears`                                       | `sears`                |
| `dell`                                        | `dell`                 |
| `sony store`                                  | `sony_store`           |
| `costco`                                      | `costco`               |
| `bestbuy`, `best buy`                         | `bestbuy`              |
| `linio`, `chedraui`                           | `other`                |

*\* Si `marketplace == "mercadolibre"` y `source == "telegram"`, el mensaje se
**ignora** por regla actual (ver `RULES.md §6`).*

## 4. Detección de links

`signal_extractor.extract_links(text)` extrae:

1. Links absolutos `http(s)://...`
2. Acortadores comunes:
   - `bit.ly/...`, `goo.gl/...`, `tinyurl.com/...`, `t.co/...`
   - `meli.la/...` (afiliado ML — se ignora desde Telegram)
   - `amzn.to/...`, `amzn.mx/...`
3. Links sin `http://` (autocompleta `https://`).

Para cada link se llama a `extraction.url_resolver.resolve(url)`:
- HTTP HEAD primero, GET sólo si HEAD no funciona.
- Sigue redirects hasta 5 saltos.
- Cachea en `resolved_urls(original_url, final_url)`.
- Devuelve `final_url`, `http_status`.

## 5. Categorización por palabras del título

El parser intenta inferir categoría desde el título del mensaje:

| Keywords del título                                          | Categoría |
|--------------------------------------------------------------|-----------|
| `laptop`, `notebook`, `macbook`, `gaming laptop`             | `laptop`  |
| `iphone`, `galaxy`, `xiaomi redmi`, `motorola`, `smartphone`, `celular` | `smartphone` |
| `ipad`, `tablet`, `lenovo tab`, `samsung tab`                | `tablet`  |
| `airpods`, `sony wf`, `bose qc`, `audífonos`, `headphones`   | `audio_premium` |
| `monitor`, `tv`, `smart tv`, `pantalla`                      | `display` |
| `playstation`, `ps5`, `xbox`, `nintendo`, `consola`          | `console` |
| `motherboard`, `gpu`, `rtx`, `ryzen`, `intel core`, `ssd`, `ram` | `pc_components` |
| `lavadora`, `refrigerador`, `licuadora`, `freidora`          | `home_appliance` |
| `camisa`, `playera`, `tenis`, `zapatos`, `ropa`              | `apparel` |

Si no matchea, `category = "uncategorized"`.

## 6. Marca

Lista mínima: `apple`, `samsung`, `sony`, `dell`, `hp`, `lenovo`, `asus`, `msi`,
`lg`, `huawei`, `xiaomi`, `motorola`, `nintendo`, `microsoft`, `amazon`,
`bose`, `jbl`, `acer`, `gigabyte`, `corsair`, `kingston`, `seagate`, `wd`.

Detector simple: la primera marca encontrada en el título (case-insensitive).

## 7. Imagen del mensaje

- Telethon descarga la foto del mensaje a `data/telegram_images/<channel>/<msg_id>.jpg`.
- Se guarda el path en `telegram_messages.image_path`.
- `image_resolver` la usa como fallback si no consigue imagen del retailer.

## 8. Backfill al arrancar

- Por cada canal: pedir últimos `TELEGRAM_BACKFILL_LIMIT_PER_CHANNEL` mensajes
  no procesados.
- Saltar mensajes ya en `telegram_messages` (clave `(channel, message_id)`).
- Procesar en orden cronológico.

## 9. Output del parser

```python
@dataclass
class ParsedTelegramMessage:
    channel: str
    message_id: int
    captured_at: datetime
    text: str
    title_guess: Optional[str]      # primera línea sin emojis
    marketplace: Optional[str]
    store_mention: Optional[str]
    brand: Optional[str]
    category: Optional[str]
    written_price: Optional[float]
    discount_visible: Optional[float]
    urgency_terms: list[str]
    urgency_score: int              # 0..40
    is_price_error_keyword: bool
    original_url: Optional[str]
    resolved_url: Optional[str]     # tras url_resolver
    image_path: Optional[str]
    skip_reason: Optional[str]      # "mercadolibre_link" si aplica
```

`skip_reason == "mercadolibre_link"` indica que el listener debe descartar el
candidato (no enviarlo a `price_intelligence`).

## 10. Ejemplos reales

### 10.1 ERROR DE PRECIO laptop Walmart

```
ERROR DE PRECIO???
Laptop HP EliteBook 840 G6 Intel Core i7 32GB RAM 512GB SSD
Walmart - $2,349
🚨🔥 CORRAN!!!
https://bit.ly/abc123
```

Detección esperada:
- `urgency_terms = ["ERROR DE PRECIO", "CORRAN"]`
- `urgency_score = 35`
- `is_price_error_keyword = True`
- `marketplace = "walmart"`
- `brand = "hp"`
- `category = "laptop"`
- `written_price = 2349.0`
- `original_url = "https://bit.ly/abc123"` → resuelve.

### 10.2 Galaxy S24 a $1,399 en Sears

```
🔥 OTRO ERROR DE PRECIO 🔥
Samsung Galaxy S24 256GB
Sears - $1,399
DEJA PEDIR! ⚡⚡⚡
```

Detección esperada:
- `urgency_terms = ["OTRO ERROR DE PRECIO", "DEJA PEDIR"]`
- `urgency_score = 35`
- `marketplace = "sears"`
- `brand = "samsung"`
- `category = "smartphone"`
- `written_price = 1399.0`

### 10.3 Mercado Libre desde Telegram (se ignora)

```
Tablet Samsung Galaxy Tab A9
Mercado Libre - $2,499 con cupón
https://meli.la/xyz
```

Detección esperada:
- `marketplace = "mercadolibre"`
- `skip_reason = "mercadolibre_link"`
