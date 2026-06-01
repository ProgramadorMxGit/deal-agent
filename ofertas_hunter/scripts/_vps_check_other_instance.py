"""Inspecciona la otra instancia de Evolution (whatsapp-8338498692-v2)
para ver si los mensajes del admin están ahí.
"""
import asyncio
import json
import os
import sys

import httpx


BASE = os.environ.get("EVOLUTION_BASE_URL", "http://162.251.147.177:8080")
KEY = os.environ.get("EVOLUTION_API_KEY", "dev-evolution-api-key")


async def query(instance: str):
    print(f"\n========== Instance: {instance} ==========")
    url = f"{BASE.rstrip('/')}/chat/findMessages/{instance}"
    async with httpx.AsyncClient(timeout=15.0, headers={"apikey": KEY}) as c:
        r = await c.post(
            url,
            json={
                "where": {"key": {"fromMe": False}},
                "limit": 30,
                "order": "desc",
            },
        )
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
        return
    data = r.json() if r.text else {}
    records = data.get("messages", {}).get("records", []) if isinstance(data, dict) else []
    print(f"  Mensajes inbound: {len(records)}")
    senders = {}
    cookies_msgs = []
    for m in records:
        rj = m.get("key", {}).get("remoteJid") or ""
        from_num = "".join(ch for ch in rj.split("@", 1)[0] if ch.isdigit())
        senders[rj] = senders.get(rj, 0) + 1
        msg = m.get("message") or {}
        text = (
            msg.get("conversation")
            or msg.get("extendedTextMessage", {}).get("text")
            or msg.get("imageMessage", {}).get("caption")
            or msg.get("documentMessage", {}).get("caption")
            or ""
        )
        is_doc = "documentMessage" in msg
        if (text and ("/cookies_ml" in text.lower() or "mercadolibre" in text.lower() or '"domain"' in text)) or is_doc:
            cookies_msgs.append({
                "remoteJid": rj,
                "from_num": from_num,
                "ts": m.get("messageTimestamp"),
                "type": m.get("messageType"),
                "is_doc": is_doc,
                "text_preview": text[:140].replace("\n", " | "),
            })
    print(f"  Top remitentes (entrantes):")
    for jid, cnt in sorted(senders.items(), key=lambda x: -x[1])[:10]:
        print(f"    {cnt:>3} {jid}")
    print(f"  Mensajes con cookies/JSON/documento: {len(cookies_msgs)}")
    for cm in cookies_msgs:
        print(f"    - {cm['ts']} {cm['type']} from={cm['from_num']}: {cm['text_preview']!r}")


async def main():
    # Las dos instancias conocidas
    for inst in ("whatsapp-8338498692-v2", "mi-instancia"):
        try:
            await query(inst)
        except Exception as e:
            print(f"  ERROR {inst}: {e}")


asyncio.run(main())
