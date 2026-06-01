#!/usr/bin/env bash
# Limpia ~/.bashrc de cualquier export PATH a ~/.local/bin previo (incluso si
# quedo corrupto con barras invertidas) y agrega uno limpio. Idempotente.

set -euo pipefail

BASHRC="$HOME/.bashrc"
BACKUP="$HOME/.bashrc.bak.$(date +%Y%m%d_%H%M%S)"

cp "$BASHRC" "$BACKUP"
echo "  Backup: $BACKUP"

# Escribimos un script python aparte (evita problemas con escapes en heredoc bash)
PYHELPER=$(mktemp)
cat > "$PYHELPER" <<'PYEOF'
from pathlib import Path

p = Path.home() / ".bashrc"
text = p.read_text(encoding="utf-8", errors="replace")
out_lines = []
for raw in text.splitlines():
    stripped = raw.strip()
    if "local/bin" in raw and "export PATH" in raw:
        continue
    if stripped == '"' or stripped == "'":
        continue
    if stripped.startswith("\\"):
        continue
    out_lines.append(raw)

while out_lines and out_lines[-1].strip() == "":
    out_lines.pop()

out_lines.append('export PATH="$HOME/.local/bin:$PATH"')
p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
print("  Reescrito ~/.bashrc OK")
PYEOF

python3 "$PYHELPER"
rm -f "$PYHELPER"

echo
echo "  Ultimas 3 lineas:"
tail -3 "$BASHRC"

echo
echo "  Probando kiro-cli en sesion nueva..."
bash -lc 'command -v kiro-cli && kiro-cli --version'
