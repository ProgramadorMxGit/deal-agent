"""Prueba la conexión con Telegram y envía una oferta de ejemplo."""
import asyncio, sys
sys.path.insert(0, ".")

async def main():
    from telegram_sender import test_connection, send_offer_async, send_startup_message

    print("1. Probando conexión con bot de Telegram...")
    ok = await test_connection()
    if not ok:
        print("❌ No se pudo conectar al bot. Verifica el token.")
        return

    print("✅ Bot conectado")

    print("\n2. Enviando mensaje de inicio...")
    await send_startup_message()
    print("✅ Mensaje de inicio enviado")

    print("\n3. Enviando oferta de prueba...")
    test_offer = {
        "asin": "B0D5RKB95G",
        "title": "ADIDAS Vibes Happy Feels, Eau de Parfum, Fragancia Unisex Floral, con Kiwi, Toronja y Jazmín Sambac, 100ML",
        "url": "https://www.amazon.com.mx/dp/B0D5RKB95G",
        "price_current": 399.0,
        "price_original": 890.0,
        "discount_percent": 55,
        "category": "Belleza",
        "rating": 4.3,
        "reviews": 127,
        "image_url": "https://m.media-amazon.com/images/I/71example.jpg",
    }

    result = await send_offer_async(test_offer)
    if result["ok"]:
        print(f"✅ Oferta enviada a Telegram (método: {result['method']})")
    else:
        print(f"❌ Error: {result['error']}")
        # Intentar sin imagen
        test_offer["image_url"] = ""
        result2 = await send_offer_async(test_offer)
        if result2["ok"]:
            print(f"✅ Enviado sin imagen (método: {result2['method']})")
        else:
            print(f"❌ Error sin imagen: {result2['error']}")

    print("\n✅ Test completado")
    print("\nComando para ejecutar el agente:")
    print('kiro-cli chat --agent amazon-hunter --trust-all-tools "Inicia caza de ofertas autonoma en Amazon MX. Busca ofertas >= 50% y envialas a Telegram."')

asyncio.run(main())
