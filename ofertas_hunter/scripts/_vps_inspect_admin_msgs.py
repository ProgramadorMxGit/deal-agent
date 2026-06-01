"""Inspecciona mensajes recientes del admin en Evolution API."""

import asyncio
import json
import os
import sys

import httpx

BASE = os.environ.get("EVOLUTION_BASE_URL", "http://162.251.147.177:8080")
KEY = os.environ.get("EVOLUTION_API_KEY", "dev-evolution-api-key")
INST = os.environ.get("EVOLUTION_INSTANCE", "mi-instancia")
ADMIN = os.environ.get("ML_SESSION_ADMIN_NUMBERS", "528338498692").split(",")[0].strip()


async def main():
    url = f"{BASE.rstrip('/')}/chat/findMessages/{INST}"
    body = {
        "where": {"key": {"fromMe": False}},
        "limit": 50,
        "order": "desc",
    }
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(url, json=body, headers={"apikey": KEY})
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:300]}")
            sys.exit(1)
        data = r.json()
    records = data.get("messages", {}).get("records", []) if isinstance(data, dict) else []
    print(f"Total mensajes inbound recientes (fromMe=false): {len(records)}")
    print(f"Configurado en .env como admin: {ADMIN}")
    print()
    print("=== Top 20 remitentes únicos ===")
    senders = {}
    cookies_msgs = []
    for m in records:
        rj = (m.get("key", {}).get("remoteJid") or "")
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
        if text and "/cookies_ml" in text.lower():
            cookies_msgs.append({
                "remoteJid": rj,
                "from_num": from_num,
                "ts": m.get("messageTimestamp"),
                "text_preview": text[:200].replace("\n", " | "),
                "has_document": "documentMessage" in msg,
            })

    for jid, count in sorted(senders.items(), key=lambda x: -x[1])[:20]:
        from_num = "".join(ch for ch in jid.split("@", 1)[0] if ch.isdigit())
        marker = "  <-- ADMIN configurado" if from_num == ADMIN else ""
        print(f"  {jid:<60} ({count} msgs) num={from_num}{marker}")

    print()
    print(f"=== Mensajes con /cookies_ml encontrados: {len(cookies_msgs)} ===")
    for cm in cookies_msgs:
        print(json.dumps(cm, indent=2, ensure_ascii=False))


asyncio.run(main())
