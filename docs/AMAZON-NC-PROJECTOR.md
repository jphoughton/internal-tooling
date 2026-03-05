# Amazon NC Projector — Implementation Handoff

## Problem

Amazon's Fulfilled Shipments report (the source of customer-level data) lags 2-3 days behind the Sales & Traffic report. When the team checks yesterday's performance each morning, Amazon NC customer count and NC revenue show as 0 or stale. We need projected values to fill the gap until actuals arrive.

## Solution Built

`analytics/amazon_nc_projector.py` — a 4-method ensemble that projects yesterday's Amazon NC customers and NC revenue using signals that arrive before the fulfillment report.

### Backtest Results (60 days)

| Metric | NC Customers | NC Revenue |
|--------|-------------|------------|
| Trimmed MAPE (P90) | 13.2% | 16.2% |
| Median APE | 11.6% | 15.4% |
| Raw MAPE | 25.7% | 28.0% |
| R-squared | 0.025 | -0.048 |
| Bias | +2.1 overpredicts | +$29 |
| Outlier days (>50% err) | 6 / 60 | — |

**Translation**: On a typical day (~34 NCs), we're off by about 4 customers. Half the time we're within 11.6%. About once a week we'll be significantly off (usually Fridays/Saturdays). We slightly overpredict.

### Accuracy by Day of Week

| Day | NC MAPE | Rev MAPE | Rating |
|-----|---------|----------|--------|
| Mon | 7.5% | 12.1% | Good |
| Tue | 20.2% | 19.7% | Fair |
| Wed | 14.1% | 16.8% | Good |
| Thu | 21.3% | 28.8% | Fair |
| Fri | 71.1% | 73.7% | Poor |
| Sat | 34.0% | 30.6% | Poor |
| Sun | 17.3% | 19.8% | Fair |

Friday/Saturday errors are dominated by 2-3 extreme anomaly days (e.g., Feb 20 had 5 actual NCs vs normal 30+ — likely stockout or data issue).

## Architecture

### Files

- `analytics/amazon_nc_projector.py` — the algorithm (already built and tested)
- `scripts/backtest_nc_projector.py` — backtest harness (run to validate changes)

### Key Functions

```python
from analytics.amazon_nc_projector import project_amazon_nc

# Get projection for yesterday (default)
result = project_amazon_nc()

# Get projection for a specific date
from datetime import date
result = project_amazon_nc(target_date=date(2026, 3, 4))

# With existing DB connection
with get_db() as conn:
    result = project_amazon_nc(target_date=date(2026, 3, 4), conn=conn)
```

### Return Value

```python
{
    'target_date': date(2026, 3, 4),
    'projected_nc_customers': 32.0,      # float, projected NC count
    'projected_nc_revenue': 1304.00,      # float, projected NC revenue
    'confidence': 0.717,                  # float 0-1, how confident we are
    'methods_used': ['ridge_ml', 'ewma_dow', 'revenue_scaled', 'dtc_nc_ratio'],
    'method_details': { ... },            # per-method projections
    'actual_nc_customers': 25,            # int or None if not yet available
    'actual_nc_revenue': 935.0,           # float or None if not yet available
}
```

### When to Show Projected vs Actual

```python
result = project_amazon_nc()
if result['actual_nc_customers'] is not None:
    # Actual data has arrived — use it, ignore projection
    nc = result['actual_nc_customers']
    rev = result['actual_nc_revenue']
    is_projected = False
else:
    # No actual data yet — show projection
    nc = result['projected_nc_customers']
    rev = result['projected_nc_revenue']
    is_projected = True
```

## 4 Ensemble Methods

### 1. Ridge Regression (`ridge_ml`)
- Scikit-learn Ridge (alpha=10.0) trained on rolling history
- Features: nc_ma7/14/28, nc_ewma7/14, nc_same_dow, trend_7_14, seasonal_idx, is_weekend, is_month_end, amz_revenue, dtc_nc, dtc_spend, amz_spend, nc_frac_l7, amz_dtc_nc_ratio
- Retrains on every call using all data before the target date (no data leakage)
- Confidence derived from recent-14-day R-squared

### 2. Same-DOW EWMA with Bias Correction (`ewma_dow`)
- Exponentially weighted average of last 6 same-weekday NC values (alpha=0.5)
- Tracks recent prediction errors on same-DOW days and subtracts 70% of average bias
- Trend adjustment from overall L7D EWMA vs L14D MA
- Best for capturing weekday-specific patterns

### 3. Revenue-Scaled NC (`revenue_scaled`)
- If Amazon total revenue is known for the target day (S&T report arrives before fulfillment), applies DOW-specific NC-per-dollar ratio
- Uses last 8 same-DOW days for the ratio calculation
- Strong when revenue data is available but NC data is not

### 4. DTC NC Ratio (`dtc_nc_ratio`)
- Uses same-day Shopify NC count (available near real-time) as a leading indicator
- Applies DOW-specific Amazon-to-Shopify NC ratio from trailing history
- Captures DTC ad spend halo effect on Amazon NC

### Ensemble Logic
- Takes **median** of all method predictions (robust to one method being way off)
- Also computes confidence-weighted mean — if median and mean agree within 15%, uses median; otherwise averages them
- Applies IQR-based guardrails (caps projection within 1.5x IQR of recent 14-day history)
- Revenue = projected NC count x DOW-specific trailing NC AOV

## Data Sources

All customer/NC data comes from the main PostgreSQL database. Google Sheets is ONLY used for spend data.

| Signal | Source Table | Timing | Used By |
|--------|-------------|--------|---------|
| Amazon NC history | `orders` + `customers` + `order_items` (source='amazon') | Lags 2-3 days | All methods |
| Amazon total revenue | `daily_sku_sales` (source='amazon') | Lags 1-2 days | revenue_scaled, ridge_ml |
| Amazon ad spend | `amazon_daily_rollup` | Lags 2-4 days | ridge_ml |
| DTC ad spend | `google_sheet_data.blended_ad_spend` (SPEND ONLY) | Same-day | ridge_ml |
| Shopify NC count | `orders` + `customers` (source='shopify') | Near real-time | dtc_nc_ratio, ridge_ml |
| Seasonal indices | `utils/constants.py` DEFAULT_SEASONAL_INDICES | Static | ridge_ml |

## Implementation Tasks

### 1. Add projection to Overview page (`views/overview.py`)

In `_load_amazon_daily()`, after loading the data, check if yesterday's row has NC data. If not, call `project_amazon_nc()` and fill in the projected values.

```python
from analytics.amazon_nc_projector import project_amazon_nc

# After loading df in _load_amazon_daily:
yesterday = (date.today() - timedelta(days=1)).isoformat()
if yesterday not in df['sale_date'].values or df[df['sale_date'] == yesterday]['new_customers'].sum() == 0:
    proj = project_amazon_nc()
    if proj['projected_nc_customers'] > 0:
        # Insert or update yesterday's row with projected values
        # Mark as projected so UI can distinguish
        ...
```

### 2. Add projection to Pacing page (`views/pacing.py`)

The pacing page calls `get_nc_stats()` from `analytics/metrics.py` for yesterday's Amazon NC. When this returns 0, fall back to the projector.

### 3. Visual treatment for projected data

Projected values MUST be visually distinct from actuals:
- Add a label like "(est)" or "(projected)" suffix
- Use a different color or opacity (e.g., lighter shade, dashed border)
- Show confidence: "~32 NCs (est, 72% conf)" or "32 +/- 8 NCs"
- When actual data arrives and replaces the projection, remove the label

### 4. Add scikit-learn to dependencies

```
# requirements.txt
scikit-learn>=1.4.0
```

Also add to Railway Dockerfile if not already installed.

### 5. Cache the projection

The projection is moderately expensive (5 DB queries + ML training). Cache with `@st.cache_data(ttl=300)` (5 min) since it only needs to update when new data syncs.

### 6. Auto-replace projected with actual

When the ETL sync runs and Amazon fulfillment data arrives, the projection should automatically stop showing. The check is simple: `project_amazon_nc()` returns `actual_nc_customers` when real data exists — use actual when non-None.

## Running the Backtest

```bash
# Load env vars and run
source .env && python scripts/backtest_nc_projector.py --days 60

# Longer backtest
source .env && python scripts/backtest_nc_projector.py --days 90
```

## Known Limitations

1. **Friday/Saturday accuracy is poor** — high variance days with occasional extreme lows (likely stockouts or data issues). Consider showing wider confidence bands on Fri/Sat.
2. **Slight overprediction bias (+2.1 NCs)** — we tend to say things are slightly better than they are. Could subtract 2 from the projection as a simple debias.
3. **Anomalous days are unpredictable** — if Amazon has a stockout, Prime event, or data glitch, the projection will be wrong. No model can predict regime changes.
4. **scikit-learn dependency** — adds ~30MB. If this is a problem, the ridge_ml method can be replaced with numpy-only OLS (minor accuracy loss).
5. **Revenue accuracy (16.2% trimmed MAPE)** is slightly worse than NC accuracy (13.2%) because AOV varies more than NC count. The DOW-specific AOV helps but doesn't fully solve it.
