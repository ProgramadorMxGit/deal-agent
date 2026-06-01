"""Integración del scraper legacy `AmazonScrapperIA` como hunter Amazon
del bot `ofertas_hunter`.

El legacy es robusto contra captchas (browser efímero + UA random + stealth +
delays gaussianos + comportamiento humano). Esta carpeta lo encapsula y
provee un adapter (`adapter.py`) que convierte sus dicts a los modelos del
bot nuevo (`Product` / `Offer` / `OutboxItem`) reusando el `PriceErrorScorer`,
`AccessoryDetector` y todas las salvaguardas anti-falsos-positivos vigentes.

Módulos:
- `browser_worker`: copia adaptada del `BrowserWorker` legacy.
- `price_parser`: extractor HTML legacy (productos + búsquedas).
- `dom_healer`: monitor de degradación + (opcional) heal vía IA.
- `memory_store`: memoria liviana de URLs/ASINs vistos.
- `ai_evaluator`: stub modo `rules`-only por defecto.
- `adapter`: convierte resultados legacy → modelos `ofertas_hunter`.

El hunter público se llama `LegacyAmazonHunterAgent` y vive en
`ofertas_hunter.agents.legacy_amazon_hunter_agent`. Esa clase es la que
debe usarse desde el orquestador cuando `settings.amazon_hunter_legacy=True`.
"""
