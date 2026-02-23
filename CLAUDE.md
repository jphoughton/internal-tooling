# Hydrant Command Center — Inventory & Demand Forecasting Dashboard

## Overview

Multi-channel e-commerce analytics dashboard for **Hydrant** (DTC hydration mix brand). Forecasts demand across Shopify (DTC) and Amazon, tracks customer retention and lifetime value, recommends reorder timing using 3PL (Packiyo) and FBA inventory, and optimizes media spend ROI through waterfall demand modeling. Built with Streamlit + SQLite + Facebook Prophet.

## Tech Stack

- **Frontend**: Streamlit 1.50.0 (modular multi-page app — thin router + 11 page modules)
- **Backend**: Python 3.x, SQLite (WAL mode for concurrent reads)
- **Forecasting**: Facebook Prophet 1.1.5+ (daily → monthly time series)
- **Data Processing**: Pandas 2.1+, NumPy 1.24+
- **Charts**: Plotly 5.18+
- **APIs**: Amazon SP-API, Shopify Admin API, Packiyo REST API, Google Sheets (public CSV)
- **Scheduling**: `schedule` library (daemon mode)
- **Hosting**: Railway (Docker + supervisord) and local (localhost:8501)

## Architecture

```
Inventory/
├── dashboard.py           # Thin router: sidebar, auth, notification bar, page dispatch (~575 lines)
├── config.py              # Environment & credentials management
├── db.py                  # PostgreSQL/SQLite adapter, schema, connection helpers, upserts
├── scheduler.py           # Daemon scheduler for daily ETL sync
├── requirements.txt       # Python dependencies
├── run.sh                 # Launch script
├── launch.command         # macOS double-click launcher
│
├── pages/                 # One file per dashboard page — each exports render(ctx)
│   ├── overview.py        # KPIs, revenue trends, source split, inventory snapshot
│   ├── retention.py       # Cohort matrix heatmap, retention curve, seasonality editor
│   ├── demand_forecast.py # Waterfall chart, media spend editor, 5 SKU-by-month tables
│   ├── projected_inventory.py # Planned inbound editor, inventory runway projections
│   ├── inventory_3pl.py   # Packiyo 3PL stock levels, forecast vs inventory
│   ├── inventory_amazon.py# FBA stock levels, forecast vs inventory
│   ├── reorder_alerts.py  # Urgency-ranked reorder plan, runway charts, timeline Gantt
│   ├── fba_transfers.py   # 3PL→FBA transfer urgency, timeline Gantt
│   ├── marketing.py       # Pacing tables, DoD/WoW/MoM with gradient coloring
│   ├── financials.py      # Bank transaction import, P&L, cash flow charts
│   └── settings.py        # API credentials, data imports, Google Sheets sync
│
├── ui/                    # Reusable Streamlit UI building blocks
│   ├── styles.py          # Global CSS (Hydrant brand), Plotly theme, nav section CSS
│   ├── components.py      # HTML table renderer, freshness badge, date filter, auth
│   ├── charts.py          # Plotly chart factories (timeline/Gantt charts)
│   └── tables.py          # DataFrame formatting, gradient styling, SKU display helpers
│
├── utils/                 # Shared utilities
│   ├── date_helpers.py    # month_str, parse_month, add_months, month_diff
│   └── constants.py       # FORECAST_SKUS, TW_ADJUSTMENT, seasonal indices, pacing bounds
│
├── analytics/             # Demand & retention calculation modules
│   ├── forecast.py        # Prophet-based SKU demand forecasting
│   ├── waterfall.py       # New vs. repeat demand split engine
│   ├── retention.py       # Cohort analysis: repurchase rates + SKU lifecycle
│   ├── reorder.py         # Inventory runway simulation & reorder schedule
│   ├── sku_flavors.py     # SKU → flavor name mapping (17 core SKUs)
│   └── dtc_demand.py      # Master DTC rollup: 4 forecast tables combined
│
├── etl/                   # Data integration pipelines
│   ├── sync.py            # Daily orchestration: triggers all syncs
│   ├── amazon.py          # Sales & Traffic reports (SP-API)
│   ├── amazon_inventory.py# FBA inventory levels (multi-strategy fallback)
│   ├── amazon_sku_map.py  # ASIN ↔ Master SKU mapping (23 entries)
│   ├── customer_id.py     # Shared Shopify customer ID generation
│   ├── shopify_client.py  # Order/customer sync + auto-token refresh
│   ├── shopify_oauth.py   # OAuth flow for Shopify token
│   ├── shopify_bulk_import.py # Bulk JSONL operations handler
│   ├── packiyo_client.py  # 3PL real-time inventory query
│   ├── google_sheets.py   # Public Sheet CSV import (no OAuth)
│   └── klaviyo_client.py  # Klaviyo email metrics (configured, not actively synced)
│
├── tests/                 # Test suite
│   ├── conftest.py        # Pytest fixtures, read-only DB connection
│   ├── test_data_quality.py # 32 data quality tests (revenue, dates, integrity)
│   ├── test_date_helpers.py # Date math utility tests
│   ├── test_sku_map.py    # ASIN→SKU mapping tests
│   ├── test_customer_id.py# Customer ID generation tests
│   └── test_constants.py  # Constants sanity checks
│
├── .streamlit/
│   └── config.toml        # Streamlit theme config
│
├── data/
│   ├── inventory.db       # SQLite database (gitignored, local only)
│   └── seed.sql.gz        # Real data seed (Git LFS, auto-restored on first run)
│
├── .env                   # API credentials (gitignored)
├── .mcp.json              # MCP servers (committed — auto-loads for all Claude Code users)
├── .claude/
│   └── settings.json      # Claude Code permissions (committed — shared with team)
│
├── setup-git-autopilot.sh # One-time setup script (hooks, linting, MCPs, permissions)
└── .env.example           # Onboarding template for env vars
```

## MCP Servers (auto-configured via .mcp.json)

These load automatically for anyone using Claude Code on this repo — no manual config needed:

| Server                  | What it does                                                                                   | Auth needed            |
| ----------------------- | ---------------------------------------------------------------------------------------------- | ---------------------- |
| **context7**            | Real-time, version-specific library documentation. Prevents using outdated/deprecated methods. | None                   |
| **sequential-thinking** | Structured problem-solving for complex architectural decisions.                                | None                   |
| **playwright**          | Browser automation — Claude can click, type, screenshot, and verify the app works.             | None                   |
| **github**              | PR/issue management, CI/CD status, code review.                                                | `GITHUB_TOKEN` env var |
| **sqlite**              | Direct SQL queries against `inventory.db` — debug ETL, verify forecasts, inspect data.         | None                   |

To add more MCPs: edit `.mcp.json`, commit, push. Every teammate gets them on next pull.

### Claude Code Permissions (.claude/settings.json)

Pre-configured safe defaults committed to the repo:

- **Allowed**: python, pip, streamlit, git read ops, git commit, npm run, prettier, eslint, playwright
- **Denied**: reading .env files, `rm -rf`, `git push --force`

## Data Flow

```
EXTERNAL APIs              ETL                     DATABASE              ANALYTICS          DASHBOARD
──────────────────────────────────────────────────────────────────────────────────────────────────────

Amazon SP-API ──────► amazon.py ──────────────► daily_sku_sales ─┐
  (flat-file, 30d)    map ASIN→Master SKU       (source='amazon') │
                                                                    ├──► forecast.py (Prophet)
Shopify Admin ──────► shopify_client.py ──────► customers     ────┤
  (/orders.json)      fetch_orders()          ► orders            ├──► waterfall.py (demand split)
                                              ► order_items       │
                                              ► daily_sku_sales   ├──► retention.py (cohort)
                                               (source='shopify') │
Packiyo 3PL ────────► packiyo_client.py ──────► [real-time only]──┤
  (inventory)          get_inventory()                             ├──► reorder.py (runway sim)
                                                                    │
FBA Inventory ──────► amazon_inventory.py ────► [real-time only]──┘    ──► pages/*.py
                                                                            (11 pages)
Google Sheets ──────► google_sheets.py ───────► google_sheet_data
  (public CSV)

Manual Input ───────► Dashboard UI ───────────► media_spend
  (settings page)                              ► amazon_revenue_forecast
                                               ► planned_inbound
                                               ► seasonal_indices
```

**Key pattern**: Amazon data goes directly to `daily_sku_sales` (no customer-level detail). Shopify data goes through `orders` → `order_items` → rebuilt into `daily_sku_sales`.

## Database Schema (SQLite)

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

### Reorder Defaults (analytics/reorder.py)

- `LEAD_TIME_WEEKS` = 12 (84 days)
- `MOQ_UNITS` = 5,000
- `SAFETY_STOCK_WEEKS` = 2

### Seasonal Indices (hydration product seasonality)

Jan: 0.95, Feb: 0.92, Mar: 0.98, Apr: 1.02, May: 1.05, Jun: 1.10, Jul: 1.12, Aug: 1.08, Sep: 1.02, Oct: 0.98, Nov: 0.92, Dec: 0.88

### Triple Whale Correction

`_TW_ADJ = 0.5` — Triple Whale overstates attribution by ~2x; all TW-sourced data is halved.

### Reorder Urgency Tiers

- `OVERDUE` — Reorder date has passed
- `ORDER NOW` — < 7 days until reorder
- `ORDER SOON` — < 21 days
- `UPCOMING` — < 42 days
- `OK` — 42+ days
- `EN ROUTE` / `EN ROUTE ⚠️` — Planned inbound exists

## Key Algorithms

### Waterfall Demand Split (waterfall.py)

Splits total demand into repeat (retention curve) + new customer components:

```
For each forecast month:
  new_customers = (media_spend × ROAS) / new_customer_AOV
  repeat_units = Σ(each past cohort × retention_curve[months_since] × units_per_repeat)
  total = (new + repeat) × seasonal_index[calendar_month]
```

### Retention Curve (waterfall.py → get_average_retention_curve)

- Recency-weighted: Last 12 cohorts = 60%, next 12 = 30%, rest = 10%
- Auto-detects contaminated cohorts (M1 retention > 40%)
- Decay rate 0.98, terminal floor 0.5%
- Extrapolates 60 months beyond observed data

### Reorder Schedule (reorder.py)

Day-by-day forward simulation:

1. Start with current 3PL + FBA stock
2. Deduct daily demand (from monthly forecast spread evenly)
3. Add planned inbound arrivals
4. When projected inventory < reorder point over next (lead_time + safety), place order
5. Order qty = ceil(cover_demand / MOQ) × MOQ

### Prophet Forecasting (forecast.py)

- Weekly seasonality: always on
- Yearly seasonality: on if > 180 days history
- Fallback to moving average if < 60 days history
- 80% confidence interval

## Dashboard Pages

Each page is a standalone module in `pages/` exporting a `render(ctx)` function. The `ctx` dict provides shared cached functions and state from the router.

| #   | Page                | Module                         | Key Features                                                 |
| --- | ------------------- | ------------------------------ | ------------------------------------------------------------ |
| 1   | Overview            | `pages/overview.py`            | KPIs, revenue trends by source, top SKUs, inventory snapshot |
| 2   | Retention           | `pages/retention.py`           | Cohort heatmap, retention curve, seasonality editor          |
| 3   | Demand Forecast     | `pages/demand_forecast.py`     | Waterfall chart, media spend editor, 5 SKU-by-month tables   |
| 4   | Projected Inventory | `pages/projected_inventory.py` | Planned inbound editor, inventory runway projections         |
| 5   | 3PL Inventory       | `pages/inventory_3pl.py`       | Packiyo stock levels, forecast vs inventory comparison       |
| 6   | Amazon Inventory    | `pages/inventory_amazon.py`    | FBA stock levels, forecast vs inventory comparison           |
| 7   | Reorder Alerts      | `pages/reorder_alerts.py`      | Urgency-ranked table, runway charts, timeline Gantt          |
| 8   | FBA Transfers       | `pages/fba_transfers.py`       | Transfer urgency alerts, 3PL→FBA timeline Gantt              |
| 9   | Marketing           | `pages/marketing.py`           | Pacing tables, DoD/WoW/MoM with gradient coloring            |
| 10  | Financials          | `pages/financials.py`          | Bank transaction import, P&L, cash flow charts               |
| 11  | Settings            | `pages/settings.py`            | API credentials, data imports, Google Sheets sync            |

### Page Module Convention

```python
# pages/example.py
"""Example page."""
import streamlit as st
from db import get_db, read_sql
from ui.components import render_html_table, render_freshness_badge

def render(ctx):
    """Render the page. ctx contains shared cached functions and state."""
    forecast_skus = ctx['forecast_skus']
    cached_waterfall = ctx['cached_waterfall']
    # ... page implementation ...
```

### UI Patterns

- `st.segmented_control` for channel filters (NOT st.button — buttons don't maintain state across reruns)
- `gradient_perf_style()` in `ui/tables.py` for DoD/WoW/MoM — smooth 5-tier color gradient
- `render_html_table()` in `ui/components.py` for all styled HTML tables
- `render_freshness_badge()` in `ui/components.py` for data freshness indicators
- `render_timeline_chart()` in `ui/charts.py` for Gantt-style timeline charts
- Slim single-line notification banner at top for urgent reorder alerts (in `dashboard.py` router)
- All SKU tables sorted by best-seller rank (use `get_sku_sales_rank()`)
- `@st.cache_data(ttl=...)` used extensively (10s to 1800s depending on computation cost)

## Testing

```bash
pytest tests/ -v              # Run all tests (data quality + unit tests)
pytest tests/test_date_helpers.py -v  # Run specific test file
```

| Test file              | What it tests                                                     | Count |
| ---------------------- | ----------------------------------------------------------------- | ----- |
| `test_data_quality.py` | Revenue sanity, date validity, referential integrity, sync health | 32    |
| `test_date_helpers.py` | Date math utilities (add_months, month_diff, parse/format)        | 21    |
| `test_sku_map.py`      | ASIN→SKU mapping, seller SKU resolution, registration             | 17    |
| `test_customer_id.py`  | Customer ID generation (REST, GraphQL, email fallback)            | 12    |
| `test_constants.py`    | Constants sanity (SKU count, seasonal indices, TW adjustment)     | 9     |

## Environment Variables (.env)

**Amazon SP-API**: AMAZON_REFRESH_TOKEN, AMAZON_LWA_CLIENT_ID, AMAZON_LWA_CLIENT_SECRET, AMAZON_MARKETPLACE_ID (default: ATVPDKIKX0DER = US)

**Shopify**: SHOPIFY_STORE_URL, SHOPIFY_ACCESS_TOKEN, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_API_VERSION (default: 2024-01)

**Packiyo**: PACKIYO_API_URL (default: https://aveshops.packiyo.com/api/v1), PACKIYO_API_TOKEN, PACKIYO_CUSTOMER_ID (default: 12)

**Mode**: USE_MOCK_DATA (default: true)

## Running the App

```bash
# Development (mock data)
python mock_data.py                # Generate test data
streamlit run dashboard.py         # Launch on localhost:8501

# Production (live APIs)
# Set .env credentials first
python scheduler.py                # Daemon mode — syncs daily at 6 AM
python scheduler.py --now          # Single sync + exit
python scheduler.py --full         # Full historical pull + exit
streamlit run dashboard.py         # Dashboard (separate terminal)
```

Auto-sync: If last sync > 24h ago, dashboard triggers sync on page load.

---

## Git Conventions (AUTO-ENFORCED)

Commits follow **Conventional Commits** (enforced by commitlint + husky):

| Type       | When to use                               | Example                                                  |
| ---------- | ----------------------------------------- | -------------------------------------------------------- |
| `feat`     | New feature or capability                 | `feat(forecast): add seasonal demand multipliers`        |
| `fix`      | Bug fix                                   | `fix(reorder): respect planned inbound in stockout calc` |
| `refactor` | Code restructuring, no new feature or fix | `refactor: extract API helper functions`                 |
| `docs`     | Documentation only                        | `docs: update CLAUDE.md with new schema`                 |
| `style`    | Formatting, no logic change               | `style: fix indentation in dashboard`                    |
| `test`     | Adding or updating tests                  | `test(retention): add cohort contamination test`         |
| `perf`     | Performance improvement                   | `perf(waterfall): cache retention curve for 5min`        |
| `build`    | Build system or dependency changes        | `build: add klaviyo-api to requirements`                 |
| `ci`       | CI/CD config changes                      | `ci: add staging deploy workflow`                        |
| `chore`    | Maintenance, no production code change    | `chore: clean up unused imports`                         |
| `revert`   | Reverting a previous commit               | `revert: undo cart redesign`                             |
| `wip`      | Work in progress (feature branches only)  | `wip: checkout flow partial implementation`              |

### Scopes (what part of the app)

```
feat(forecast): ...   # Prophet / waterfall / demand
feat(reorder): ...    # Reorder alerts / runway
feat(retention): ...  # Cohort analysis / LTV
feat(etl): ...        # Data sync / API integrations
feat(ui): ...         # Dashboard layout / styling
feat(db): ...         # Database schema / migrations
feat(marketing): ...  # Marketing page / pacing
feat(overview): ...   # Overview page
```

### Branch Workflow

```
main (production)
  └── develop (integration)
        ├── feat/seasonal-demand
        ├── feat/fba-transfers
        ├── fix/reorder-planned-inbound
        └── refactor/overview-page
```

1. Branch from `develop`: `git checkout -b feat/my-feature develop`
2. Work + commit with conventional messages
3. Push + create PR to `develop`
4. Merge to `main` for releases

## Code Style (AUTO-ENFORCED)

- **Python**: 4-space indentation, single quotes preferred
- **Prettier** auto-formats JS/JSON/MD on save
- **ESLint** auto-fixes on save
- **lint-staged** runs on every commit
- **EditorConfig** ensures consistent whitespace across editors

## Git Autopilot — Setup & How It Works

### One-Time Setup

Run once in the project root to install all hooks, tooling, MCPs, and permissions:

```bash
bash setup-git-autopilot.sh
```

**Requires Node.js** (for husky, commitlint, lint-staged). Installs these dev dependencies:

- **husky** — Git hooks manager (pre-commit, commit-msg, pre-push)
- **@commitlint/cli + config-conventional** — Rejects non-conventional commit messages
- **lint-staged** — Auto-lints only staged files before commit
- **prettier** — Code auto-formatter (JSON, MD, YAML, CSS, JS/TS)

### Config Files Created by Setup

| File                      | Purpose                                                           |
| ------------------------- | ----------------------------------------------------------------- |
| `.mcp.json`               | MCP servers — auto-loaded for all Claude Code users               |
| `.claude/settings.json`   | Claude Code permissions (allow/deny rules)                        |
| `commitlint.config.js`    | Allowed commit types + rules (max 100 char subject)               |
| `.lintstagedrc.json`      | Which file types get linted/formatted on commit                   |
| `.prettierrc.json`        | Formatting rules (single quotes, trailing commas, 100 char width) |
| `.husky/pre-commit`       | Runs `npx lint-staged`                                            |
| `.husky/commit-msg`       | Runs `npx commitlint --edit`                                      |
| `.husky/pre-push`         | Runs `npm test` if test script exists                             |
| `.vscode/settings.json`   | Auto-format on save, git branch protection, editor defaults       |
| `.vscode/extensions.json` | Recommended extensions (auto-prompted on project open)            |
| `.editorconfig`           | Universal whitespace rules (works in any editor)                  |
| `.env.example`            | Onboarding template — shows new devs what tokens they need        |

### New Dev Onboarding (after cloning)

1. `npm install` — hooks auto-install via husky
2. `cp .env.example .env` — fill in `GITHUB_TOKEN` and API credentials
3. Open in VS Code — accept extension recommendations
4. That's it. MCP servers, formatting, linting, commit enforcement all activate automatically.

### What Happens Automatically After Setup

| When you...            | What happens automatically               |
| ---------------------- | ---------------------------------------- |
| **Save a file**        | Prettier formats it, ESLint fixes issues |
| **Stage & commit**     | lint-staged re-checks staged files       |
| **Write a commit msg** | commitlint validates the format          |
| **Push to remote**     | Tests run first (if they exist)          |
| **Open the project**   | VS Code prompts recommended extensions   |

### VS Code Shortcuts

- **Conventional Commits extension**: Click the checkmark icon in Source Control panel for commit GUI
- **GitLens**: Hover over any line to see who changed it and when
- **Git Graph**: Click "Git Graph" in bottom status bar for visual branch history

### Git Aliases (added by setup)

```bash
git lg          # Pretty one-line log with graph
git last        # Show last commit details
git st          # Short status
git branches    # All branches sorted by recent
git undo        # Undo last commit (keep changes)
git amend       # Add to last commit without changing message
```

### Emergency Hook Skip

```bash
git commit -m "fix: emergency hotfix" --no-verify
```

## Rules for AI

### Architecture

- **Page changes go in `pages/` modules** — dashboard.py is a thin router, do NOT add page code to it
- **New UI components go in `ui/`** — reusable widgets, chart factories, table formatters
- **Shared constants go in `utils/constants.py`** — no magic numbers in page modules
- **Shared date math goes in `utils/date_helpers.py`** — do NOT define \_month_str etc. locally
- **Customer ID generation uses `etl/customer_id.py`** — do NOT duplicate in Shopify modules
- Each page module exports `render(ctx)` — receives shared cached functions via the ctx dict

### Code Quality

- Always use conventional commit messages (enforced by hooks)
- Run the app and verify changes before committing: `streamlit run dashboard.py`
- Run tests before committing: `pytest tests/ -v`
- Don't modify .env or credentials — instruct the user to do it
- Don't add new dependencies without explaining why in the commit message
- All SKU displays must be sorted by best-seller rank (use `get_sku_sales_rank()`)
- Use `@st.cache_data(ttl=N)` for expensive computations
- Use `st.segmented_control` for stateful toggles, NOT `st.button`
- Use `gradient_perf_style()` from `ui/tables.py` for DoD/WoW/MoM displays
- Use `render_html_table()` from `ui/components.py` instead of st.dataframe
- Use `render_timeline_chart()` from `ui/charts.py` for Gantt charts
- Amazon data never goes through the `orders` table — only `daily_sku_sales`
- **After any deploy or UI change**: Use Playwright MCP to screenshot each page and verify rendering
- Pages must never show completely empty — always provide a database fallback when API creds are missing

## Current State

- **Working**: Full dashboard with 11 pages, ETL for Amazon + Shopify + Packiyo + Google Sheets, Prophet forecasting, waterfall demand split, reorder alerts with urgency tiers, FBA transfer alerts, gradient DoD/WoW/MoM coloring, channel-filtered marketing page, notification banner, bank transaction import (Financials page)
- **Architecture**: Modular — thin router (575 lines) + 11 page modules + ui/ + utils/ packages, 91 unit tests
- **Known Issues**: None critical
- **Next Up**: Klaviyo integration, more analytics unit tests (waterfall, reorder simulation)
