# Detección de errores de precio

> Lógica completa del `PriceErrorScorer` y del flujo de decisión.

## 1. Visión general

Un **error de precio** es un precio publicado por una tienda que está claramente
fuera del rango razonable del producto (por error humano del retailer, fallo de
sistema, dígito faltante, descuento no controlado). El bot intenta capturarlos
porque son la oportunidad más valiosa para el grupo de WhatsApp y deben tener
**prioridad máxima** y **bypass del cooldown** de 5 minutos.

A diferencia de una oferta normal (`>= 50%` descuento publicable), un error de
precio se evalúa con un **score 0-100** que combina varias señales (precio
absoluto, anomalía vs histórico, anomalía vs categoría, urgencia textual,
contexto de marca).

## 2. Clasificación

| Rango score | Clasificación            | Acción del dispatcher                                |
|-------------|--------------------------|------------------------------------------------------|
| `>= 80`     | `price_error_confirmed`  | Publicar inmediato (bypass cooldown) si imagen+precio+link válidos |
| `60..79`    | `possible_price_error`   | Revalidar inmediatamente; publicar si sigue cumpliendo |
| `40..59`    | `suspicious_deal`        | Guardar en watchlist, no publicar                    |
| `< 40`      | `no_price_error`         | Tratar como oferta normal o descartar                |

Los thresholds son configurables (`PRICE_ERROR_THRESHOLD_*` en `.env`).

## 3. Señales positivas (suman score)

| Señal                                                                        | +Puntos |
|------------------------------------------------------------------------------|---------|
| Producto premium con precio < 20% de su rango normal de categoría            | +35     |
| Precio actual < histórico propio del producto en >= 70%                      | +35     |
| Smartphone flagship reciente (iPhone Pro/Galaxy S/S Ultra) < $5,000 MXN      | +35     |
| Descuento visible explícito >= 80%                                           | +30     |
| Laptop nueva por debajo de $4,000 MXN                                        | +30     |
| Combo laptop + accesorios con precio menor a $5,000 MXN                      | +30     |
| Texto de Telegram contiene "error de precio" / "ERROR DE PRECIO"             | +25     |
| iPad / tablet Apple por debajo de $5,000 MXN                                 | +25     |
| Audífonos flagship (AirPods Pro / Sony WF-1000XM) por debajo de $1,000 MXN   | +25     |
| Producto Apple/Samsung/Dell/HP/Lenovo/MSI/Sony/ASUS con precio anormal       | +20     |
| Precio parece faltarle un dígito comparado con el rango de categoría         | +20     |
| Precio actual contradice precio anterior visible (descuento real >= 70%)    | +20     |
| Texto Telegram contiene "corran", "a solo", "en solo", "deja pedir"          | +10     |
| Muchos emojis o urgencia en fuente Telegram (>=3 emojis o `!!`)              | +5      |

## 4. Señales negativas (restan score / hacen no publicable)

| Señal                                                                  | -Puntos / Estado |
|------------------------------------------------------------------------|--------|
| Producto sin imagen                                                    | **NO publicable** |
| Precio no extraíble                                                    | **NO publicable** |
| Producto sin stock                                                     | -50     |
| Precio corresponde a accesorio y no al producto principal              | -40     |
| Variación seleccionada no coincide con la oferta (diff de SKU/talla)   | -35     |
| No se puede validar link final (resolve_url falla)                     | -30     |
| Producto usado / reacondicionado                                       | -25     |
| Precio depende de cupón no comprobable                                 | -15     |
| Marketplace vendedor externo sospechoso                                | -15     |
| Producto genérico o sin marca clara                                    | -10     |

## 5. Rangos heurísticos por categoría (primera aproximación)

Estos son los rangos iniciales del `category_ranges.json`. El bot **aprende
mejores rangos** con su propio histórico (`category_price_ranges` en SQLite).

### Laptops

| Característica                              | Rango sospechoso |
|---------------------------------------------|---|
| Laptop nueva básica                         | < $4,000 MXN    |
| Laptop con i7 / Ryzen 7                     | < $6,000 MXN (muy sospechosa) |
| Laptop gamer con GPU dedicada               | < $8,000 MXN (muy sospechosa) |
| Laptop empresarial 16GB+ RAM + SSD          | < $5,000 MXN (muy sospechosa) |

### Smartphones

| Característica                              | Rango sospechoso |
|---------------------------------------------|---|
| iPhone Pro / Pro Max reciente               | < $8,000 MXN (muy sospechoso) |
| Samsung Galaxy S / S Ultra reciente         | < $7,000 MXN (muy sospechoso) |
| Smartphone funcional de marca reconocida    | < $500 MXN (error extremo)  |

### Tablets

| Característica                              | Rango sospechoso |
|---------------------------------------------|---|
| iPad reciente                               | < $5,000 MXN    |
| iPad 256GB                                  | < $6,000 MXN    |

### Audio premium

| Característica                              | Rango sospechoso |
|---------------------------------------------|---|
| AirPods Pro                                 | < $1,500 MXN    |
| Sony WF-1000XM (4/5)                        | < $1,500 MXN    |

### Componentes PC

| Característica                                    | Rango sospechoso |
|---------------------------------------------------|---|
| Motherboard de marca reconocida                   | precio 50-70% < histórico |
| GPU / CPU / RAM / SSD                             | precio 60% < histórico  |

### Ropa

| Característica                              | Regla |
|---------------------------------------------|---|
| Descuento visible >= 85%                    | Posible oferta extrema; **publicar sólo si precio final, talla/color y variación coinciden**.

## 6. Validación contra falsos errores

El bot **rechaza** publicar cuando detecta:

1. Precio de mensualidad mostrado como precio total (`/mes`, `MSI`, "12 pagos de…").
2. Precio de accesorio en lugar del producto principal.
3. Variación barata seleccionada cuyo título corresponde a variación cara.
4. Producto usado / reacondicionado sin marcar.
5. Producto sin stock.
6. Precio con cupón no aplicado realmente.
7. Descuento mostrado para otra variante.
8. Link acortado que redirige a producto distinto.
9. Marketplace externo dudoso (vendedor sin antigüedad).
10. Scraping incompleto del DOM (selector roto, snapshot vacío).
11. Precio anterior inflado artificialmente (ej. previous = 999, current = 99 ≠ error real).
12. Publicación vieja reciclada en Telegram (resolved_url ya en `published_messages`).

**Antes de publicar**:
- resolver URL final (HTTP HEAD/GET).
- abrir producto con Playwright.
- confirmar título.
- confirmar precio.
- confirmar imagen.
- confirmar tienda.
- confirmar disponibilidad.
- confirmar que no sea sólo mensualidad.
- confirmar que la variación seleccionada coincida.

## 7. Flujo de evaluación

```
candidate
  │
  ▼
extract_signals
  │  (telegram terms, brand keywords, category, price, prev_price,
  │   discount_visible, urgency_emojis, condition, has_stock, has_image,
  │   marketplace_signals, historical_median if any)
  │
  ▼
PriceErrorScorer.score(signals) -> { score: int, reasons: [...] }
  │
  ▼
classify(score) -> classification
  │
  ▼
decide:
   price_error_confirmed → publish_immediately
   possible_price_error  → revalidate_now → re-score → decide
   suspicious_deal       → save_watchlist
   no_price_error        → if discount >= 50% enqueue normal else discard
```

## 8. Ejemplos de los fixtures

Los ejemplos A-M de la spec viven en
`tests/fixtures/price_errors/example_<letter>.json`. Cada fixture contiene los
campos del `PriceErrorSignal` (ver §11), el `score_expected_min`,
`classification_expected` y `confidence_expected`.

| Ejemplo | Producto                                   | Precio observado | Clasificación esperada       |
|---------|--------------------------------------------|---|------------------------------|
| A | Laptop HP EliteBook 840 G6 i7/32/512        | $2,349           | high → `price_error_confirmed` |
| B | Asus Vivobook 16 R7/16/512 + accesorios    | $305.88          | very high → `price_error_confirmed` |
| C | iPad 11" 256GB A16                          | $2,611           | high → `price_error_confirmed` |
| D | Camisa COOFANDY 91% off                    | $56.85           | possible / extreme offer     |
| E | Dell Pro 16 nueva                           | $1,544.70        | high → `price_error_confirmed` |
| F | Sony WF-1000XM5                             | < $1,000         | high → `possible_price_error` |
| G | MSI Thin GF63 i5/8/1TB/RTX2050              | $2,719           | very high → `price_error_confirmed` |
| H | AirPods Pro                                 | $599.90          | high → `price_error_confirmed` |
| I | iPhone 16 Pro Max 256GB                     | $3,899           | very high → `price_error_confirmed` |
| J | Motherboard Gigabyte B450 AORUS M           | $611.62          | medium-high → `possible_price_error` |
| K | Samsung Galaxy S24 256GB                    | $1,399           | very high → `price_error_confirmed` |
| L | Samsung Galaxy A32 128GB                    | $197             | very high → `price_error_confirmed` |
| M | iPad Mini Apple WiFi 64GB                   | $4,999           | medium-high → `possible_price_error` |

## 9. Etiquetas de confianza para WhatsApp

```
score >= 90 → "very high"
80..89    → "high"
60..79    → "medium" (sólo si tras revalidación se publica)
```

Estas etiquetas viajan al template de error de precio (`RULES.md §12.2`).

## 10. Aprendizaje continuo

El bot mejora el scorer sin feedback humano:

- **Categoría → rangos**: cada `price_observation` actualiza
  `category_price_ranges` (median, p10, p90, sample_size).
- **Patrones de Telegram**: si un mensaje con "ERROR DE PRECIO" termina siendo
  precio mensual o accesorio, registra el patrón y reduce el peso del término
  en futuras evaluaciones.
- **Revalidaciones**: si una oferta encolada como `price_error_confirmed`
  desaparece o sube de precio en < 1h, registra el patrón "tienda X tiende a
  corregir errores rápidamente" para subir prioridad.

## 11. Modelo `PriceErrorSignal`

```python
@dataclass
class PriceErrorSignal:
    product_title: str
    marketplace: str          # amazon | mercadolibre | telegram-store-x
    source: str               # "amazon_hunter" | "mercadolibre_hunter" | "telegram"
    source_channel: Optional[str]   # nombre del canal Telegram si aplica
    original_text: Optional[str]
    original_url: str
    resolved_url: Optional[str]
    current_price: Optional[float]
    previous_price: Optional[float]
    historical_median_price: Optional[float]
    category_estimated_range_min: Optional[float]
    category_estimated_range_max: Optional[float]
    discount_percent: Optional[float]
    brand: Optional[str]
    category: Optional[str]
    condition: str = "new"           # new | used | refurbished | unknown
    has_stock: Optional[bool] = None
    has_image: bool = False
    urgency_terms: list[str] = field(default_factory=list)
    telegram_confidence: float = 0.0
    price_anomaly_score: int = 0
    category_anomaly_score: int = 0
    historical_anomaly_score: int = 0
    final_score: int = 0
    classification: str = "no_price_error"
    reasons: list[str] = field(default_factory=list)
    created_at: datetime
    revalidated_at: Optional[datetime] = None
```
