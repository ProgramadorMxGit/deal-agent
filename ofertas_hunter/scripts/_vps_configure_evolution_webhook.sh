#!/usr/bin/env bash
# Configura el webhook de Evolution API para que envíe mensajes entrantes
# al servidor HTTP del ML Session Recovery (localhost:9099/wa/inbound).
#
# Uso: bash _vps_configure_evolution_webhook.sh

set -uo pipefail

EVOLUTION_URL="http://162.251.147.177:8080"
EVOLUTION_KEY="dev-evolution-api-key"
EVOLUTION_INSTANCE="mi-instancia"
WEBHOOK_URL="http://127.0.0.1:9099/wa/inbound"
WEBHOOK_SECRET="FzmDbaYfmRt-jAk_7MBu7r2nnRKR4IJ0jsn-ejaC21c"

echo "=== Configurando webhook en Evolution API ==="
echo "  Instance: $EVOLUTION_INSTANCE"
echo "  Webhook:  $WEBHOOK_URL"
echo

# Intentar con el endpoint de webhook de Evolution API v2
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  "${EVOLUTION_URL}/webhook/set/${EVOLUTION_INSTANCE}" \
  -H "apikey: ${EVOLUTION_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"${WEBHOOK_URL}\",
    \"webhook_by_events\": false,
    \"webhook_base64\": false,
    \"events\": [\"MESSAGES_UPSERT\", \"messages.upsert\"],
    \"headers\": {
      \"X-Webhook-Secret\": \"${WEBHOOK_SECRET}\"
    }
  }")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

echo "HTTP $HTTP_CODE"
echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
  echo
  echo "OK: webhook configurado."
else
  echo
  echo "Intentando endpoint alternativo (v1)..."
  RESPONSE2=$(curl -s -w "\n%{http_code}" -X POST \
    "${EVOLUTION_URL}/instance/setWebhook/${EVOLUTION_INSTANCE}" \
    -H "apikey: ${EVOLUTION_KEY}" \
    -H "Content-Type: application/json" \
    -d "{
      \"url\": \"${WEBHOOK_URL}\",
      \"events\": [\"MESSAGES_UPSERT\"],
      \"headers\": {
        \"X-Webhook-Secret\": \"${WEBHOOK_SECRET}\"
      }
    }")
  HTTP_CODE2=$(echo "$RESPONSE2" | tail -1)
  BODY2=$(echo "$RESPONSE2" | head -n -1)
  echo "HTTP $HTTP_CODE2"
  echo "$BODY2" | python3 -m json.tool 2>/dev/null || echo "$BODY2"
fi

echo
echo "=== Verificando webhook actual ==="
curl -s -X GET \
  "${EVOLUTION_URL}/webhook/find/${EVOLUTION_INSTANCE}" \
  -H "apikey: ${EVOLUTION_KEY}" | python3 -m json.tool 2>/dev/null || echo "(sin respuesta)"
