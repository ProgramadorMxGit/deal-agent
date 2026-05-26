# False price-error training examples

Negative examples used by the bot to learn what is **NOT** a price error.

Format: JSON Lines (`*.jsonl`), one example per line.

Schema:

```json
{
  "title": "<product title>",
  "price": <number>,
  "marketplace": "amazon" | "mercadolibre" | ...,
  "reason": "<short tag describing why it's a false positive>",
  "expected_classification": "no_price_error_or_normal_offer",
  "should_publish_as_price_error": false
}
```

## Files

- `generic_chargers.jsonl` — accesorios genéricos (cargadores, cables) para
  iPhone que el bot publicó por error como ERROR DE PRECIO con confianza
  `medium` (mayo 2026). Razón: el matcher `_fingerprint_smartphone` aceptaba
  cualquier título con la palabra "iphone" y combinaba con la regla
  `smartphone_below_500_extreme`. La corrección añade `is_generic_accessory`
  + `mentions_compatible_with_premium` en `accessory_detector.py`.

## Cómo se usan

El módulo `intelligence/accessory_detector.py` codifica las heurísticas;
estos archivos sirven como referencia humana y como dataset para tests
parametrizados (ver `tests/unit/intelligence/test_accessory_detector.py`).
