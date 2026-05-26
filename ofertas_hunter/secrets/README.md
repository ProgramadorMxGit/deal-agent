# Secrets

Esta carpeta es **gitignored**. Aquí van los archivos sensibles del bot.

## Archivos esperados

### `mercadolibre_cookies.json`

Cookies exportadas con [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
(JSON array). Soporta tanto formato Playwright como formato extension.

Ver `mercadolibre_cookies.example.json` para la estructura.

Para configurar:

1. Ir a `https://www.mercadolibre.com.mx` con sesión iniciada.
2. Exportar cookies como JSON.
3. Guardar como `secrets/mercadolibre_cookies.json` (o configurar
   `MERCADOLIBRE_COOKIES_PATH` en `.env`).

### `telegram.session`

Generada automáticamente por Telethon en el primer login interactivo.
Path por defecto: `secrets/telegram.session`. Configurable con
`TELEGRAM_SESSION_PATH`.

## Cómo NO perder los secrets actuales

- **NO** rotar credenciales automáticamente.
- **NO** eliminar archivos sin respaldo.
- Si vienes del legacy, tus paths cross-repo siguen funcionando (envvars
  `BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH` y `OFERTAS_MELI_BROWSER_COOKIES_PATH`
  se respetan como compatibilidad).
