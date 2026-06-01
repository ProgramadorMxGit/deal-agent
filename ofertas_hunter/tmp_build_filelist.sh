#!/usr/bin/env bash
cd /opt/deal-agent/ofertas_hunter
# Lista de archivos a sincronizar: tracked + untracked NO ignorados,
# excluyendo basura (.bak*, .orig, __pycache__, egg-info, .session).
git ls-files --cached --others --exclude-standard \
  | grep -vE '(\.bak[^/]*$|\.orig$|__pycache__/|\.egg-info/|\.session|\.pyc$)' \
  | sort > /tmp/sync_filelist.txt
echo "===== total archivos a sincronizar ====="
wc -l /tmp/sync_filelist.txt
echo
echo "===== desglose por carpeta top-level ====="
awk -F/ '{print $1}' /tmp/sync_filelist.txt | sort | uniq -c | sort -rn
echo
echo "===== ¿algún archivo sensible se coló? (debe estar vacío) ====="
grep -E '(^\.env|^secrets/|^data/|^\.venv/|^logs/.*\.log$|\.session)' /tmp/sync_filelist.txt | head -20 || echo "(ninguno - OK)"
echo
echo "===== muestra de archivos nuevos interesantes ====="
grep -E 'maintenance/|outbox_admission|diversity_metadata|frontier_category|test_nightly|amazon_outbox_sanitizer' /tmp/sync_filelist.txt
echo
echo "===== tamaño total estimado del set ====="
t_total=0
while IFS= read -r f; do
  if [ -f "$f" ]; then s=$(stat -c '%s' "$f" 2>/dev/null || echo 0); t_total=$((t_total+s)); fi
done < /tmp/sync_filelist.txt
echo "bytes: $t_total  (~$((t_total/1024/1024)) MB)"
