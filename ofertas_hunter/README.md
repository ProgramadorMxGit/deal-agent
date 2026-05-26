# ofertas_hunter

Bot autónomo de detección de ofertas reales (≥ 50% descuento) y errores de
precio en **Amazon México**, **Mercado Libre México** y **canales de Telegram**,
con publicación final en un grupo de **WhatsApp** vía **Evolution API**.

Reescritura limpia que fusiona lo mejor de los dos proyectos legacy
(`bot_diversidad_global` y `AmazonScrapperIA`).

## Documentación

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — capas, agentes y diagrama.
- [`docs/MIGRATION_PLAN.md`](docs/MIGRATION_PLAN.md) — qué se reutiliza/descarta de los dos proyectos.
- [`docs/AGENTS.md`](docs/AGENTS.md) — responsabilidades de cada agente.
- [`docs/RULES.md`](docs/RULES.md) — reglas duras de negocio (incluye formato JBL Tune 510BT).
- [`docs/PRICE_ERROR_DETECTION.md`](docs/PRICE_ERROR_DETECTION.md) — `PriceErrorScorer` y rangos por categoría.
- [`docs/TELEGRAM_PRICE_ERROR_PATTERNS.md`](docs/TELEGRAM_PRICE_ERROR_PATTERNS.md) — términos, emojis, marketplaces.
- [`docs/SCHEMA.md`](docs/SCHEMA.md) — modelo SQLite (tablas + índices).
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — Windows local + Ubuntu VPS + Docker.
- [`docs/audit/`](docs/audit) — auditoría de cada proyecto legacy.

## Estado actual

| Fase | Estado |
|---|---|
| **Fase 1 — auditoría, diseño, schema, fixtures, tests** | ✅ |
| **Fase 2 — core (config/db/models, scorer, formatter, outbox/cooldown, telegram parser)** | ✅ |
| Fase 3 — agentes Amazon/ML, dispatcher Evolution, listener Telethon | ⬜ |
| Fase 4 — watchdog, self-healing, memory compressor, deploy systemd | ⬜ |

59 tests (incluyendo los 16 obligatorios de la spec §16) pasan en `pytest`.

## Setup rápido (Windows + PowerShell)

```powershell
cd ofertas_hunter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env       # edita los secretos cuando los tengas
python -m ofertas_hunter init-db
python -m ofertas_hunter check-config
pytest -q
```

## Setup rápido (Ubuntu VPS)

Ver [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Estructura del proyecto

```
ofertas_hunter/
├── docs/                              # spec, architecture, audit, deploy
├── migrations/001_init.sql            # schema SQLite (WAL, FKs, índices)
├── src/ofertas_hunter/
│   ├── config.py                      # pydantic-settings, .env, defaults
│   ├── db.py                          # conexión SQLite + init_db
│   ├── logging_setup.py
│   ├── models.py                      # dataclasses + enums
│   ├── intelligence/
│   │   ├── price_error_scorer.py      # 0..100 score determinista
│   │   ├── discount_calculator.py
│   │   └── category_ranges.py
│   ├── publishing/
│   │   └── formatter.py               # template JBL + template price_error
│   ├── dispatching/
│   │   ├── outbox.py                  # InMemoryOutbox + SqliteOutbox
│   │   └── cooldown.py
│   └── telegram/
│       ├── message_parser.py          # parser + ML link → skip
│       └── signal_extractor.py        # urgency terms, emojis
├── tests/
│   ├── conftest.py                    # carga fixtures price_errors A-M
│   ├── fixtures/price_errors/         # ejemplos A-M de la spec §2.1
│   └── unit/                          # 59 tests
└── pyproject.toml / requirements.txt / .env.example / pytest.ini
```

## Comandos disponibles

```bash
python -m ofertas_hunter init-db          # crea / actualiza schema SQLite
python -m ofertas_hunter check-config     # imprime config (secretos enmascarados)
python -m ofertas_hunter run              # (Fase 3) arranca agentes
pytest -q                                 # corre la suite
```
