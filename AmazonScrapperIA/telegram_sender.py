"""
telegram_sender.py — Envía ofertas de Amazon al bot de Telegram.

Formato confirmado funcionando:
  [foto del producto]
  *Título*

  🔥 *X% de descuento*
  ❌ Antes: $ORIGINAL
  ✅ *AHORA: $ACTUAL MXN*

  👉 *Ver oferta:*
  https://www.amazon.com.mx/dp/ASIN
"""
import asyncio
import json
import logging
from pathlib import Path

import httpx

logger = logging.getLogger("telegram_sender")

BOT_TOKEN = "8761858664:AAHwTNtxHe3LY0_ZFx9osoWd1QS4HOs57JA"
CHAT_ID   = "5054325626"
BASE_URL  = f"https://api.telegram.org/bot{BOT_TOKEN}"

SENT_FILE = Path(__file__).parent / "data" / "telegram_sent.json"


def _load_sent() -> set:
    if SENT_FILE.exists():
        try:
            return set(json.loads(SENT_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def _mark_sent(asin: str):
    sent = _load_sent()
    sent.add(asin)
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENT_FILE.write_text(json.dumps(list(sent), ensure_ascii=False), encoding="utf-8")


def _safe_md(text: str) -> str:
    """Escapa solo los caracteres que rompen Markdown v1 de Telegram."""
    # En Markdown v1 solo * _ ` [ son especiales — escapar solo si no son parte del formato
    # Para títulos de productos lo más seguro es no escapar nada y dejar el texto limpio
    return text.replace("*", "").replace("_", " ").replace("`", "").replace("[", "(").replace("]", ")")


def _fmt_price(price) -> str:
    try:
        p = float(price)
        return f"${p:,.0f}"
    except Exception:
        return str(price)


def _build_caption(offer: dict) -> str:
    """Construye el caption exacto del formato de la imagen de referencia."""
    title    = _safe_md(offer.get("title", "Producto Amazon"))
    discount = offer.get("discount_percent", 0)
    price    = offer.get("price_current")
    original = offer.get("price_original")
    asin     = offer.get("asin", "")
    url      = f"https://www.amazon.com.mx/dp/{asin}" if asin else offer.get("url", "")
    rating   = offer.get("rating")
    reviews  = offer.get("reviews")
    category = offer.get("category", "")

    lines = [f"*{title}*", ""]
    lines.append(f"🔥 *{discount}% de descuento*")
    if original:
        lines.append(f"❌ Antes: {_fmt_price(original)}")
    lines.append(f"✅ *AHORA: {_fmt_price(price)} MXN*")

    if rating and reviews:
        lines.append(f"⭐ {rating}/5 ({reviews:,} reseñas)")
    elif rating:
        lines.append(f"⭐ {rating}/5")

    if category:
        lines.append(f"📂 {category}")

    lines.extend(["", "👉 *Ver oferta:*", url])
    return "\n".join(lines)


async def send_offer_async(offer: dict) -> dict:
    """
    Envía una oferta a Telegram.
    Intenta sendPhoto primero, fallback a sendMessage con link preview.
    """
    asin      = offer.get("asin", "")
    image_url = offer.get("image_url", "") or offer.get("image", "")
    caption   = _build_caption(offer)

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Intentar con foto
        if image_url and image_url.startswith("http"):
            try:
                r = await client.post(f"{BASE_URL}/sendPhoto", data={
                    "chat_id": CHAT_ID,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "Markdown",
                })
                data = r.json()
                if data.get("ok"):
                    if asin:
                        _mark_sent(asin)
                    return {"ok": True, "method": "photo", "error": None}
                logger.warning(f"sendPhoto falló ({data.get('description')}) — intentando texto")
            except Exception as e:
                logger.warning(f"sendPhoto excepción: {e}")

        # 2. Fallback: sendMessage (muestra preview de imagen desde la URL de Amazon)
        try:
            r = await client.post(f"{BASE_URL}/sendMessage", data={
                "chat_id": CHAT_ID,
                "text": caption,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "false",
            })
            data = r.json()
            if data.get("ok"):
                if asin:
                    _mark_sent(asin)
                return {"ok": True, "method": "text", "error": None}
            err = data.get("description", "Error desconocido")
            logger.error(f"sendMessage falló: {err}")

            # 3. Último fallback: sin parse_mode
            caption_plain = caption.replace("*", "").replace("_", " ")
            r2 = await client.post(f"{BASE_URL}/sendMessage", data={
                "chat_id": CHAT_ID,
                "text": caption_plain,
                "disable_web_page_preview": "false",
            })
            data2 = r2.json()
            if data2.get("ok"):
                if asin:
                    _mark_sent(asin)
                return {"ok": True, "method": "plain_text", "error": None}
            return {"ok": False, "method": "plain_text", "error": data2.get("description")}

        except Exception as e:
            return {"ok": False, "method": "text", "error": str(e)}


def send_offer(offer: dict) -> dict:
    """Versión síncrona."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, send_offer_async(offer))
                return future.result(timeout=35)
        else:
            return loop.run_until_complete(send_offer_async(offer))
    except Exception:
        return asyncio.run(send_offer_async(offer))


def is_already_sent(asin: str) -> bool:
    return asin in _load_sent()


async def test_connection() -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{BASE_URL}/getMe")
            data = r.json()
            if data.get("ok"):
                bot = data["result"]
                logger.info(f"Bot: @{bot.get('username')} ({bot.get('first_name')})")
                return True
        except Exception as e:
            logger.error(f"Error Telegram: {e}")
    return False


async def send_startup_message():
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(f"{BASE_URL}/sendMessage", data={
                "chat_id": CHAT_ID,
                "text": "🤖 *Amazon Offer Hunter activo*\n\nBuscando ofertas ≥50% en Amazon.com.mx...\nTe notificaré cada oferta que encuentre 🛍️",
                "parse_mode": "Markdown",
            })
        except Exception:
            pass
