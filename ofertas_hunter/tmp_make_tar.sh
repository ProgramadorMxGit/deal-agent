#!/usr/bin/env bash
set -e
cd /opt/deal-agent/ofertas_hunter
# Set final de sincronización (código/config/tests/docs/deploy/scripts).
# Exclusiones: data/, .cache/, logs/, egg-info, basura, y TODO secrets/
# salvo *.example.json y README (NUNCA cookies reales).
git ls-files --cached --others --exclude-standard \
  | grep -vE '^(data/|\.cache/|logs/)' \
  | grep -vE '(\.egg-info/|__pycache__/|\.bak[^/]*$|\.orig$|\.pyc$|\.session)' \
  | grep -vE '^secrets/' \
  | sort > /tmp/sync_filelist.txt
# Re-agregar SOLO los secrets seguros (ejemplos + README).
{
  echo "secrets/README.md"
  echo "secrets/mercadolibre_cookies.example.json"
} >> /tmp/sync_filelist.txt
# Filtrar a solo los que existen.
: > /tmp/sync_final.txt
while IFS= read -r f; do [ -f "$f" ] && echo "$f" >> /tmp/sync_final.txt; done < /tmp/sync_filelist.txt
sort -u /tmp/sync_final.txt -o /tmp/sync_final.txt

echo "===== total final ====="
wc -l /tmp/sync_final.txt
echo "===== verificación anti-secretos (debe ser vacío) ====="
grep -E '^secrets/' /tmp/sync_final.txt | grep -vE '(README\.md$|\.example\.json$)' || echo "(limpio)"
grep -E '^(\.env$|data/|\.cache/|\.venv/|logs/)' /tmp/sync_final.txt || echo "(sin pesados/sensibles)"
echo "===== construyendo tar ====="
tar -czf /tmp/vps_sync.tar.gz -T /tmp/sync_final.txt
ls -lh /tmp/vps_sync.tar.gz
echo "sha256:"; sha256sum /tmp/vps_sync.tar.gz
