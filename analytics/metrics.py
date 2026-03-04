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
                        cogs_pct=0.25, fulfillment_pct=0.18,
                        retention_curve=None, max_months=24):
    """CAC Payback in months using contribution-margin model.

    CFO formula: each month a customer generates revenue with costs deducted
    (COGS + fulfillment). Payback = month when cumulative contribution covers CAC.

    Args:
        spend: Total media spend in period.
        new_customers: New customers acquired in period.
        nc_revenue: New-customer revenue in period (first-order total).
        cogs_pct: Cost of goods as fraction of revenue (e.g. 0.25).
        fulfillment_pct: Fulfillment cost as fraction of revenue (e.g. 0.18).
        retention_curve: Dict {month_offset: revenue_fraction} from retention
            model. If None, falls back to simple CAC / monthly_contribution.
        max_months: Cap on payback search (default 24).

    Returns:
        Float months to payback, or 0 if inputs invalid / payback > max_months.
    """
    if new_customers <= 0 or nc_revenue <= 0 or spend <= 0:
        return 0

    cac = spend / new_customers
    aov = nc_revenue / new_customers  # first-order AOV per NC
    margin_rate = 1 - cogs_pct - fulfillment_pct

    if margin_rate <= 0:
        return 0

    # Month 0: first purchase contribution
    cumulative = aov * margin_rate

    if cumulative >= cac:
        # Pays back within first purchase — return fraction of month
        return cac / cumulative if cumulative > 0 else 0

    if not retention_curve:
        # Fallback: simple monthly contribution = annualised NC rev / 12
        monthly_contrib = (nc_revenue / new_customers) * margin_rate
        return cac / monthly_contrib if monthly_contrib > 0 else 0

    # Walk the retention curve month-by-month
    for month in range(1, max_months + 1):
        repeat_frac = retention_curve.get(month, 0)
        if repeat_frac <= 0:
            continue
        month_rev = aov * repeat_frac
        month_contrib = month_rev * margin_rate
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
