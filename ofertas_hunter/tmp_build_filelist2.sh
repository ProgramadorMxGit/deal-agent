#!/usr/bin/env bash
cd /opt/deal-agent/ofertas_hunter
# Set de SINCRONIZACIÓN = código + config + tests + docs + deploy + scripts,
# honrando .gitignore y excluyendo además:
#  - data/ y .cache/ (artefactos pesados de runtime/scraping)
#  - *.egg-info (build artifacts)
#  - *.bak*/.orig/__pycache__/.pyc (basura)
#  - logs/  (logs de runtime)
git ls-files --cached --others --exclude-standard \
  | grep -vE '^(data/|\.cache/|logs/)' \
  | grep -vE '(\.egg-info/|__pycache__/|\.bak[^/]*$|\.orig$|\.pyc$|\.session)' \
  | sort > /tmp/sync_filelist.txt
echo "===== total archivos a sincronizar ====="
wc -l /tmp/sync_filelist.txt
echo
echo "===== desglose por carpeta top-level ====="
awk -F/ '{print $1}' /tmp/sync_filelist.txt | sort | uniq -c | sort -rn
echo
echo "===== verificación: NADA sensible ni pesado ====="
grep -E '^(\.env$|\.env\.|secrets/|data/|\.cache/|\.venv/)' /tmp/sync_filelist.txt | grep -vE '\.env\.example$' | head || echo "(ninguno - OK)"
echo "conteo data/.cache/secrets en lista: $(grep -cE '^(data/|\.cache/|secrets/)' /tmp/sync_filelist.txt)"
echo
echo "===== tamaño total del set ====="
t=0; while IFS= read -r f; do [ -f "$f" ] && { s=$(stat -c '%s' "$f" 2>/dev/null||echo 0); t=$((t+s)); }; done < /tmp/sync_filelist.txt
echo "bytes: $t  (~$((t/1024/1024)) MB)"
echo
echo "===== secrets incluidos (solo ejemplos permitidos) ====="
grep '^secrets/' /tmp/sync_filelist.txt || echo "(ninguno)"
