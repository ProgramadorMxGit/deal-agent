# ofertas_hunter — imagen base.
#
# Build:
#   docker build -t ofertas-hunter -f deploy/docker/Dockerfile .
#
# Run (modo dispatcher, dry-run por default):
#   docker run --rm --env-file .env -v $(pwd)/data:/app/data \
#              -v $(pwd)/secrets:/app/secrets ofertas-hunter dispatch

FROM python:3.11-slim-bookworm

# Dependencias del sistema necesarias para Playwright Chromium headless.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       wget gnupg ca-certificates \
       libnss3 libatk1.0-0 libxkbcommon0 libgbm1 libxcomposite1 libxdamage1 \
       libxrandr2 libasound2 libpangocairo-1.0-0 libpango-1.0-0 \
       libcups2 libdrm2 libxshmfence1 libatk-bridge2.0-0 libatspi2.0-0 \
       fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia primero requirements para aprovechar cache de capas.
COPY requirements.txt pyproject.toml ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

# Resto del código.
COPY src ./src
COPY migrations ./migrations
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir -e .

# Volúmenes recomendados:
#   /app/data        SQLite + heal_samples + telegram_images
#   /app/secrets     mercadolibre_cookies.json + telegram.session
#   /app/logs        logs estructurados
VOLUME ["/app/data", "/app/secrets", "/app/logs"]

# Por defecto NO publica: PUBLISHING_DRY_RUN=true en .env.
ENTRYPOINT ["python", "-m", "ofertas_hunter"]
CMD ["status"]
