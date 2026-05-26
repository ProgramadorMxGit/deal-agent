---
inclusion: fileMatch
fileMatchPattern: "*"
---

# Producto: ofertas_hunter

## Que hace el bot

`ofertas_hunter` es un bot autonomo que detecta ofertas reales (>=50% de descuento)
y errores de precio en Amazon Mexico y Mercado Libre Mexico, y los publica a un
grupo de WhatsApp via Evolution API. Tambien escucha canales de Telegram para
priorizar errores de precio reportados por humanos.

## Que es una oferta publicable

- **normal**: descuento >=50% comprobado contra precio anterior real (no inflado).
- **price_error_confirmed**: score>=80 segun `PriceErrorScorer`. Salta el cooldown.
- **possible_pe**: score 60-79. Requiere QA antes de publicar.
- **suspicious**: score 40-59. No se publica automaticamente.
- **no_error**: score <40. Ignorado.

## Reglas de negocio (inviolables, aplicadas server-side)

1. **Cooldown 5 min** entre publicaciones tipo `normal` (`WHATSAPP_COOLDOWN_SECONDS=300`).
   Errores de precio bypasean cooldown.
2. **Gates duros** para publicar: imagen, precio actual y URL valida deben existir.
3. **ML afiliado obligatorio** (`MERCADOLIBRE_AFFILIATE_REQUIRED_FOR_PUBLISH=true`).
   Sin `affiliate_url`, el item de ML no se publica.
4. **Telegram->ML siempre bloqueado**: links de ML detectados via Telegram se ignoran.
5. **Modo seguro**: `PUBLISHING_DRY_RUN=true` o `PUBLISHING_ENABLED=false` -> dry-run.
6. **Cooldown anti-duplicados** 48h: items publicados en las ultimas 48h no se re-encolan.
7. **Scheduler nocturno**: `hibernating` 23:30-06:30 MX, `warmup` 06:30-07:00 (caza pero no
   publica), `active` 07:00-23:30.

## Marketplaces soportados

- **Amazon Mexico** (amazon_enabled=true): scraping con Playwright headless.
- **Mercado Libre Mexico** (mercadolibre_enabled=true): scraping + extractor de
  link de afiliado via modal "Compartir" (cookies sesion @yohanarteagaescobar,
  comision 9%).
- **Telegram canales**: `ofertonesmexico`, `superofertasm`, `Ofertaspremiummx`.

## Rol de WhatsApp y Telegram

- **WhatsApp**: canal de salida. Grupo `120363426569734715@g.us`. Solo recibe ofertas
  publicables. Formato: imagen + caption (titulo + precios + URL).
- **Telegram**: canal de entrada. Senal de errores de precio reportados por humanos.
  Filtra urgencia ("ERROR DE PRECIO", "CORRAN", "A SOLO", emojis 🚨🔥‼️). Solo procesa
  Amazon/Walmart/Coppel/Sams/Liverpool/OfficeDepot/Sears/Dell/Sony. Links ML bloqueados.

## Anti-patterns (NO repetir del legacy AmazonScrapperIA)

1. **NO una tool por producto**. Las tools MCP son por marketplace (`hunt_amazon(limit=5)`)
   y agregadas. El legacy gastaba tokens por cada item.
2. **NO scoring/parsing en el LLM**. El `PriceErrorScorer` (Python) decide. El LLM solo
   hace QA narrativa.
3. **NO override de Hard_Rules**. Argumentos como `force`, `bypass_schedule`, `override_*`
   fallan con `validation_failed`. Las reglas no consultan args.
4. **NO duplicar logica determinista en el prompt**. Thresholds, formato del mensaje y
   cadena cooldown->gates->publish viven en codigo Python.
