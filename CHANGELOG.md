# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-02-24

### Added

#### Dashboard & Navigation
- Thin router (`dashboard.py`) with sidebar navigation, authentication, and notification banner
- 11 dashboard pages as standalone `views/` modules, each exporting `render(ctx)`
- Slim single-line notification banner for urgent reorder alerts

#### Pages
- **Overview** — KPIs, revenue trends by source, top SKUs, inventory snapshot
- **Retention** — Cohort matrix heatmap, retention curve, seasonality editor
- **Demand Forecast** — Waterfall chart, media spend editor, 5 SKU-by-month forecast tables
- **Projected Inventory** — Planned inbound editor, inventory runway projections
- **3PL Inventory** — Packiyo stock levels with forecast vs. inventory comparison
- **Amazon Inventory** — FBA stock levels with forecast vs. inventory comparison
- **Reorder Alerts** — Urgency-ranked reorder plan (OVERDUE → OK tiers), runway charts, timeline Gantt
- **FBA Transfers** — 3PL→FBA transfer urgency alerts with timeline Gantt
- **Marketing** — Pacing tables with DoD/WoW/MoM gradient coloring
- **Financials** — Bank transaction import, P&L, cash flow charts
- **Settings** — API credentials management, data imports, Google Sheets sync

#### Analytics Engine
- **Prophet forecasting** (`analytics/forecast.py`) — SKU-level demand forecasting with weekly/yearly seasonality and moving-average fallback
- **Waterfall demand split** (`analytics/waterfall.py`) — Splits total demand into new vs. repeat customer components using media spend × ROAS model
- **Cohort retention analysis** (`analytics/retention.py`) — Recency-weighted retention curves with contamination detection, 60-month extrapolation
- **Reorder simulation** (`analytics/reorder.py`) — Day-by-day inventory runway simulation with lead time (84 days), MOQ (5,000 units), and safety stock (2 weeks)
- **SKU flavor mapping** (`analytics/sku_flavors.py`) — 17 core SKUs mapped to display names
- **DTC demand rollup** (`analytics/dtc_demand.py`) — Master rollup combining 4 forecast tables

#### ETL Pipelines
- **Amazon SP-API** (`etl/amazon.py`) — Sales & Traffic flat-file reports (30-day rolling)
- **Amazon Inventory** (`etl/amazon_inventory.py`) — FBA inventory levels with multi-strategy fallback
- **ASIN→SKU mapping** (`etl/amazon_sku_map.py`) — 23-entry mapping table
- **Shopify** (`etl/shopify_client.py`) — Order/customer sync with auto-token refresh
- **Shopify OAuth** (`etl/shopify_oauth.py`) — OAuth flow for token management
- **Shopify bulk import** (`etl/shopify_bulk_import.py`) — Bulk JSONL operations handler
- **Packiyo 3PL** (`etl/packiyo_client.py`) — Real-time 3PL inventory queries
- **Google Sheets** (`etl/google_sheets.py`) — Public CSV import (no OAuth required)
- **Klaviyo** (`etl/klaviyo_client.py`) — Email metrics client (configured, not actively synced)
- **Daily scheduler** (`scheduler.py`) — Daemon mode syncing at 6 AM; supports `--now` and `--full` flags

#### Database
- SQLite with WAL mode for concurrent reads (PostgreSQL adapter via `db.py`)
- 11 tables: `customers`, `orders`, `order_items`, `sku_master`, `daily_sku_sales`, `media_spend`, `amazon_revenue_forecast`, `planned_inbound`, `seasonal_indices`, `app_settings`, `sync_log`
- Real data seed file (`data/seed.sql.gz`) auto-restored on first run

#### UI Components & Utilities
- Global CSS with Hydrant brand theme and Plotly theme (`ui/styles.py`)
- HTML table renderer, freshness badges, date filter, auth (`ui/components.py`)
- Plotly chart factories including Gantt/timeline charts (`ui/charts.py`)
- DataFrame formatting, gradient styling, SKU display helpers (`ui/tables.py`)
- Date math utilities: `month_str`, `parse_month`, `add_months`, `month_diff` (`utils/date_helpers.py`)
- Shared constants: `FORECAST_SKUS`, `TW_ADJUSTMENT`, seasonal indices, reorder tiers (`utils/constants.py`)

#### Configuration & Tooling
- Environment-based configuration loading (`config.py`)
- Retry decorator with exponential backoff on all API calls
- Health check endpoint (`app.py`)
- CLI entry point with subcommand support (`__main__.py`)
- Conventional Commits enforcement via husky + commitlint
- lint-staged with Prettier auto-formatting on commit
- MCP servers auto-configured via `.mcp.json`: context7, sequential-thinking, playwright, github, sqlite

#### Testing
- 91 unit and data quality tests across 5 test files
- `test_data_quality.py` — 32 tests: revenue sanity, date validity, referential integrity, sync health
- `test_date_helpers.py` — 21 tests: date math utilities
- `test_sku_map.py` — 17 tests: ASIN→SKU mapping
- `test_customer_id.py` — 12 tests: customer ID generation
- `test_constants.py` — 9 tests: constants sanity checks
