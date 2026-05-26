# Despliegue

## 1. Desarrollo local (Windows + PowerShell + Kiro)

```powershell
# 1. Clonar y entrar
cd C:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter

# 2. Crear venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Instalar deps
pip install -r requirements.txt
playwright install chromium

# 4. Configurar
copy .env.example .env
# editar .env

# 5. Inicializar DB
python -c "from ofertas_hunter.db import init_db; init_db()"

# 6. Tests
pytest -q

# 7. Correr (solo recolector + dispatcher)
python -m ofertas_hunter --profile full
```

## 2. Desarrollo local (Linux/Mac)

```bash
cd ofertas_hunter
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
python -c "from ofertas_hunter.db import init_db; init_db()"
pytest -q
python -m ofertas_hunter --profile full
```

## 3. Producción VPS Ubuntu (22.04 / 24.04)

### 3.1 Pre-requisitos

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv build-essential \
    libnss3 libatk1.0-0 libxkbcommon0 libgbm1 libxcomposite1 libxdamage1 \
    libxrandr2 libasound2t64 libpangocairo-1.0-0 libpango-1.0-0
```

### 3.2 Usuario y carpeta

```bash
sudo useradd -m -s /bin/bash ofertas
sudo mkdir -p /opt/ofertas-hunter
sudo chown -R ofertas:ofertas /opt/ofertas-hunter
```

### 3.3 Instalar app

```bash
sudo -iu ofertas
cd /opt/ofertas-hunter
git clone <repo-url> .
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# editar /opt/ofertas-hunter/.env con secrets reales
python -c "from ofertas_hunter.db import init_db; init_db()"
exit
```

### 3.4 systemd

Tres servicios independientes para aislar fallos:

- `ofertas-hunter.service` — orchestrator (hunters + price_intelligence)
- `ofertas-hunter-dispatcher.service` — dispatcher serializado a WhatsApp
- `ofertas-hunter-telegram.service` — listener Telegram

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ofertas-hunter
sudo systemctl enable --now ofertas-hunter-dispatcher
# opcional:
sudo systemctl enable --now ofertas-hunter-telegram
```

### 3.5 Logs y mantenimiento

```bash
# Ver logs
sudo journalctl -u ofertas-hunter -f
sudo journalctl -u ofertas-hunter-dispatcher -f

# Health check
sudo systemctl status ofertas-hunter

# Estado SQLite
sudo -iu ofertas /opt/ofertas-hunter/scripts/status.sh
sudo -iu ofertas /opt/ofertas-hunter/.venv/bin/python /opt/ofertas-hunter/scripts/check_db.py

# Revalidar outbox manualmente
sudo -iu ofertas /opt/ofertas-hunter/.venv/bin/python /opt/ofertas-hunter/scripts/revalidate_outbox.py
```

### 3.6 Variables de entorno críticas

```env
DB_PATH=/opt/ofertas-hunter/data/ofertas_hunter.db
MERCADOLIBRE_COOKIES_PATH=/opt/ofertas-hunter/secrets/mercadolibre_cookies.json
TELEGRAM_SESSION_PATH=/opt/ofertas-hunter/secrets/telegram.session
EVOLUTION_BASE_URL=https://evolution.tu-dominio.com
EVOLUTION_API_KEY=...
EVOLUTION_INSTANCE=...
WHATSAPP_TARGET_GROUP_ID=120363xxxxxxxxxxx@g.us
```

## 4. Docker Compose (opcional)

```yaml
# deploy/docker-compose.yml (ejemplo)
services:
  ofertas-hunter:
    build:
      context: ../
      dockerfile: deploy/docker/Dockerfile
    env_file: ../.env
    volumes:
      - ./data:/app/data
      - ./secrets:/app/secrets
      - ./logs:/app/logs
    restart: unless-stopped

  ofertas-hunter-dispatcher:
    build:
      context: ../
      dockerfile: deploy/docker/Dockerfile
    command: python -m ofertas_hunter --profile dispatcher
    env_file: ../.env
    volumes:
      - ./data:/app/data
      - ./secrets:/app/secrets
      - ./logs:/app/logs
    restart: unless-stopped
    depends_on:
      - ofertas-hunter
```

## 5. Secretos

- `secrets/mercadolibre_cookies.json` — exportadas con Cookie-Editor.
- `secrets/telegram.session` — generada al primer login interactivo.
- `.env` — con todas las claves y endpoints.

**Nunca** commitear estos archivos. `.gitignore` ya los excluye.

Para rotar (manual, nunca automático):

1. Editar `.env` o el archivo `secrets/...`.
2. `sudo systemctl restart ofertas-hunter*`.

## 6. Mantenimiento periódico

| Tarea | Frecuencia | Comando |
|---|---|---|
| Backup SQLite | diario | `sqlite3 data/ofertas_hunter.db .dump > backups/$(date +%F).sql` |
| Compresión memoria | automática | `memory_compressor` cada 6h |
| Vacuum SQLite | semanal | `sqlite3 data/ofertas_hunter.db "VACUUM;"` |
| Renovar cookies ML | cuando watchdog avisa | exportar cookies y reemplazar archivo |
| Renovar sesión Telegram | si caduca | `python scripts/test_telegram.py --re-login` |

## 7. Roadmap de despliegue

1. ✅ Spec, audit, schema, esqueleto, scorer, formatter, outbox.
2. ⬜ Implementar marketplaces Amazon + ML.
3. ⬜ Implementar Telegram listener.
4. ⬜ Implementar dispatcher Evolution API.
5. ⬜ Watchdog + self-healing.
6. ⬜ Dockerfile + compose.
7. ⬜ Servicios systemd.
8. ⬜ Backups y monitoring.
