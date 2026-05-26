"""Script de login interactivo para Telegram (Telethon).

Genera el archivo de sesión en secrets/telegram.session.
Solo necesitas correrlo UNA VEZ. Después el bot usa la sesión guardada
automáticamente sin volver a pedir credenciales.

Uso:
    .\.venv\Scripts\python.exe scripts\telegram_login.py
"""

import asyncio
import sys
from pathlib import Path

# Asegurar que el paquete del proyecto esté en el path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


API_ID   = 22295300
API_HASH = "611f70b50f4c98de216e7bf3c83f0b7a"
SESSION  = str(Path(__file__).resolve().parents[1] / "secrets" / "telegram")


async def main():
    print("=" * 60)
    print("Login interactivo de Telegram para ofertas_hunter")
    print("=" * 60)
    print(f"Sesión se guardará en: {SESSION}.session")
    print()

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"✅ Ya estás logueado como: {me.first_name} (@{me.username})")
        print("No necesitas hacer nada más.")
        await client.disconnect()
        return

    print("Ingresa tu número de teléfono (formato internacional, ej: +521234567890):")
    phone = input("Teléfono: ").strip()

    await client.send_code_request(phone)
    print()
    print("📱 Te llegará un código por Telegram (o SMS si no tienes la app).")
    print("⚠️  Tienes ~2 minutos para ingresarlo. Si expira, vuelve a correr el script.")
    print()
    code = input("Código (solo los números, sin espacios): ").strip().replace(" ", "")

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        print()
        print("🔐 Tu cuenta tiene verificación en dos pasos activada.")
        password = input("Contraseña 2FA: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    print()
    print(f"✅ Login exitoso como: {me.first_name} (@{me.username})")
    print(f"Sesión guardada en: {SESSION}.session")
    print()
    print("Ahora puedes activar Telegram en .env:")
    print("  TELEGRAM_ENABLED=true")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
