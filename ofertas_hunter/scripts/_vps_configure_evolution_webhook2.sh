#!/usr/bin/env bash
# Configura el webhook de Evolution API (formato correcto v2).
# El endpoint correcto es PUT /webhook/set/{instance} con el body
# que incluye la propiedad "webhook" como objeto.

set -uo pipefail

EVOLUTION_URL="http://162.251.147.177:8080"
EVOLUTION_KEY="dev-evolution-api-key"
EVOLUTION_INSTANCE="mi-instancia"
WEBHOOK_URL="http://127.0.0.1:9099/wa/inbound"
WEBHOOK_SECRET="FzmDbaYfmRt-jAk_7MBu7r2nnRKR4IJ0jsn-ejaC21c"

echo "=== Configurando webhook (formato v2) ==="

# Formato correcto: body con propiedad "webhook" como objeto
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  "${EVOLUTION_URL}/webhook/set/${EVOLUTION_INSTANCE}" \
  -H "apikey: ${EVOLUTION_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"webhook\": {
      \"url\": \"${WEBHOOK_URL}\",
      \"enabled\": true,
      \"webhookByEvents\": false,
      \"webhookBase64\": false,
      \"events\": [\"MESSAGES_UPSERT\"],
      \"headers\": {
        \"X-Webhook-Secret\": \"${WEBHOOK_SECRET}\"
      }
    }
  }")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)
echo "HTTP $HTTP_CODE"
echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
  echo
  echo "OK: webhook actualizado."
else
  echo
  echo "Intentando con PUT..."
  RESPONSE2=$(curl -s -w "\n%{http_code}" -X PUT \
    "${EVOLUTION_URL}/webhook/set/${EVOLUTION_INSTANCE}" \
    -H "apikey: ${EVOLUTION_KEY}" \
    -H "Content-Type: application/json" \
    -d "{
      \"webhook\": {
        \"url\": \"${WEBHOOK_URL}\",
        \"enabled\": true,
        \"webhookByEvents\": false,
        \"webhookBase64\": false,
        \"events\": [\"MESSAGES_UPSERT\"],
        \"headers\": {
          \"X-Webhook-Secret\": \"${WEBHOOK_SECRET}\"
        }
      }
    }")
  HTTP_CODE2=$(echo "$RESPONSE2" | tail -1)
  BODY2=$(echo "$RESPONSE2" | head -n -1)
  echo "HTTP $HTTP_CODE2"
  echo "$BODY2" | python3 -m json.tool 2>/dev/null || echo "$BODY2"
fi

echo
echo "=== Estado actual del webhook ==="
curl -s "${EVOLUTION_URL}/webhook/find/${EVOLUTION_INSTANCE}" \
  -H "apikey: ${EVOLUTION_KEY}" | python3 -m json.tool 2>/dev/null
