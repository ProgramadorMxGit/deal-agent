"""
send_pending.py — Envía a Telegram todas las ofertas guardadas que aún no fueron enviadas.
Ejecutar: py send_pending.py
"""
import asyncio, json, sys
sys.path.insert(0, ".")
from pathlib import Path
from telegram_sender import send_offer_async, is_already_sent, _mark_sent

OFERTS_FILE = Path("oferts/oferts.json")

async def main():
    if not OFERTS_FILE.exists():
        print("No hay ofertas guardadas.")
        return

    offers = json.loads(OFERTS_FILE.read_text(encoding="utf-8"))
    pending = [o for o in offers if not o.get("telegram_sent") and not is_already_sent(o.get("asin",""))]

    print(f"Total ofertas: {len(offers)} | Pendientes de enviar: {len(pending)}")

    sent_count = 0
    for i, offer in enumerate(pending, 1):
        title = offer.get("title","")[:50]
        disc  = offer.get("discount_percent",0)
        asin  = offer.get("asin","")
        print(f"\n[{i}/{len(pending)}] {disc}% OFF — {title}")
        print(f"  Imagen: {offer.get('image_url','')[:60]}")

        result = await send_offer_async(offer)
        if result["ok"]:
            print(f"  ✅ Enviado a Telegram (método: {result['method']})")
            # Marcar como enviado en el JSON
            offer["telegram_sent"] = True
            sent_count += 1
        else:
            print(f"  ❌ Error: {result['error']}")

        # Guardar estado actualizado
        OFERTS_FILE.write_text(json.dumps(offers, ensure_ascii=False, indent=2), encoding="utf-8")

        # Pausa entre mensajes para no hacer flood
        if i < len(pending):
            await asyncio.sleep(3)

    print(f"\n✅ Enviadas: {sent_count}/{len(pending)}")

asyncio.run(main())
