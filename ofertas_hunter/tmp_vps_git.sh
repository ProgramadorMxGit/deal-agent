#!/usr/bin/env bash
cd /opt/deal-agent/ofertas_hunter
echo "===== git status resumido ====="
echo "tracked modificados: $(git status --short 2>/dev/null | grep -c '^ M')"
echo "untracked: $(git status --short 2>/dev/null | grep -c '^??')"
echo "staged: $(git status --short 2>/dev/null | grep -c '^M')"
echo "total status lines: $(git status --short 2>/dev/null | wc -l)"
echo
echo "===== últimos commits ====="
git log --oneline -5 2>/dev/null
echo
echo "===== untracked .py/.sh/.md relevantes (excluyendo ignorados) ====="
git status --short 2>/dev/null | grep '^??' | grep -E '\.(py|sh|ps1|md|service|timer|json|toml|ini)$' | head -60
echo
echo "===== ¿maintenance está tracked o untracked? ====="
git status --short -- src/ofertas_hunter/maintenance tests/unit/maintenance 2>/dev/null
echo
echo "===== archivos .bak_robust o backups dentro de src (no deberían ir al repo) ====="
find src tests -name '*.bak*' -o -name '*.orig' 2>/dev/null | head
echo
echo "===== check-ignore de dirs sensibles (confirma exclusión) ====="
for d in data/ofertas_hunter.db .env secrets/amazon_cookies.json .venv/x logs/x.log; do
  git check-ignore "$d" >/dev/null 2>&1 && echo "IGNORED: $d" || echo "NOT-ignored: $d"
done
