# 🛒 Amazon.com.mx Offer Hunter con IA

Scraper autónomo que navega Amazon México buscando ofertas ≥50% de descuento,
usando **kiro-cli (Claude Sonnet 4.6)** como cerebro IA para:

- Evaluar si los descuentos son genuinos
- Sanar selectores CSS cuando Amazon cambia su DOM
- Aprender qué categorías tienen más ofertas
- Generar estrategias de búsqueda mejoradas

---

## 🚀 Inicio Rápido

```bash
# Probar que todo funciona
py test_scraper.py

# Ejecutar scraper (sesión única)
py run_with_kiro.py

# Modo continuo (loop infinito)
py run_with_kiro.py --continuous

# Ver ofertas encontradas
py run_with_kiro.py --report

# Pedir estrategia mejorada a la IA
py run_with_kiro.py --get-strategy

# Analizar DOM de una URL específica
py run_with_kiro.py --analyze-dom https://www.amazon.com.mx/dp/ASIN
```

---

## ⚙️ Opciones

| Opción | Default | Descripción |
|--------|---------|-------------|
| `--min-discount N` | 50 | Descuento mínimo en % |
| `--max-offers N` | 50 | Máximo ofertas por sesión |
| `--no-headless` | — | Mostrar ventana del browser |
| `--no-ai` | — | Sin evaluación IA |
| `--continuous` | — | Loop infinito |

---

## 🧠 Sistema de Auto-Aprendizaje

### MemoryStore (`data/memory.json`)
- Recuerda qué URLs y ASINs ya fueron visitados
- Aprende qué categorías tienen más ofertas
- Registra historial de evaluaciones IA
- Prioriza seeds con mejor rendimiento histórico

### DegradationMonitor
- Monitorea tasa de fallos de extracción
- Si >40% de páginas fallan → activa DomHealer

### DomHealer
- Captura HTML de muestra cuando los selectores fallan
- Llama a kiro-cli para proponer nuevos selectores CSS
- Actualiza `config/selectors.json` automáticamente

### AIEvaluator
- Evalúa cada oferta con Claude Sonnet 4.6
- Veredictos: EXCELENTE / BUENA / REGULAR / DESCARTAR
- Genera descripción atractiva para compartir
- Cada 5 sesiones genera estrategia de búsqueda mejorada

---

## 📁 Estructura

```
AmazonScrapperIA/
├── scraper.py              # Script principal
├── run_with_kiro.py        # Orquestador con kiro-cli
├── test_scraper.py         # Suite de pruebas
├── config/
│   ├── settings.json       # Configuración general
│   ├── seeds.json          # URLs semilla (40 categorías)
│   └── selectors.json      # Selectores CSS (auto-sanados)
├── src/
│   ├── price_parser.py     # Extracción de precios del DOM
│   ├── browser_worker.py   # Navegador autónomo Playwright
│   ├── dom_healer.py       # Auto-sanado de selectores
│   ├── memory_store.py     # Memoria persistente
│   └── ai_evaluator.py     # Evaluación con kiro-cli
└── data/
    ├── offers.json         # Ofertas encontradas
    ├── state.json          # Estado del crawler
    ├── memory.json         # Memoria de aprendizaje
    └── scraper.log         # Logs detallados
```

---

## 🔧 Configuración (`config/settings.json`)

```json
{
  "min_discount_percent": 50,
  "max_pages_per_seed": 10,
  "headless": true,
  "ai_eval_enabled": true,
  "dom_heal_threshold": 0.4
}
```

---

## 📊 Datos de Salida (`data/offers.json`)

Cada oferta incluye:
```json
{
  "asin": "B0XXXXXXXX",
  "title": "Nombre del producto",
  "price_current": 499.0,
  "price_original": 1299.0,
  "discount_percent": 62,
  "url": "https://www.amazon.com.mx/dp/B0XXXXXXXX",
  "rating": 4.5,
  "reviews": 1234,
  "has_prime": true,
  "category": "Electrónicos",
  "ai_evaluation": {
    "verdict": "EXCELENTE",
    "score": 92,
    "genuine_discount": true,
    "reasoning": "Descuento real verificado, buenas reseñas",
    "emoji": "🎧",
    "short_description": "Audífonos Sony con 62% OFF"
  }
}
```
