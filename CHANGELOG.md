# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.1.0] - 2026-02-24

### Added

#### Dashboard & UI
- 11-page Streamlit dashboard with thin router architecture (`dashboard.py`)
- Overview page: KPIs, revenue trends by source, top SKUs, inventory snapshot
- Retention page: cohort heatmap, retention curve, seasonality editor
- Demand Forecast page: waterfall chart, media spend editor, 5 SKU-by-month tables
- Projected Inventory page: planned inbound editor, inventory runway projections
- 3PL Inventory page: Packiyo stock levels, forecast vs inventory comparison
- Amazon Inventory page: FBA stock levels, forecast vs inventory comparison
- Reorder Alerts page: urgency-ranked table, runway charts, timeline Gantt
- FBA Transfers page: transfer urgency alerts, 3PL→FBA timeline Gantt
- Marketing page: pacing tables, DoD/WoW/MoM with gradient coloring
- Financials page: bank transaction import, P&L, cash flow charts
- Settings page: API credentials management, data imports, Google Sheets sync
- Slim single-line notification banner for urgent reorder alerts
- Reusable UI components: HTML table renderer, freshness badge, date filter, auth (`ui/components.py`)
- Plotly chart factories including Gantt-style timeline charts (`ui/charts.py`)
- DataFrame formatting, gradient styling, SKU display helpers (`ui/tables.py`)
- Global CSS with Hydrant brand theme and Plotly theme (`ui/styles.py`)

#### Analytics & Forecasting
- Facebook Prophet-based SKU demand forecasting with weekly/yearly seasonality (`analytics/forecast.py`)
- Waterfall demand split engine separating new vs. repeat customer demand (`analytics/waterfall.py`)
- Cohort retention analysis: repurchase rates and SKU lifecycle (`analytics/retention.py`)
- Inventory runway simulation and reorder schedule generation (`analytics/reorder.py`)
- SKU-to-flavor name mapping for 17 core Hydrant SKUs (`analytics/sku_flavors.py`)
- Master DTC demand rollup combining 4 forecast tables (`analytics/dtc_demand.py`)
- Recency-weighted retention curve with contamination detection and 60-month extrapolation
- Reorder urgency tiers: OVERDUE, ORDER NOW, ORDER SOON, UPCOMING, OK, EN ROUTE

#### ETL & Data Integration
- Daily ETL orchestration scheduler with daemon mode (`scheduler.py`, `etl/sync.py`)
- Amazon SP-API integration: Sales & Traffic flat-file reports (`etl/amazon.py`)
- Amazon FBA inventory sync with multi-strategy fallback (`etl/amazon_inventory.py`)
- ASIN ↔ Master SKU mapping for 23 entries (`etl/amazon_sku_map.py`)
- Shopify Admin API order/customer sync with auto-token refresh (`etl/shopify_client.py`)
- Shopify OAuth flow (`etl/shopify_oauth.py`)
- Shopify bulk JSONL operations handler (`etl/shopify_bulk_import.py`)
- Packiyo 3PL real-time inventory query (`etl/packiyo_client.py`)
- Google Sheets public CSV import without OAuth (`etl/google_sheets.py`)
- Klaviyo email metrics client (configured, not actively synced) (`etl/klaviyo_client.py`)
- Shared Shopify customer ID generation (`etl/customer_id.py`)
- Retry decorator with exponential backoff on all API calls

#### Database & Configuration
- PostgreSQL/SQLite adapter with WAL mode for concurrent reads (`db.py`)
- Full schema: customers, orders, order_items, sku_master, daily_sku_sales, media_spend, amazon_revenue_forecast, planned_inbound, seasonal_indices, app_settings, sync_log
- Environment-based configuration loading (`config.py`)
- CLI entry point accepting subcommands (`__main__.py`)

#### Utilities
- Date math utilities: month_str, parse_month, add_months, month_diff (`utils/date_helpers.py`)
- Shared constants: FORECAST_SKUS, TW_ADJUSTMENT (0.5×), seasonal indices, reorder bounds (`utils/constants.py`)

#### Developer Tooling
- Git autopilot setup script: husky hooks, commitlint, lint-staged, prettier (`setup-git-autopilot.sh`)
- Conventional Commits enforcement via commitlint + husky
- MCP servers auto-configured via `.mcp.json`: context7, sequential-thinking, playwright, github, sqlite
- Claude Code permissions pre-configured in `.claude/settings.json`
- VS Code settings, recommended extensions, and EditorConfig

#### Testing
- 91 unit and data quality tests across 5 test files
- Data quality tests: revenue sanity, date validity, referential integrity, sync health (32 tests)
- Date helper tests (21 tests)
- ASIN→SKU mapping tests (17 tests)
- Customer ID generation tests (12 tests)
- Constants sanity checks (9 tests)
