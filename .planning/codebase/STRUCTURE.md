# Structure

## Directory Layout

```
internal-tooling/
├── .claude/                          # Claude Code config & extensions
│   ├── agents/                       # Custom subagent definitions (7 agents)
│   ├── commands/gsd/                 # GSD workflow slash commands
│   ├── get-shit-done/                # GSD runtime (workflows, agents, hooks)
│   └── settings.json                 # Permissions (allow/deny rules)
├── .husky/                           # Git hooks (commitlint, lint-staged)
├── .planning/                        # GSD project planning docs
│   └── codebase/                     # Codebase mapping (this directory)
├── .streamlit/
│   └── config.toml                   # Streamlit theme config
├── .vscode/                          # VS Code settings & extensions
├── analytics/                        # Domain-specific analytics modules
│   ├── __init__.py
│   ├── dtc_demand.py                 # Master DTC rollup (~900 lines)
│   ├── forecast.py                   # Prophet forecasting (~212 lines)
│   ├── reorder.py                    # Reorder schedule simulation (~480 lines)
│   ├── retention.py                  # Cohort analysis (~194 lines)
│   ├── sku_flavors.py                # SKU → flavor mapping (~172 lines)
│   └── waterfall.py                  # Demand split engine (~800 lines)
├── data/                             # Data directory (gitignored locally)
│   ├── inventory.db                  # SQLite database
│   └── seed.sql.gz                   # Real data seed (Git LFS)
├── etl/                              # API integrations & data sync
│   ├── __init__.py
│   ├── amazon.py                     # Amazon SP-API sales (~318 lines)
│   ├── amazon_inventory.py           # Amazon FBA inventory (~374 lines)
│   ├── amazon_restock.py             # Restock alerts (unused)
│   ├── amazon_sku_map.py             # ASIN ↔ SKU mapping (~233 lines)
│   ├── google_sheets.py              # Public Sheets import (~204 lines)
│   ├── klaviyo_client.py             # Klaviyo (configured, not active)
│   ├── packiyo_client.py             # 3PL inventory (~61 lines)
│   ├── shopify_bulk_import.py        # Bulk operations (~312 lines)
│   ├── shopify_client.py             # Shopify Admin API (~298 lines)
│   ├── shopify_oauth.py              # OAuth flow (~73 lines)
│   └── sync.py                       # ETL orchestration (~204 lines)
├── AGENTS.md                         # Subagent documentation
├── CLAUDE.md                         # Master project instructions
├── Dockerfile                        # Railway container image
├── config.py                         # Credentials & config (~222 lines)
├── dashboard.py                      # Main Streamlit UI (~5,400 lines)
├── db.py                             # Database layer (~448 lines)
├── entrypoint.sh                     # Railway startup script
├── launch.command                    # macOS double-click launcher
├── mock_data.py                      # Test data generator
├── requirements.txt                  # Python dependencies
├── run.sh                            # Local dev launcher
├── scheduler.py                      # ETL daemon (~55 lines)
└── supervisord.conf                  # Railway process manager
```

## Key Locations

| What            | Where                                  |
| --------------- | -------------------------------------- |
| All UI code     | `dashboard.py` (single file, 10 pages) |
| ETL pipelines   | `etl/` directory (one file per API)    |
| Analytics logic | `analytics/` directory                 |
| Database schema | `db.py` → `init_db()`                  |
| Configuration   | `config.py` + `.env`                   |
| Theme           | `.streamlit/config.toml`               |
| Git hooks       | `.husky/` + `commitlint.config.js`     |
| GSD workflow    | `.claude/commands/gsd/`                |

## Naming Conventions

### Files

- Python modules: `lowercase_snake_case.py` (e.g., `shopify_client.py`, `amazon_inventory.py`)
- Packages: directories with `__init__.py` (analytics/, etl/)

### Functions

- Public: `function_name()` (e.g., `forecast_sku()`, `get_customer_cohort_data()`)
- Private/helper: `_function_name()` (e.g., `_cached_waterfall()`, `_smart_date_filter()`)
- Cached: `_cached_*()` (marks Streamlit cache decorators)
- Getters: `get_*()` (e.g., `get_db()`, `get_setting()`)
- Setters: `set_*()` or `upsert_*()` (e.g., `set_setting()`, `upsert_order()`)

### Variables

- Constants: `UPPERCASE_SNAKE_CASE` (e.g., `LEAD_TIME_WEEKS`, `FORECAST_SKUS`)
- Local: `snake_case` (e.g., `current_stock`, `daily_demand`)
- DataFrames: suffix `_df` (e.g., `cohort_df`, `forecast_df`)

### Database

- Tables: `lowercase_snake_case` (e.g., `daily_sku_sales`, `order_items`)
- Columns: `lowercase_snake_case` (e.g., `customer_id`, `units_sold`)
- Sources: lowercase strings ('amazon', 'shopify')
- Urgency tiers: UPPERCASE ('OVERDUE', 'ORDER NOW', 'ORDER SOON')

## Module Boundaries

### `dashboard.py` owns:

- Page routing & navigation
- UI widgets (st.metric, st.plotly_chart, st.dataframe)
- Date filtering & user input
- Caching of expensive computations
- Freshness badges & alert banners
- Password-protected Settings page

### `analytics/` owns:

- Demand forecasting (Prophet + moving average)
- Customer retention modeling
- Waterfall demand split (new vs repeat)
- Reorder schedule simulation
- SKU ranking & flavor mapping

### `etl/` owns:

- API authentication & credential refresh
- Data fetching from external services
- Source-specific normalization (ASIN → SKU, etc.)
- Sync orchestration & event logging

### `db.py` owns:

- Schema definition & initialization
- Connection management (WAL mode, context manager)
- Upsert/query helpers
- Settings persistence (app_settings table)

### `config.py` owns:

- .env file loading
- DB-persisted credential overlay
- Module-level globals
- Config reload on demand
