#!/usr/bin/env bash
cd /opt/deal-agent/ofertas_hunter
echo "===== ¿es repo git? ====="
git rev-parse --is-inside-work-tree 2>/dev/null && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "NO es git repo"
echo
echo "===== .gitignore ====="
cat .gitignore 2>/dev/null
echo
echo "===== mtime real de archivos clave (que yo desplegué) ====="
for f in scripts/orquestador_ia.py src/ofertas_hunter/config.py src/ofertas_hunter/__main__.py \
         src/ofertas_hunter/maintenance/nightly.py src/ofertas_hunter/maintenance/environment.py \
         src/ofertas_hunter/agents/amazon_outbox_sanitizer.py \
         src/ofertas_hunter/publishing/whatsapp_publisher.py \
         changes.md; do
  if [ -f "$f" ]; then stat -c '%y  %s  %n' "$f"; else echo "(falta) $f"; fi
done
echo
echo "===== conteo de .py en src y tests ====="
echo "src/.py: $(find src -name '*.py' | wc -l)"
echo "tests/.py: $(find tests -name '*.py' | wc -l)"
echo "scripts/.py+.sh+.ps1: $(find scripts -type f \( -name '*.py' -o -name '*.sh' -o -name '*.ps1' \) | wc -l)"
echo
echo "===== ¿existe maintenance dir y sus tests? ====="
ls -la src/ofertas_hunter/maintenance/ 2>/dev/null || echo "(sin maintenance)"
ls -la tests/unit/maintenance/ 2>/dev/null || echo "(sin tests/maintenance)"
echo
echo "===== tamaño de dirs candidatos a EXCLUIR ====="
du -sh .venv .cache data secrets backups logs .pytest_cache src/ofertas_hunter.egg-info 2>/dev/null
