# Concerns

## Technical Debt

### 1. Monolithic Dashboard (~5,400 lines)

**File**: `dashboard.py`

The entire UI is in one file: 10 pages, inline CSS, caching logic, state management. Makes it hard to assign different pages to different developers, test individual pages, or refactor without conflicts.

**Recommendation**: Split into `dashboard/pages/`, `dashboard/components/`, `dashboard/styles/`.

### 2. Tight Database-Analytics Coupling

**Files**: `analytics/forecast.py`, `analytics/retention.py`, `analytics/waterfall.py`

All analytics modules directly call `get_db()` and issue SQL queries. No abstraction layer between analytics and data access. Testing analytics requires a real database.

### 3. Hardcoded Constants

- `dashboard.py`: `FORECAST_SKUS` (17 SKUs hardcoded)
- `analytics/reorder.py`: `LEAD_TIME_WEEKS=12`, `MOQ_UNITS=5000`, `SAFETY_STOCK_WEEKS=2`
- `dashboard.py`: `_TW_ADJ = 0.5` (Triple Whale correction)
- `analytics/waterfall.py`: Retention decay rate (0.98), terminal floor (0.5%)

Users cannot adjust these without modifying code. Should live in `app_settings` with Settings page UI.

### 4. Silent Error Handling

- `config.py`: `except Exception: pass` silently swallows credential load failures
- `etl/sync.py`: Exceptions caught but only printed (no structured persistence)
- No structured logging (using print instead of logging module)

## Security

### 1. Credential Handling

- Credentials stored as module-level globals in `config.py`
- If error traces leak, full API access is compromised
- No credential masking in logs

### 2. SQL Patterns

String interpolation for `IN` clauses in some queries. Currently safe but fragile — a future edit could introduce injection. Should use parameterized query builders.

### 3. Missing Input Validation

Settings page accepts unchecked values:

- Seasonal indices can be negative/zero
- Media spend can be negative
- API URLs not validated

## Performance

### 1. Missing Database Indexes

**File**: `db.py`

Currently indexed: order_date, customer_id, sku, sale_date.

**Missing**:

- `daily_sku_sales(source)` — waterfall filters by source
- `daily_sku_sales(sku, source)` — composite for forecasts
- `orders(source, order_date)` — retention queries
- `customers(source, first_order_date)` — cohort analysis

**Impact**: Table scans on large datasets. **Fix**: Add 4 indexes for 5-10x speedup (< 1 day effort).

### 2. N+1 Query in Waterfall

**File**: `analytics/waterfall.py`

`_get_contaminated_cohort_rates()` loops and queries per cohort × per month. With 24 cohorts × 6 months = ~150 queries instead of 1-2.

### 3. Cache Invalidation

**File**: `dashboard.py`

Cache keys use full JSON serialization of media plan. Changing one value invalidates the entire 30-second computation.

## Fragile Areas

### 1. Prophet Fallback Untested

**File**: `analytics/forecast.py`

Moving average fallback (< 60 days history) uses arbitrary 0.7/1.3 confidence multipliers. No way to know which SKUs use fallback. If a new SKU launches with a spike, it will overestimate permanently.

### 2. Retention Contamination Detection

**File**: `analytics/waterfall.py`

Uses magic number thresholds: M1 retention > 40% = contaminated. For subscription products, 40% M1 might be normal. Thresholds should be configurable.

### 3. Reorder Assumes Constant Daily Demand

**File**: `analytics/reorder.py`

Spreads monthly demand evenly across days, ignoring intra-month seasonality, day-of-week patterns, and promotional events.

### 4. Waterfall Chains Fragile Components

**File**: `analytics/waterfall.py`

Depends on: user-entered media spend (unvalidated), retention curve (auto-detects contamination), AOV (may be stale), seasonal indices, source filters. Failures cascade silently.

## Missing Infrastructure

- **No CI/CD pipeline** — No GitHub Actions, no staging environment, no automated tests
- **No monitoring/alerting** — Can't tell if ETL syncs fail, forecasts go stale, or API credentials expire
- **No data validation** — Data from APIs arrives unchecked (duplicates, malformed SKUs, negative prices)
- **No audit log** — Can't track who changed settings or when credentials were updated
- **No database migrations** — Schema changes require manual intervention

## Prioritized Recommendations

| Priority | Issue                        | Impact                   | Effort            |
| -------- | ---------------------------- | ------------------------ | ----------------- |
| 1        | Add database indexes         | 5-10x query speedup      | Low (< 1 day)     |
| 2        | Fix N+1 queries in waterfall | Major perf improvement   | Low (< 1 day)     |
| 3        | Add test suite (pytest)      | Enables safe refactoring | Medium (3-5 days) |
| 4        | Input validation in Settings | Prevents garbage data    | Low (1 day)       |
| 5        | Split dashboard.py           | Unblocks all future work | High (3-4 days)   |

### Quick Wins (< 1 day each)

1. Database indexes
2. N+1 query fix
3. Structured logging (replace print with logging module)

### Strategic Work (1+ weeks)

1. Dashboard modularization
2. Test suite
3. CI/CD pipeline
4. Data validation layer
