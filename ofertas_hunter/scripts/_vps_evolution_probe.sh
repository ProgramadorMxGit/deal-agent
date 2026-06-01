#!/usr/bin/env bash
# Prueba endpoints de Evolution API para encontrar uno que liste mensajes recientes.
set -uo pipefail

BASE="${EVOLUTION_BASE_URL:-http://162.251.147.177:8080}"
KEY="${EVOLUTION_API_KEY:-dev-evolution-api-key}"
INST="${EVOLUTION_INSTANCE:-mi-instancia}"

H_KEY="apikey: $KEY"
H_KEY2="Authorization: Bearer $KEY"

run() {
    local label="$1"; shift
    echo "================================================================"
    echo "[$label] $*"
    echo "----------------------------------------------------------------"
    "$@" 2>&1 | head -c 1500
    echo
    echo
}

echo "Base: $BASE"
echo "Instance: $INST"
echo

# 1) listar instancias
run "fetchInstances" curl -s -m 8 -H "$H_KEY" "$BASE/instance/fetchInstances"

# 2) info de instancia
run "connectionState" curl -s -m 8 -H "$H_KEY" "$BASE/instance/connectionState/$INST"

# 3) findMessages POST con body bien formado
run "findMessages POST" curl -s -m 10 -H "$H_KEY" -H "Content-Type: application/json" \
    -X POST "$BASE/chat/findMessages/$INST" \
    -d '{"where":{"key":{"fromMe":false}},"limit":10,"order":"desc"}'

# 4) findMessages alternativo
run "findMessages.fromMe" curl -s -m 10 -H "$H_KEY" -H "Content-Type: application/json" \
    -X POST "$BASE/chat/findMessages/$INST" \
    -d '{"limit":10}'

# 5) chat findChats
run "findChats" curl -s -m 10 -H "$H_KEY" -H "Content-Type: application/json" \
    -X POST "$BASE/chat/findChats/$INST" -d '{}'

# 6) getMessages directo
run "getMessages" curl -s -m 10 -H "$H_KEY" "$BASE/message/findMessages/$INST"

# 7) webhook actual
run "webhook find" curl -s -m 8 -H "$H_KEY" "$BASE/webhook/find/$INST"
