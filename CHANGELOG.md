# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] - 2026-02-24

Initial release of the Hydrant Command Center — a multi-channel inventory and demand forecasting dashboard for Hydrant (DTC hydration mix brand).

### Added

#### Dashboard & Routing
- Thin router (`dashboard.py`) with sidebar navigation, password authentication, and notification banner for urgent reorder alerts
- 11 dashboard pages dispatched via modular `views/` architecture — each page exports `render(ctx)` and receives shared cached state

#### Pages
- **Overview** — KPI summary cards, revenue trends by channel, top SKUs by sales rank, inventory snapshot
- **Retention** — Cohort heatmap, retention curve chart, seasonality index editor
- **Demand Forecast** — Waterfall chart (new vs. repeat demand), media spend editor, 5 SKU-by-month forecast tables
- **Projected Inventory** — Planned inbound PO editor, per-SKU inventory runway projections
- **3PL Inventory** — Real-time Packiyo stock levels, forecast vs. on-hand comparison
- **Amazon Inventory** — FBA stock levels, forecast vs. on-hand comparison
- **Reorder Alerts** — Urgency-ranked reorder table (OVERDUE / ORDER NOW / ORDER SOON / UPCOMING / OK), runway bar charts, Gantt timeline
- **FBA Transfers** — 3PL→FBA transfer urgency alerts, Gantt timeline
- **Marketing** — Pacing tables with DoD/WoW/MoM gradient coloring, spend vs. target tracking
- **Financials** — Bank transaction CSV import, P&L summary, cash flow charts
- **Settings** — API credential management, manual data imports, Google Sheets sync trigger

#### Analytics
- **Prophet forecasting** (`analytics/forecast.py`) — Facebook Prophet daily→monthly time series for 17 core SKUs; falls back to moving average if < 60 days history
- **Waterfall demand split** (`analytics/waterfall.py`) — Splits total demand into new-customer and repeat-customer components using media spend, ROAS, and retention curve
- **Retention / cohort analysis** (`analytics/retention.py`) — Recency-weighted cohort repurchase rates, SKU lifecycle analysis, contamination detection
- **Reorder simulation** (`analytics/reorder.py`) — Day-by-day forward simulation with lead time (12 weeks), MOQ (5,000 units), and safety stock (2 weeks); produces urgency-tiered reorder schedule
- **DTC demand rollup** (`analytics/dtc_demand.py`) — Master rollup combining 4 forecast tables

#### ETL Pipelines
- **Amazon SP-API sync** (`etl/amazon.py`) — Sales & Traffic flat-file reports (30-day window), ASIN→Master SKU mapping (23 entries)
- **Amazon FBA inventory** (`etl/amazon_inventory.py`) — Multi-strategy fallback for real-time FBA stock levels
- **Shopify sync** (`etl/shopify_client.py`) — Order and customer sync with auto token refresh; bulk JSONL operations support
- **Shopify OAuth flow** (`etl/shopify_oauth.py`) — Token acquisition and refresh
- **Packiyo 3PL sync** (`etl/packiyo_client.py`) — Real-time 3PL inventory queries
- **Google Sheets import** (`etl/google_sheets.py`) — Public CSV import (no OAuth required)
- **Klaviyo client** (`etl/klaviyo_client.py`) — Email metrics client (configured, not actively synced)
- **Daily orchestration** (`etl/sync.py`) — Coordinates all ETL syncs; triggered by scheduler or on dashboard load if > 24 h stale

#### Database
- SQLite database in WAL mode with 11 tables: `customers`, `orders`, `order_items`, `sku_master`, `daily_sku_sales`, `media_spend`, `amazon_revenue_forecast`, `planned_inbound`, `seasonal_indices`, `app_settings`, `sync_log`
- PostgreSQL/SQLite adapter (`db.py`) for Railway cloud hosting compatibility

#### UI Framework
- Global CSS with Hydrant brand theme and Plotly chart theme (`ui/styles.py`)
- HTML table renderer with gradient styling, freshness badges, date filter, auth gate (`ui/components.py`)
- Gantt/timeline chart factory (`ui/charts.py`)
- DataFrame formatters, gradient performance styling, SKU display helpers (`ui/tables.py`)

#### Infrastructure
- `scheduler.py` — Daemon mode syncing daily at 6 AM; supports `--now` (single sync) and `--full` (full historical pull) flags
- `config.py` — Environment-based credential loading with Railway and local `.env` support; `BUILD_VERSION` constant; health check endpoint
- Railway deployment via Docker + supervisord
- Retry decorator with exponential backoff on all API calls

#### Testing
- 91 unit and data quality tests across 5 test files
- `test_data_quality.py` — 32 tests: revenue sanity, date validity, referential integrity, sync health
- `test_date_helpers.py` — 21 tests for date math utilities
- `test_sku_map.py` — 17 tests for ASIN→SKU mapping
- `test_customer_id.py` — 12 tests for customer ID generation
- `test_constants.py` — 9 tests for constants sanity checks

#### Developer Tooling
- Git autopilot setup script (`setup-git-autopilot.sh`) — installs husky, commitlint, lint-staged, prettier in one command
- Conventional Commits enforced via commitlint + husky hooks
- Prettier auto-formatting on save and pre-commit
- MCP server auto-configuration (`.mcp.json`): context7, sequential-thinking, playwright, github, sqlite
- Claude Code permissions pre-configured (`.claude/settings.json`)
- VS Code workspace settings, recommended extensions, and EditorConfig

[0.1.0]: https://github.com/hydrant/internal-tooling/releases/tag/v0.1.0
