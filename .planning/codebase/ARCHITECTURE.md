# Architecture

## Pattern

**Monolithic Streamlit Multi-Page App with Modular Analytics and ETL Layers**

The system follows a three-tier architecture: ETL pipeline (data ingestion), analytics engine (computations), and UI layer (Streamlit dashboard). All components converge on a single SQLite database as the source of truth.

- **Dashboard**: Single-file Streamlit app (`dashboard.py`, ~5,400 lines) with multi-page routing
- **Analytics**: Modular Python packages for forecasting, retention, demand modeling
- **ETL**: Modular API integrations and data sync orchestration
- **Database**: SQLite (WAL mode) for concurrent reads, persistent storage
- **Configuration**: Environment-based credentials with DB persistence for Railway

Key design principle: **Data flows through the database; logic lives in analytics modules; UI reads from DB and calls analytics functions with caching.**

## Layers

### 1. ETL Layer (`etl/`)

Ingests data from external APIs and normalizes into the database.

| Module                   | Lines | Purpose                                    |
| ------------------------ | ----- | ------------------------------------------ |
| `sync.py`                | ~204  | Orchestrates daily sync across all sources |
| `amazon.py`              | ~318  | Sales reports, ASIN→SKU mapping            |
| `amazon_inventory.py`    | ~374  | FBA inventory real-time queries            |
| `amazon_sku_map.py`      | ~233  | ASIN ↔ Master SKU mapping (23 entries)     |
| `shopify_client.py`      | ~298  | Orders, customers, line items              |
| `shopify_oauth.py`       | ~73   | OAuth token flow                           |
| `shopify_bulk_import.py` | ~312  | Bulk operations handler                    |
| `packiyo_client.py`      | ~61   | 3PL inventory real-time queries            |
| `google_sheets.py`       | ~204  | Public CSV data import                     |
| `klaviyo_client.py`      | ~213  | Email campaigns (configured, not active)   |

**Pattern**: Each connector is independent and idempotent. Sync operates on date ranges. Data lands in normalized tables.

### 2. Database Layer (`db.py`, ~448 lines)

SQLite schema, connection management, upsert helpers, queries.

**Key functions**:

- `get_db()` — Context manager (WAL mode, 30s timeout, foreign keys)
- `init_db()` — Schema creation + default seasonal indices
- `upsert_*()` — Idempotent insert/update (customer, order, order_item, sku)
- `rebuild_daily_sales()` — Aggregate order_items → daily_sku_sales by source
- `get_setting()` / `set_setting()` — Persistent app_settings table

### 3. Analytics Layer (`analytics/`)

Domain-specific computations called by the dashboard.

| Module           | Lines | Purpose                                                       |
| ---------------- | ----- | ------------------------------------------------------------- |
| `forecast.py`    | ~212  | Prophet-based SKU demand forecasting, moving average fallback |
| `retention.py`   | ~194  | Cohort repurchase analysis, SKU lifecycle classification      |
| `waterfall.py`   | ~800  | Demand split: repeat (retention curve) + new (media × ROAS)   |
| `reorder.py`     | ~480  | Day-by-day inventory runway simulation, urgency tiers         |
| `dtc_demand.py`  | ~900  | Master DTC rollup: 4 forecast tables combined                 |
| `sku_flavors.py` | ~172  | SKU → flavor name mapping, best-seller ranking                |

**Key algorithms**:

- Retention curve: recency-weighted (60/30/10%), decay 0.98, floor 0.5%, 60-month extrapolation
- Waterfall: new_customers = (media_spend × ROAS) / AOV, repeat = Σ(cohort × retention × units)
- Reorder: forward simulation with lead_time=12wk, MOQ=5000, safety=2wk
- Prophet: weekly seasonality always on, yearly if >180 days, fallback to MA if <60 days

### 4. Dashboard/UI Layer (`dashboard.py`, ~5,400 lines)

Streamlit multi-page app with 10 pages.

**Pages** (in routing order):

1. **Overview** — KPIs, revenue by source, top SKUs, daily trend
2. **Retention** — Cohort heatmap, LTV, SKU lifecycle, seasonality editor
3. **Demand Forecast** — Prophet output, waterfall tables, pacing
4. **Projected Inventory** — 90-day runway forecast
5. **3PL Inventory** — Real-time Packiyo stock levels
6. **Amazon Inventory** — Real-time FBA stock levels
7. **Reorder Alerts** — Urgency table, runway chart, FBA transfers
8. **Marketing** — Channel pacing, DoD/WoW/MoM gradients
9. **Financials** — Revenue breakdown, ROAS, bank transactions
10. **Settings** — Credentials, media spend, seasonal indices, planned inbound

**UI patterns**:

- `st.segmented_control()` for stateful toggles (NOT st.button)
- `@st.cache_data(ttl=...)` for expensive computations (10s–1800s)
- `_gradient_perf_style()` for DoD/WoW/MoM coloring (5-tier green→red)
- SKU tables sorted by best-seller rank (last 90 days)
- Slim alert banner at top for urgent reorder alerts

## Data Flow

```
Amazon SP-API → etl/amazon.py → daily_sku_sales (source='amazon') ─┐
Shopify API   → etl/shopify_client.py → orders → order_items ──────┼→ analytics/ → dashboard.py
Packiyo 3PL   → etl/packiyo_client.py → [real-time, not stored] ───┤
FBA Inventory → etl/amazon_inventory.py → [real-time, not stored] ──┘
Google Sheets → etl/google_sheets.py → google_sheet_data
Manual Input  → Dashboard Settings → media_spend, seasonal_indices, planned_inbound
```

## Key Abstractions

- **`get_db()`** — All database access goes through this context manager
- **`@st.cache_data(ttl=N)`** — All expensive computations are cached
- **`FORECAST_SKUS`** — Set of 17 core SKU codes used across forecast/reorder/waterfall
- **`source` column** — Distinguishes 'amazon' vs 'shopify' data throughout the system
- **`app_settings` table** — Key-value store for persistent config (survives Railway redeploys)

## Entry Points

| Entry Point   | File           | Command                               |
| ------------- | -------------- | ------------------------------------- |
| Dashboard UI  | `dashboard.py` | `streamlit run dashboard.py`          |
| ETL Scheduler | `scheduler.py` | `python scheduler.py [--now\|--full]` |
| Mock Data     | `mock_data.py` | `python mock_data.py`                 |
| Configuration | `config.py`    | Imported as module                    |
| Database      | `db.py`        | Imported as module                    |
