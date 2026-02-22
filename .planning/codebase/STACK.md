# Stack

## Languages & Runtime

- **Language**: Python 3.11
- **Runtime**: CPython 3.11-slim (Docker image), local development on macOS/Linux
- **Package Manager**: pip
- **Node.js**: v20+ (dev dependency for Husky, commitlint, lint-staged, Prettier)

## Frameworks

### Frontend

- **Streamlit** 1.50.0 — Multi-page web dashboard with 10 pages
- **Plotly** 5.18.0+ — Interactive charts and heatmaps
- **Pandas** 2.1.0+ — DataFrames for data manipulation and aggregation

### Forecasting & Analytics

- **Facebook Prophet** 1.1.5+ — Time series demand forecasting per SKU (weekly + yearly seasonality)
- **NumPy** 1.24.0+ — Numerical computations

### Data & Storage

- **SQLite** 3.x (WAL mode) — Primary data store at `data/inventory.db`

### API Integrations

- **python-amazon-sp-api** 0.12.0+ — Amazon Selling Partner API
- **ShopifyAPI** 12.0.0+ — Shopify Admin API
- **Requests** 2.28.0+ — HTTP client for Packiyo, Google Sheets
- **python-dotenv** 1.0.0+ — Environment variable management

### Scheduling & Daemon

- **Schedule** 1.2.0+ — Task scheduler for daily ETL syncs at 6 AM UTC
- **Supervisord** (Docker only) — Process manager for daemon mode on Railway

## Dependencies

### Core Requirements (`requirements.txt`)

| Package              | Version  | Purpose                     |
| -------------------- | -------- | --------------------------- |
| streamlit            | >=1.50.0 | Web UI framework            |
| plotly               | >=5.18.0 | Interactive visualization   |
| prophet              | >=1.1.5  | Time series forecasting     |
| pandas               | >=2.1.0  | Data manipulation           |
| numpy                | >=1.24.0 | Numerical computing         |
| python-amazon-sp-api | >=0.12.0 | Amazon SP-API client        |
| ShopifyAPI           | >=12.0.0 | Shopify Admin API client    |
| python-dotenv        | >=1.0.0  | Environment variable loader |
| requests             | >=2.28.0 | HTTP requests               |
| schedule             | >=1.2.0  | Task scheduling daemon      |

### Dev Dependencies (`package.json`)

| Package                               | Purpose                         |
| ------------------------------------- | ------------------------------- |
| husky                                 | Git hooks manager               |
| @commitlint/cli + config-conventional | Conventional commit enforcement |
| lint-staged                           | Auto-lint staged files          |
| prettier                              | Code formatter (JSON, MD, YAML) |

## Configuration

### Environment Variables (`.env`)

Loaded via `python-dotenv`, overlaid with persistent values from SQLite `app_settings` table.

**Config loading order:**

1. System env vars (Docker, Railway)
2. `.env` file (local development)
3. SQLite `app_settings` table (persistent, overrides both above)

**Key config file:** `config.py` — Central module that reads `.env`, overlays DB settings, exports module-level constants.

### Config Files

| File                     | Purpose                                                     |
| ------------------------ | ----------------------------------------------------------- |
| `config.py`              | Credentials & paths, DB persistence, runtime reload         |
| `.streamlit/config.toml` | Streamlit theme (Hydrant blue #7ECCE5)                      |
| `commitlint.config.js`   | Conventional Commits validation                             |
| `.prettierrc.json`       | Formatting rules (single quotes, trailing commas, 100 char) |
| `.lintstagedrc.json`     | Auto-lint on commit                                         |
| `.editorconfig`          | Universal whitespace rules                                  |

## Build & Deploy

### Local Development

```bash
python mock_data.py                # Generate test data
streamlit run dashboard.py         # Launch on localhost:8501
```

**Startup script** (`run.sh`): Copies .env.example, installs deps, restores seed, inits DB, launches Streamlit.

### Docker & Railway

- **Base image**: `python:3.11-slim`
- **Entrypoint**: `entrypoint.sh` (seed restore + DB init + supervisord)
- **Processes**: Streamlit (port 8501) + scheduler daemon (daily syncs)
- **Persistent volume**: `/data` for SQLite DB (survives redeploys)

### Scheduler

```bash
python scheduler.py              # Daemon mode (6 AM daily)
python scheduler.py --now        # Single sync + exit
python scheduler.py --full       # Full historical refresh + exit
```
