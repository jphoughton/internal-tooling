# Hydrant Command Center — Inventory & Demand Forecasting Dashboard

## Overview

Multi-channel e-commerce analytics dashboard for **Hydrant** (DTC hydration mix brand). Forecasts demand across Shopify (DTC) and Amazon, tracks customer retention and lifetime value, recommends reorder timing using 3PL (Packiyo) and FBA inventory, and optimizes media spend ROI through waterfall demand modeling. Built with Streamlit + PostgreSQL + Facebook Prophet.

## Tech Stack

- **Frontend**: Streamlit 1.50.0 (modular multi-page app — thin router + 11 page modules)
- **Backend**: Python 3.11 (Docker), Python 3.9 (local dev)
- **Database**: PostgreSQL (prod/Railway), SQLite (local fallback) — adapter in `db.py`
- **Forecasting**: Facebook Prophet 1.1.5+
- **Data Processing**: Pandas 2.1+, NumPy 1.24+
- **Charts**: Plotly 5.18+
- **APIs**: Amazon SP-API, Shopify Admin API, Packiyo REST API, Google Sheets (public CSV)
- **Hosting**: Railway (Docker + supervisord) and local (localhost:8501)

## Architecture

```
├── dashboard.py              # Thin router: sidebar, auth, notification bar, page dispatch
├── config.py                 # Environment & credentials management
├── db.py                     # PostgreSQL/SQLite adapter, schema, connection pool, upserts
├── scheduler.py              # Daemon scheduler for daily ETL sync
├── views/                    # One file per dashboard page — each exports render(ctx)
├── ui/                       # Reusable Streamlit UI: styles, components, charts, tables
├── utils/                    # Constants (FORECAST_SKUS, seasonal indices) + date helpers
├── analytics/                # forecast, waterfall, retention, reorder, sku_flavors, dtc_demand
├── etl/                      # Amazon, Shopify, Packiyo, Google Sheets, Klaviyo sync
└── tests/                    # pytest suite (data quality + unit tests)
```

## Data Flow

```
Amazon SP-API ──────► amazon.py ──────────────► daily_sku_sales (source='amazon')
Shopify Admin ──────► shopify_client.py ──────► customers → orders → order_items → daily_sku_sales (source='shopify')
Packiyo 3PL ────────► packiyo_client.py ──────► [real-time only]
FBA Inventory ──────► amazon_inventory.py ────► [real-time only]
Google Sheets ──────► google_sheets.py ───────► google_sheet_data
Dashboard UI ───────► settings page ──────────► media_spend, amazon_revenue_forecast, planned_inbound, seasonal_indices
```

**Key pattern**: Both Amazon and Shopify have customer-level data (Amazon uses hashed emails as customer IDs). Both channels flow through `orders` → `order_items` → rebuilt into `daily_sku_sales`.

## Database

### Schema

| Table                     | Purpose                            | Primary Key                        |
| ------------------------- | ---------------------------------- | ---------------------------------- |
| `customers`               | Customer records                   | `customer_id`                      |
| `orders`                  | Shopify orders (Amazon skips this) | `order_id`                         |
| `order_items`             | Line items per order               | `id` (auto), UNIQUE(order_id, sku) |
| `sku_master`              | Product catalog                    | `sku`                              |
| `daily_sku_sales`         | Aggregated daily sales             | `(sale_date, sku, source)`         |
| `media_spend`             | Monthly ad spend + ROAS            | `(month, source)`                  |
| `amazon_revenue_forecast` | Amazon revenue targets             | `month`                            |
| `planned_inbound`         | User-entered POs                   | `(sku, month)`                     |
| `seasonal_indices`        | Monthly demand multipliers (1-12)  | `month_num`                        |
| `app_settings`            | Key-value config store             | `key`                              |
| `sync_log`                | ETL sync history                   | `id` (auto)                        |

### Query Patterns (db.py)

```python
from db import get_db, read_sql

# Reading data — always use read_sql() for SELECT queries
with get_db() as conn:
    df = read_sql('SELECT * FROM orders WHERE order_date > %s', conn, params=('2024-01-01',))

# Writing data — use the upsert helpers
with get_db() as conn:
    upsert_order(conn, order_id='123', customer_id='C1', order_date='2024-01-15', total=49.99)
    # auto-commits on context manager exit

# Direct execute for non-SELECT
with get_db() as conn:
    conn.execute('DELETE FROM planned_inbound WHERE sku = %s', ('SKU123',))
```

- **Always use `%s` placeholders** — the adapter auto-translates for SQLite
- **Always pass params as tuple** — never use f-strings (SQL injection risk)
- **Use `read_sql()`** for all reads — returns a DataFrame
- **Use `upsert_*()` helpers** for inserts — they handle ON CONFLICT
- Connection pool: `ThreadedConnectionPool(min=1, max=20)`, 30s statement timeout
- `_translate_sql()` auto-converts SQLite syntax to PostgreSQL (julianday, strftime, DATE(), etc.)

## Critical Constants

### FORECAST_SKUS (17 core Hydrant products)

```python
FORECAST_SKUS = {
    "ENBP-BP0030-LEB0", "HYBP-BP0030-CHLB0", "HYBP-BP0030-GFB0",
    "HYBP-BP0030-ICLEB0", "HYBP-BP0050-NAB0", "SLBP-BP0030-CHB0",
    "ENPO-ST0030-RSLEB0", "HYPO-ST0030-BOB0", "HYPO-ST0030-FRPUB0",
    "HYPO-ST0030-LELMB0", "HYPO-ST0030-VPBFLB0", "IMPO-ST0030-ELB0",
    "NSPO-ST0030-BEB0", "NSPO-ST0030-LEB0", "NSPO-ST0030-VPWLBB0",
    "NSPO-ST0030-WALEB0", "SLPO-ST0030-ELB0",
}
```

### Other Constants

- **Reorder**: `LEAD_TIME_WEEKS=12` (84 days), `MOQ_UNITS=5000`, `SAFETY_STOCK_WEEKS=2`
- **Seasonal Indices** (month_num 1-12): Jan:0.95, Feb:0.92, Mar:0.98, Apr:1.02, May:1.05, Jun:1.10, Jul:1.12, Aug:1.08, Sep:1.02, Oct:0.98, Nov:0.92, Dec:0.88
- **Triple Whale**: `_TW_ADJ=0.5` — TW overstates attribution ~2x; all TW data halved
- **Urgency Tiers**: OVERDUE → ORDER NOW (<7d) → ORDER SOON (<21d) → UPCOMING (<42d) → OK (42+d) → EN ROUTE

## Key Algorithms

### Waterfall Demand Split (waterfall.py)

```
For each forecast month:
  new_customers = (media_spend × ROAS) / new_customer_AOV
  repeat_units = Σ(each past cohort × retention_curve[months_since] × units_per_repeat)
  total = (new + repeat) × seasonal_index[calendar_month]
```

### Retention Curve (waterfall.py → get_average_retention_curve)

- Recency-weighted: Last 12 cohorts = 60%, next 12 = 30%, rest = 10%
- Auto-detects contaminated cohorts (M1 retention > 40%)
- Decay rate 0.98, terminal floor 0.5%, extrapolates 60 months

### Reorder Schedule (reorder.py)

Day-by-day forward simulation: start with 3PL+FBA stock → deduct daily demand → add planned inbound → trigger reorder when projected inventory < (lead_time + safety) coverage → qty = ceil(demand / MOQ) × MOQ

### Prophet Forecasting (forecast.py)

- Weekly seasonality: always on. Yearly: on if >180 days history
- Fallback to moving average if <60 days history
- 80% confidence interval. Columns must be named `ds` and `y`

## Rules for AI

### Architecture — Where Code Goes

- **Page code → `views/`** — dashboard.py is a thin router, do NOT add page code to it
- **UI widgets → `ui/`** — reusable components, chart factories, table formatters
- **Constants → `utils/constants.py`** — no magic numbers in page modules
- **Date math → `utils/date_helpers.py`** — do NOT define month_str etc. locally
- **Customer IDs → `etl/customer_id.py`** — do NOT duplicate in Shopify modules

### Import Conventions

```python
# 1. stdlib
import logging
from datetime import datetime, timedelta

# 2. third-party
import streamlit as st
import pandas as pd
import plotly.graph_objects as go

# 3. project
from db import get_db, read_sql
from config import get_config
from ui.components import render_html_table, render_freshness_badge
from ui.tables import gradient_perf_style
from utils.constants import FORECAST_SKUS
from utils.date_helpers import month_str, parse_month, add_months
from analytics.waterfall import build_waterfall
```

### Adding a New Page

1. Create `views/my_page.py` with `def render(ctx):`
2. Add nav entry in `dashboard.py` PAGES dict
3. Use `ctx['forecast_skus']`, `ctx['cached_waterfall']` etc. for shared data
4. Sort SKUs with `get_sku_sales_rank()`
5. Use `@st.cache_data(ttl=300)` for any DB query

### Streamlit Patterns

- **`st.segmented_control`** for channel filters — NOT st.button (buttons don't survive reruns)
- **`st.session_state`** for anything that must survive a rerun — ctx is rebuilt each time
- **Editor widgets** (media spend, planned inbound): store pending edits in session_state, flush to DB on "Save"
- **`@st.cache_data(ttl=N)`** for expensive computations (10s–1800s depending on cost)
- **`render_html_table()`** from `ui/components.py` — use instead of st.dataframe
- **`gradient_perf_style()`** from `ui/tables.py` — for DoD/WoW/MoM displays
- **`render_timeline_chart()`** from `ui/charts.py` — for Gantt charts
- **`render_freshness_badge()`** from `ui/components.py` — for data freshness indicators
- All SKU tables sorted by best-seller rank (use `get_sku_sales_rank()`)
- Pages must never show completely empty — always provide a fallback when data is missing

### Logging

```python
log = logging.getLogger(__name__)
```

- ETL modules: `log.info()` for sync start/end, `log.warning()` for retries
- Never `print()` — always use the logger

### Error Handling

- **ETL syncs**: catch + log + continue (one failed sync shouldn't block others)
- **Dashboard pages**: catch + `st.warning()` with user-friendly message
- Never silently swallow exceptions

### Gotchas

- `daily_sku_sales` has composite PK `(sale_date, sku, source)` — always filter by source
- Amazon DOES have customer-level data (hashed emails) in orders/order_items tables
- Shopify `order_id` is TEXT not INT (Shopify uses large numeric strings)
- `seasonal_indices` `month_num` is 1–12, NOT 0–11
- Prophet requires columns named exactly `ds` and `y`
- Streamlit reruns the entire script on every interaction — never rely on variable persistence outside `st.session_state`
- `_translate_sql()` handles SQLite↔PostgreSQL differences, but avoid raw PostgreSQL-only syntax in new code

## Git Conventions

Commits follow **Conventional Commits** (enforced by commitlint + husky):

```
feat(forecast): add seasonal demand multipliers
fix(reorder): respect planned inbound in stockout calc
refactor: extract API helper functions
test(retention): add cohort contamination test
```

**Types**: feat, fix, refactor, docs, style, test, perf, build, ci, chore, revert, wip

**Scopes**: forecast, reorder, retention, etl, ui, db, marketing, overview

**Branch workflow**: Branch from `develop` → PR to `develop` → merge to `main` for releases

**Code style**: 4-space indent, single quotes preferred. Prettier/ESLint/lint-staged auto-enforce on commit.

## Testing

```bash
pytest tests/ -v                       # All tests
pytest tests/test_date_helpers.py -v   # Specific file
```

Run tests before committing. 91 tests across 5 files: data quality (32), date helpers (21), SKU map (17), customer ID (12), constants (9).

## Running the App

```bash
streamlit run dashboard.py              # Dashboard on localhost:8501
python scheduler.py --now               # Single ETL sync
python scheduler.py                     # Daemon mode (daily 6 AM)
```

## Known Tech Debt

- Local dev uses Python 3.9 (macOS) while Railway runs 3.11 — avoid match/case syntax
- Some pages still use `st.dataframe` instead of `render_html_table`
- Klaviyo integration configured but not actively synced

## Current State

- **Working**: Full dashboard (11 pages), ETL (Amazon + Shopify + Packiyo + Sheets), Prophet forecasting, waterfall demand split, reorder alerts, FBA transfers, gradient DoD/WoW/MoM, marketing pacing, bank transactions
- **91 unit tests**, modular architecture (thin router + page modules + ui/ + utils/)
- **Next Up**: Klaviyo integration, more analytics unit tests (waterfall, reorder simulation)
