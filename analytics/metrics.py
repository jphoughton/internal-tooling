"""Centralized metric formulas and shared DB query helpers.

All pacing, efficiency, and customer-segmentation queries live here
so that views/pacing.py, views/marketing.py, and analytics modules
use a single source of truth.
"""
import logging

from db import get_db, read_sql

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Formula helpers (pure math, no DB)
# ──────────────────────────────────────────────────────────────────

def compute_mer(revenue, spend):
    """Marketing Efficiency Ratio = Total Revenue / Total Spend."""
    return revenue / spend if spend > 0 else 0


def compute_nc_roas(nc_revenue, spend):
    """New Customer Return on Ad Spend = NC Revenue / Spend."""
    return nc_revenue / spend if spend > 0 else 0


def compute_nc_cpa(spend, nc_count):
    """New Customer Cost Per Acquisition = Spend / NC Count."""
    return spend / nc_count if nc_count > 0 else 0


def compute_aov(revenue, order_count):
    """Average Order Value = Revenue / Orders."""
    return revenue / order_count if order_count > 0 else 0


def compute_cac_payback(spend, new_customers, nc_revenue,
                        cogs_pct=0.17, fulfillment_pct=0.18,
                        net_to_gross=1.0,
                        retention_curve=None, repeat_multiplier=1.0,
                        max_months=24):
    """CAC Payback in months using contribution-margin model.

    CFO formula: each month a customer generates revenue with costs deducted
    (COGS + fulfillment). Payback = month when cumulative contribution covers CAC.

    COGS is applied as a % of gross sales (net revenue × net_to_gross).
    Fulfillment is applied as a % of net revenue.

    Args:
        spend: Total media spend in period.
        new_customers: New customers acquired in period.
        nc_revenue: New-customer revenue in period (first-order total, net).
        cogs_pct: Cost of goods as fraction of gross sales (default 0.17 = 17%).
        fulfillment_pct: Fulfillment cost as fraction of net revenue (e.g. 0.18).
        net_to_gross: Multiplier to convert net revenue to gross sales (e.g. 1.386).
        retention_curve: Dict {month_offset: revenue_fraction} from retention
            model. If None, falls back to simple CAC / monthly_contribution.
        repeat_multiplier: Scale factor applied to the retention curve to
            reflect actual vs modeled repeat performance. Computed as
            actual_repeat_rev / (goal_repeat_rev * pct_month_elapsed).
            >1 means repeat is outperforming model → faster payback.
        max_months: Cap on payback search (default 24).

    Returns:
        Float months to payback, or 0 if inputs invalid / payback > max_months.
    """
    if new_customers <= 0 or nc_revenue <= 0 or spend <= 0:
        return 0

    cac = spend / new_customers
    aov = nc_revenue / new_customers  # first-order AOV per NC (net)

    def _contribution(rev):
        """Contribution margin: revenue minus COGS (on gross) minus fulfillment (on net)."""
        cogs = rev * net_to_gross * cogs_pct
        fulfill = rev * fulfillment_pct
        return rev - cogs - fulfill

    test_contrib = _contribution(aov)
    if test_contrib <= 0:
        return 0

    # Month 0: first purchase contribution
    cumulative = test_contrib

    if cumulative >= cac:
        # Pays back within first purchase — return fraction of month
        return cac / cumulative if cumulative > 0 else 0

    if not retention_curve:
        # Fallback: simple monthly contribution = annualised NC rev / 12
        monthly_contrib = _contribution(aov)
        return cac / monthly_contrib if monthly_contrib > 0 else 0

    # Walk the retention curve month-by-month, scaled by repeat_multiplier
    _mult = max(repeat_multiplier, 0)
    for month in range(1, max_months + 1):
        repeat_frac = retention_curve.get(month, 0)
        if repeat_frac <= 0:
            continue
        month_rev = aov * repeat_frac * _mult
        month_contrib = _contribution(month_rev)
        cumulative += month_contrib
        if cumulative >= cac:
            # Interpolate within this month
            overshoot = cumulative - cac
            frac_of_month = 1 - (overshoot / month_contrib) if month_contrib > 0 else 0
            return month - 1 + frac_of_month
    return 0  # didn't pay back within max_months


def nc_revenue_fraction(oi_total_rev, oi_new_rev, channel_revenue):
    """Derive NC revenue using order-items fraction applied to channel revenue.

    Pattern: get the new/total fraction from order_items (ground truth for
    new vs repeat), then apply it to the authoritative revenue source
    (daily_sku_sales for Amazon, daily_sku_sales for Shopify).
    """
    frac = oi_new_rev / oi_total_rev if oi_total_rev > 0 else 0
    return channel_revenue * frac


# ──────────────────────────────────────────────────────────────────
# Shared DB query helpers
# ──────────────────────────────────────────────────────────────────

def get_channel_revenue(conn, source, start_date, end_date):
    """SUM(revenue) from daily_sku_sales for a source + date range."""
    row = conn.execute(
        "SELECT SUM(revenue) FROM daily_sku_sales "
        "WHERE source = ? AND sale_date >= ? AND sale_date <= ?",
        (source, start_date, end_date),
    ).fetchone()
    return float(row[0] or 0) if row else 0


def get_amazon_spend(conn, start_date, end_date):
    """SUM(spend) from amazon_daily_rollup for a date range."""
    row = conn.execute(
        "SELECT SUM(spend) AS total_spend "
        "FROM amazon_daily_rollup WHERE date >= ? AND date <= ?",
        (start_date, end_date),
    ).fetchone()
    if row and row['total_spend'] is not None:
        return float(row['total_spend'])
    return 0


def get_amazon_spend_projected(conn, start_date, end_date):
    """SUM(spend) from amazon_daily_rollup with L7D projection for gap days.

    If the Google Sheet lags behind (max date with data < yesterday),
    projects missing days using the average daily spend from the last 7
    days with actual data.

    Returns:
        (total_spend, projected_spend, gap_days) tuple where:
        - total_spend = actual + projected
        - projected_spend = the projected portion only
        - gap_days = number of days projected
    """
    from utils.date_helpers import business_yesterday

    actual = get_amazon_spend(conn, start_date, end_date)
    yesterday = business_yesterday()
    yesterday_str = str(yesterday)

    # If end_date is in the future or beyond yesterday, cap at yesterday
    if str(end_date) > yesterday_str:
        end_date = yesterday_str

    # Find the max date with spend data in the range
    row = conn.execute(
        "SELECT MAX(date) AS max_date FROM amazon_daily_rollup "
        "WHERE date >= ? AND date <= ? AND spend IS NOT NULL AND spend > 0",
        (start_date, end_date),
    ).fetchone()

    max_date_str = row['max_date'] if row and row['max_date'] else None
    if not max_date_str or str(max_date_str) >= yesterday_str:
        return (actual, 0, 0)

    # Calculate gap days
    from datetime import datetime
    max_date = datetime.strptime(str(max_date_str)[:10], '%Y-%m-%d').date()
    gap_days = (yesterday - max_date).days

    if gap_days <= 0:
        return (actual, 0, 0)

    # Get L7D average daily spend from last 7 days with data
    l7d_row = conn.execute(
        "SELECT AVG(spend) AS avg_spend FROM ("
        "  SELECT spend FROM amazon_daily_rollup "
        "  WHERE spend IS NOT NULL AND spend > 0 "
        "  ORDER BY date DESC LIMIT 7"
        ")",
    ).fetchone()

    l7d_avg = float(l7d_row['avg_spend']) if l7d_row and l7d_row['avg_spend'] else 0
    projected = gap_days * l7d_avg

    return (actual + projected, projected, gap_days)


def project_daily_spend_gaps(spend_df, date_col='sale_date', spend_col='_amz_spend'):
    """Fill in missing/zero spend days in a daily spend DataFrame using L7D average.

    Handles both interior gaps (zero-spend days between days with data) and
    trailing gaps (missing days after the last date with data).
    Mutates nothing — returns a new DataFrame with projected rows appended.
    Projected rows have '_projected' = True.

    Args:
        spend_df: DataFrame with date and spend columns.
        date_col: Name of the date column (string dates).
        spend_col: Name of the spend column.

    Returns:
        DataFrame with gap rows appended and interior gaps filled (if any).
    """
    import pandas as pd
    from utils.date_helpers import business_yesterday
    from datetime import timedelta

    if spend_df.empty or spend_col not in spend_df.columns:
        return spend_df

    df = spend_df.copy()
    df['_projected'] = False

    # Find rows with actual spend
    _has_spend = df[df[spend_col].fillna(0) > 0]
    if _has_spend.empty:
        return df

    # L7D average from last 7 days with data
    _recent = _has_spend.nlargest(7, date_col)
    l7d_avg = _recent[spend_col].mean()

    # 1. Backfill interior gaps — zero/NaN spend on past days
    yesterday = business_yesterday()
    zero_mask = (df[spend_col].fillna(0) == 0) & (df[date_col] <= str(yesterday))
    if zero_mask.any():
        df.loc[zero_mask, spend_col] = round(l7d_avg, 2)
        df.loc[zero_mask, '_projected'] = True

    # 2. Append trailing gap rows (missing dates after last row through yesterday)
    max_date = pd.to_datetime(_has_spend[date_col]).max().date()
    if max_date < yesterday:
        gap_rows = []
        current = max_date + timedelta(days=1)
        while current <= yesterday:
            gap_rows.append({date_col: str(current), spend_col: round(l7d_avg, 2),
                             '_projected': True})
            current += timedelta(days=1)
        if gap_rows:
            df = pd.concat([df, pd.DataFrame(gap_rows)], ignore_index=True)

    return df


def get_nc_stats(conn, source, start_date, end_date):
    """New customer count + order-item revenue split for a source + date range.

    Uses the cust_first CTE pattern (MIN(order_date) = first purchase).
    Returns dict: {new_customers, oi_total_rev, oi_new_rev}.
    """
    row = conn.execute(
        "WITH cust_first AS ("
        "  SELECT customer_id, MIN(order_date) AS first_order_date"
        "  FROM orders WHERE source = ?"
        "  GROUP BY customer_id"
        ") "
        "SELECT"
        "  COUNT(DISTINCT CASE WHEN cf.first_order_date >= ?"
        "       AND cf.first_order_date <= ?"
        "       THEN o.customer_id END) AS new_customers,"
        "  SUM(oi.total_price) AS oi_total_rev,"
        "  SUM(CASE WHEN cf.first_order_date >= ?"
        "       AND cf.first_order_date <= ?"
        "       THEN oi.total_price ELSE 0 END) AS oi_new_rev"
        " FROM orders o"
        " JOIN cust_first cf ON o.customer_id = cf.customer_id"
        " JOIN order_items oi ON o.order_id = oi.order_id"
        " WHERE o.order_date BETWEEN ? AND ?"
        " AND o.source = ?",
        (source, start_date, end_date,
         start_date, end_date,
         start_date, end_date, source),
    ).fetchone()
    if row:
        return {
            'new_customers': int(float(row['new_customers'] or 0)),
            'oi_total_rev': float(row['oi_total_rev'] or 0),
            'oi_new_rev': float(row['oi_new_rev'] or 0),
        }
    return {'new_customers': 0, 'oi_total_rev': 0, 'oi_new_rev': 0}


def get_total_customers(conn, source, start_date, end_date):
    """COUNT(DISTINCT customer_id) for a source + date range."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM orders "
        "WHERE source = ? AND order_date BETWEEN ? AND ?",
        (source, start_date, end_date),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def get_channel_period_metrics(conn, source, start_date, end_date):
    """All-in-one: revenue + spend + NC stats for one channel + date range.

    Returns dict with keys:
        revenue, spend, new_customers, nc_revenue, oi_total_rev, oi_new_rev
    """
    revenue = get_channel_revenue(conn, source, start_date, end_date)

    spend = 0
    if source == 'amazon':
        spend = get_amazon_spend(conn, start_date, end_date)

    nc = get_nc_stats(conn, source, start_date, end_date)
    nc_rev = nc_revenue_fraction(nc['oi_total_rev'], nc['oi_new_rev'], revenue)

    return {
        'revenue': revenue,
        'spend': spend,
        'new_customers': nc['new_customers'],
        'nc_revenue': nc_rev,
        'oi_total_rev': nc['oi_total_rev'],
        'oi_new_rev': nc['oi_new_rev'],
    }
