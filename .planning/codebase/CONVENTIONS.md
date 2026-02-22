# Conventions

## Code Style

- **Indentation**: 4 spaces (Python), 2 spaces (JSON/YAML/JS)
- **Quotes**: Single quotes preferred in Python and JS
- **Line length**: 100 characters (Prettier), no hard limit in Python
- **Imports**: Standard library → third-party → local, grouped with blank lines
- **Trailing commas**: Yes (Prettier enforced for JS/JSON)

## Naming Patterns

### Functions (real examples)

```python
# Public APIs
forecast_sku(sku, conn)
get_customer_cohort_data(conn, source)
compute_reorder_schedule(inventory, forecast, planned)
build_waterfall(media_plan, conn)

# Private/cached helpers
_cached_retention_curve(source)
_smart_date_filter(key, default_range)
_gradient_perf_style(df, metric_cols)
_render_df_as_html_global(df, height)
```

### Constants

```python
FORECAST_SKUS = {"ENBP-BP0030-LEB0", "HYBP-BP0030-CHLB0", ...}  # 17 core SKUs
LEAD_TIME_WEEKS = 12
MOQ_UNITS = 5_000
SAFETY_STOCK_WEEKS = 2
_TW_ADJ = 0.5  # Triple Whale correction factor
```

### Database fields

```python
source='amazon'    # or 'shopify'
urgency='OVERDUE'  # or 'ORDER NOW', 'ORDER SOON', 'UPCOMING', 'OK'
```

## Common Patterns

### Caching

```python
@st.cache_data(ttl=600)  # 10 minutes for expensive analytics
def _cached_retention_curve(source):
    ...

@st.cache_data(ttl=10)   # 10 seconds for DB reads
def load_sku_list():
    ...
```

TTL ranges: 10s (DB reads) → 600s (analytics) → 1800s (very expensive)

### Database Access

```python
with get_db() as conn:
    df = pd.read_sql(query, conn, params=params)
```

All DB access goes through `get_db()` context manager. WAL mode for concurrent reads.

### UI Toggles

```python
# CORRECT — maintains state across Streamlit reruns
channel = st.segmented_control("Channel", ["All", "DTC", "Amazon"], default="All")

# WRONG — buttons don't maintain state
if st.button("DTC"):  # Don't use this for toggles
```

### Performance Tables

```python
# DoD/WoW/MoM always use gradient styling
styled = _gradient_perf_style(df, ['DoD', 'WoW', 'MoM'])
# 5-tier: deep green (#064e3b) → light green → amber → light red → deep red (#7f1d1d)
```

### SKU Display

```python
# Always sort by best-seller rank (last 90 days sales volume)
ranked_skus = get_sku_sales_rank(conn)
df = df.sort_values('rank')
```

## Error Handling

- ETL: try/except around API calls, errors logged to `sync_log` table
- Dashboard: `st.error()` for user-facing errors, fallback data when APIs unavailable
- Config: Silent fallback if DB credentials unavailable (loads from .env instead)
- Prophet: Falls back to moving average if < 60 days history
- Pages: Never show completely empty — always provide database fallback

## Git Conventions

### Conventional Commits (enforced by commitlint + husky)

```
feat(forecast): add seasonal demand multipliers
fix(reorder): respect planned inbound in stockout calc
refactor: extract API helper functions
docs: update CLAUDE.md with new schema
```

**Types**: feat, fix, refactor, docs, style, test, perf, build, ci, chore, revert, wip
**Scopes**: forecast, reorder, retention, etl, ui, db, marketing, overview

### Branch Workflow

```
main (production)
  └── develop (integration)
        ├── feat/seasonal-demand
        ├── fix/reorder-planned-inbound
        └── refactor/overview-page
```

## Formatting & Linting

| Tool         | Config File            | What It Does                                   |
| ------------ | ---------------------- | ---------------------------------------------- |
| Prettier     | `.prettierrc.json`     | Formats JSON, MD, YAML, CSS, JS/TS on commit   |
| commitlint   | `commitlint.config.js` | Rejects non-conventional commit messages       |
| lint-staged  | `.lintstagedrc.json`   | Runs linters only on staged files              |
| Husky        | `.husky/`              | Manages pre-commit, commit-msg, pre-push hooks |
| EditorConfig | `.editorconfig`        | Consistent whitespace across editors           |

**Auto-enforced on every commit**: Prettier formats staged files, commitlint validates message.
