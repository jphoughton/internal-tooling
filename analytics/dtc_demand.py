"""
Master DTC Demand Forecast.

Builds four SKU × Month forecast tables:
1. New Customer Sales — uses new customer SKU mix × forecasted new customer volume
2. Repeat Customer Sales — uses retention curve decay per cohort × repeat SKU mix
3. Amazon Sales — velocity-based projection per SKU
4. Master DTC Rollup — sums all three into one production planning table

Each table includes % of segment sales and monthly columns for the forecast horizon.
SKU-level seasonal indices shift the mix by month so summer-peaking flavors get
more allocation in warm months, etc. Monthly totals from the waterfall stay intact.
"""
import calendar
import logging
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import get_db, get_sku_seasonal_indices, get_seasonal_indices, get_setting
from analytics.sku_flavors import get_flavor
from utils.date_helpers import month_str as _month_str, add_months as _add_months, month_diff as _month_diff

logger = logging.getLogger(__name__)


def _load_sku_seasonal_reweights():
    """Load per-SKU seasonal reweight factors from the database.

    Reweight = sku_index / global_index for each (sku, calendar_month).
    This captures how each SKU deviates from the average seasonal shape
    without double-counting the global seasonal adjustment already applied
    in the waterfall.

    Returns:
        dict: {sku: {cal_month(1-12): reweight_factor}} or empty dict
              if seasonality is disabled or no data exists.
    """
    with get_db() as conn:
        enabled = get_setting(conn, 'seasonality_enabled', 'true')
        if enabled != 'true':
            return {}
        sku_indices = get_sku_seasonal_indices(conn)
        global_indices = get_seasonal_indices(conn)

    if not sku_indices or not global_indices:
        return {}

    reweights = {}
    for sku, months in sku_indices.items():
        reweights[sku] = {}
        for cal_month in range(1, 13):
            sku_val = months.get(cal_month, 1.0)
            global_val = global_indices.get(cal_month, 1.0)
            if global_val > 0:
                reweights[sku][cal_month] = sku_val / global_val
            else:
                reweights[sku][cal_month] = 1.0
    return reweights


def _seasonal_mix_for_month(base_mix, reweights, cal_month):
    """Adjust a SKU mix dict for a calendar month using seasonal reweights.

    The base_mix {sku: pct} is reweighted so SKUs that peak in this calendar
    month get a larger share, while the total still sums to 1.0.

    Args:
        base_mix: {sku: fraction} summing to ~1.0
        reweights: {sku: {cal_month: factor}} from _load_sku_seasonal_reweights()
        cal_month: 1-12

    Returns:
        {sku: adjusted_fraction} summing to 1.0
    """
    if not reweights:
        return base_mix

    weighted = {}
    for sku, pct in base_mix.items():
        factor = reweights.get(sku, {}).get(cal_month, 1.0)
        weighted[sku] = pct * factor

    total = sum(weighted.values())
    if total <= 0:
        return base_mix

    return {sku: w / total for sku, w in weighted.items()}


def get_current_month_progress():
    """
    Return info about how far into the current month we are.

    Returns:
        dict: {
            "current_month": "2026-02",
            "day_of_month": 20,
            "days_in_month": 28,
            "days_elapsed": 20,
            "days_remaining": 8,
            "pct_elapsed": 0.714,
            "pct_remaining": 0.286,
        }
    """
    now = datetime.utcnow()
    dim = calendar.monthrange(now.year, now.month)[1]
    elapsed = now.day
    remaining = dim - elapsed
    return {
        "current_month": now.strftime("%Y-%m"),
        "day_of_month": now.day,
        "days_in_month": dim,
        "days_elapsed": elapsed,
        "days_remaining": remaining,
        "pct_elapsed": round(elapsed / dim, 3),
        "pct_remaining": round(remaining / dim, 3),
    }


def compute_remaining_month_demand(rollup_table, month_progress=None):
    """
    For the current month, compute remaining expected demand per SKU.

    Uses the full-month forecast × pct_remaining to estimate what's left.

    Returns:
        dict: {sku: {"full_month": int, "already_consumed": int, "remaining": int}} or empty dict
    """
    if rollup_table is None or rollup_table.empty:
        return {}

    if month_progress is None:
        month_progress = get_current_month_progress()

    current_month = month_progress["current_month"]
    pct_elapsed = month_progress["pct_elapsed"]
    pct_remaining = month_progress["pct_remaining"]

    # Find the current month column
    month_cols = [c for c in rollup_table.columns if c.startswith("20")]
    if current_month not in month_cols:
        return {}

    result = {}
    for _, row in rollup_table.iterrows():
        sku = row["SKU"]
        full = int(row.get(current_month, 0) or 0)
        consumed = round(full * pct_elapsed)
        remaining = full - consumed
        result[sku] = {
            "full_month": full,
            "already_consumed": consumed,
            "remaining": remaining,
        }
    return result


# ----------------------------------------------------------------
# Amazon velocity forecast
# ----------------------------------------------------------------
def get_amazon_sku_velocity(lookback_days=30):
    """Compute daily sales velocity and revenue per SKU from Amazon sales data."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT sku,
                   SUM(units_sold) as total_units,
                   SUM(revenue) as total_revenue,
                   COUNT(DISTINCT sale_date) as days_of_data,
                   ROUND(SUM(units_sold) * 1.0 / GREATEST(COUNT(DISTINCT sale_date), 1), 2) as avg_daily
            FROM daily_sku_sales
            WHERE source = 'amazon'
            GROUP BY sku
            ORDER BY total_units DESC
        """).fetchall()

        trend_rows = conn.execute("""
            SELECT sku,
                   SUM(CASE WHEN sale_date >= date('now', '-7 days') THEN units_sold ELSE 0 END) * 1.0
                       / GREATEST(COUNT(DISTINCT CASE WHEN sale_date >= date('now', '-7 days') THEN sale_date END), 1) as avg_7d,
                   SUM(units_sold) * 1.0
                       / GREATEST(COUNT(DISTINCT sale_date), 1) as avg_30d,
                   SUM(CASE WHEN sale_date >= date('now', '-7 days') THEN revenue ELSE 0 END) * 1.0
                       / GREATEST(COUNT(DISTINCT CASE WHEN sale_date >= date('now', '-7 days') THEN sale_date END), 1) as avg_rev_7d,
                   SUM(revenue) * 1.0
                       / GREATEST(COUNT(DISTINCT sale_date), 1) as avg_rev_30d
            FROM daily_sku_sales
            WHERE source = 'amazon'
            GROUP BY sku
        """).fetchall()
        trend_map = {r["sku"]: {
            "avg_7d": float(r["avg_7d"] or 0),
            "avg_30d": float(r["avg_30d"] or 0),
            "avg_rev_7d": float(r["avg_rev_7d"] or 0),
            "avg_rev_30d": float(r["avg_rev_30d"] or 0),
        } for r in trend_rows}

    result = {}
    for r in rows:
        sku = r["sku"]
        trends = trend_map.get(sku, {"avg_7d": 0, "avg_30d": 0, "avg_rev_7d": 0, "avg_rev_30d": 0})
        total_units = float(r["total_units"] or 0)
        total_rev = float(r["total_revenue"] or 0)
        rev_per_unit = total_rev / total_units if total_units > 0 else 0
        result[sku] = {
            "avg_daily": float(r["avg_daily"] or 0),
            "total_units": total_units,
            "total_revenue": total_rev,
            "rev_per_unit": rev_per_unit,
            "days_of_data": int(r["days_of_data"] or 0),
            "avg_7d": trends["avg_7d"],
            "avg_30d": trends["avg_30d"],
            "avg_rev_7d": trends["avg_rev_7d"],
            "avg_rev_30d": trends["avg_rev_30d"],
        }
    return result


def build_amazon_sku_month_table(horizon_months=12, growth_rate=0.0, forecast_skus=None,
                                  revenue_forecast=None):
    """
    Amazon SKU × Month forecast table with % of Amazon sales.

    If revenue_forecast is provided (dict {month: revenue}), units are derived
    from revenue ÷ rev_per_unit instead of velocity. The SKU mix still comes
    from velocity (what % of Amazon units each SKU represents), seasonally
    adjusted so the mix shifts by calendar month.

    In velocity mode, per-SKU seasonal indices are applied directly to each
    SKU's projected units.

    Returns:
        DataFrame: [SKU, Flavor, % of Sales, month_1, month_2, ..., Total]
    """
    velocity = get_amazon_sku_velocity()
    if not velocity:
        return pd.DataFrame()

    now = datetime.utcnow()
    current_month = _month_str(now)
    months = [_add_months(current_month, i) for i in range(horizon_months)]

    total_daily = sum(v["avg_daily"] for v in velocity.values())

    # If revenue forecast is provided, compute total monthly units from revenue
    # Use overall Amazon rev/unit to convert revenue → total units per month
    rev_driven = False
    monthly_total_units = {}
    if revenue_forecast:
        has_any = any(revenue_forecast.get(m, 0) > 0 for m in months)
        if has_any:
            rev_driven = True
            amz_rev_data = get_amazon_monthly_revenue()
            rev_per_unit = amz_rev_data["rev_per_unit"]
            if rev_per_unit <= 0:
                # Fallback: compute from velocity data
                total_rev = sum(v.get("total_revenue", 0) for v in velocity.values())
                total_units = sum(v.get("total_units", 0) for v in velocity.values())
                rev_per_unit = total_rev / total_units if total_units > 0 else 35.0
            for m in months:
                rev = revenue_forecast.get(m, 0)
                monthly_total_units[m] = round(rev / rev_per_unit) if rev > 0 else 0

    # Compute each SKU's share of velocity (used for mix regardless of mode)
    sku_mix = {}
    for sku, v in velocity.items():
        if forecast_skus and sku not in forecast_skus:
            continue
        sku_mix[sku] = v["avg_daily"] / total_daily if total_daily > 0 else 0

    # Load per-SKU seasonal indices for Amazon (applied directly, not as reweight)
    sku_seasonal = {}
    with get_db() as conn:
        enabled = get_setting(conn, 'seasonality_enabled', 'true')
        if enabled == 'true':
            raw = get_sku_seasonal_indices(conn)
            sku_seasonal = raw if raw else {}

    # For revenue-driven mode, use seasonal mix reweighting (preserves total)
    reweights = _load_sku_seasonal_reweights() if rev_driven else {}

    rows = []
    for sku, pct in sku_mix.items():
        v = velocity[sku]

        row = {"SKU": sku, "Flavor": get_flavor(sku), "% of Sales": round(pct * 100, 1)}
        total = 0

        if rev_driven:
            # Units from revenue forecast, distributed by seasonally-adjusted SKU mix
            for m in months:
                cal_month = datetime.strptime(m, '%Y-%m').month
                adjusted_mix = _seasonal_mix_for_month(sku_mix, reweights, cal_month)
                monthly_units = round(monthly_total_units.get(m, 0) * adjusted_mix.get(sku, 0))
                row[m] = monthly_units
                total += monthly_units
        else:
            # Velocity-based projection with per-SKU seasonal adjustment
            if v["avg_7d"] > 0 and v["avg_30d"] > 0:
                blended = v["avg_7d"] * 0.6 + v["avg_30d"] * 0.4
            else:
                blended = v["avg_daily"]

            for i, m in enumerate(months):
                cal_month = datetime.strptime(m, '%Y-%m').month
                growth_factor = (1 + growth_rate) ** i
                seasonal_factor = sku_seasonal.get(sku, {}).get(cal_month, 1.0)
                monthly_units = round(blended * 30.44 * growth_factor * seasonal_factor)
                row[m] = monthly_units
                total += monthly_units

        row["Total"] = total
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Total", ascending=False).reset_index(drop=True)
    return df


def get_amazon_monthly_revenue():
    """
    Get historical monthly revenue for Amazon to compute average rev/unit.

    Returns:
        dict: {"rev_per_unit": float, "monthly_rev": {month: revenue}}
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT strftime('%Y-%m', sale_date) as month,
                   SUM(revenue) as revenue,
                   SUM(units_sold) as units
            FROM daily_sku_sales
            WHERE source = 'amazon'
            GROUP BY strftime('%Y-%m', sale_date)
            ORDER BY month
        """).fetchall()

    if not rows:
        return {"rev_per_unit": 0, "monthly_rev": {}}

    monthly_rev = {r["month"]: r["revenue"] or 0 for r in rows}
    total_rev = sum(r["revenue"] or 0 for r in rows)
    total_units = sum(r["units"] or 0 for r in rows)
    rev_per_unit = total_rev / total_units if total_units > 0 else 0

    return {"rev_per_unit": rev_per_unit, "monthly_rev": monthly_rev}


# ----------------------------------------------------------------
# New customer SKU × Month forecast
# ----------------------------------------------------------------
def _get_new_customer_sku_mix(forecast_skus=None, lookback_months=3):
    """
    Get the SKU purchase mix and units-per-customer for new customers.

    Mix uses recent data (lookback_months) for current product behavior.
    UPC uses 12-month window to match the waterfall's metric timeframe.

    Returns:
        dict {sku: pct} — unit mix summing to 1.0 across forecast SKUs
        float — forecast-SKU-only units per new customer (12mo basis)
    """
    with get_db() as conn:
        # Mix from recent data
        rows = conn.execute(f"""
            SELECT oi.sku, SUM(oi.quantity) as qty
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE strftime('%Y-%m', o.order_date) = strftime('%Y-%m', c.first_order_date)
              AND o.order_date >= date('now', '-{lookback_months} months')
            GROUP BY oi.sku
            ORDER BY qty DESC
        """).fetchall()

        # UPC from 12-month window (matches waterfall metrics timeframe)
        upc_data = conn.execute("""
            SELECT SUM(oi.quantity) as total_units,
                   COUNT(DISTINCT o.customer_id) as customers
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE strftime('%Y-%m', o.order_date) = strftime('%Y-%m', c.first_order_date)
              AND o.order_date >= date('now', '-12 months')
              AND oi.sku IN ({})
        """.format(','.join('?' * len(forecast_skus))), list(forecast_skus)).fetchone() if forecast_skus else None

    if not rows:
        return {}, 0.0

    # Filter to forecast SKUs, then compute mix within those only
    if forecast_skus:
        rows = [r for r in rows if r["sku"] in forecast_skus]

    total = float(sum(r["qty"] for r in rows))
    if total == 0:
        return {}, 0.0

    mix = {r["sku"]: float(r["qty"]) / total for r in rows}

    # UPC: forecast-SKU units per new customer (12mo)
    if upc_data and upc_data["customers"] and upc_data["customers"] > 0:
        upc = float(upc_data["total_units"] or 0) / float(upc_data["customers"])
    else:
        upc = total / max(1, len(mix))  # fallback

    return mix, upc


def build_new_customer_sku_month_table(waterfall_df, horizon_months=12, forecast_skus=None):
    """
    New Customer SKU × Month forecast.

    Uses new_customers_acquired from waterfall × forecast-SKU-only UPC
    to get the correct number of forecast-SKU units (excluding merch,
    samples, virtual items, etc.). Then distributes across SKUs by mix,
    seasonally adjusted so the mix shifts by calendar month.

    Returns:
        DataFrame: [SKU, Flavor, % of New Sales, month_1, ..., Total]
    """
    if waterfall_df is None or waterfall_df.empty:
        return pd.DataFrame()

    sku_mix, forecast_upc = _get_new_customer_sku_mix(forecast_skus=forecast_skus)
    if not sku_mix or forecast_upc <= 0:
        return pd.DataFrame()

    reweights = _load_sku_seasonal_reweights()

    now = datetime.utcnow()
    current_month = _month_str(now)
    months = [_add_months(current_month, i) for i in range(horizon_months)]

    # Use new_customers_acquired × forecast-SKU UPC for correct unit count
    monthly_new_units = {}
    for _, row in waterfall_df.iterrows():
        monthly_new_units[row["month"]] = row["new_customers_acquired"] * forecast_upc

    rows = {sku: {"SKU": sku, "Flavor": get_flavor(sku), "% of New Sales": round(pct * 100, 1)}
            for sku, pct in sku_mix.items()}
    totals = {sku: 0 for sku in sku_mix}

    for m in months:
        cal_month = datetime.strptime(m, '%Y-%m').month
        adjusted_mix = _seasonal_mix_for_month(sku_mix, reweights, cal_month)
        month_total = monthly_new_units.get(m, 0)
        for sku in sku_mix:
            units = round(month_total * adjusted_mix.get(sku, 0))
            rows[sku][m] = units
            totals[sku] += units

    result = []
    for sku in sku_mix:
        rows[sku]["Total"] = totals[sku]
        result.append(rows[sku])

    df = pd.DataFrame(result)
    if not df.empty:
        df = df.sort_values("Total", ascending=False).reset_index(drop=True)
    return df


# ----------------------------------------------------------------
# Repeat customer SKU × Month forecast (with retention decay)
# ----------------------------------------------------------------
def _get_repeat_customer_sku_mix(forecast_skus=None, lookback_months=3):
    """
    Get the SKU purchase mix and units-per-returning-customer for repeat customers.

    Mix uses recent data (lookback_months) for current product behavior.
    UPC uses 12-month window to match the waterfall's metric timeframe.

    Returns:
        dict {sku: pct} — unit mix summing to 1.0 across forecast SKUs
        float — forecast-SKU-only units per repeat customer per return month (12mo)
    """
    with get_db() as conn:
        # Mix from recent data
        rows = conn.execute(f"""
            SELECT oi.sku, SUM(oi.quantity) as qty
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE strftime('%Y-%m', o.order_date) != strftime('%Y-%m', c.first_order_date)
              AND o.order_date >= date('now', '-{lookback_months} months')
            GROUP BY oi.sku
            ORDER BY qty DESC
        """).fetchall()

        # UPC from 12-month window, forecast SKUs only (matches waterfall)
        upc_row = conn.execute("""
            SELECT AVG(monthly_upc) as upc FROM (
                SELECT strftime('%Y-%m', o.order_date) as month,
                       CAST(SUM(oi.quantity) AS REAL) / COUNT(DISTINCT o.customer_id) as monthly_upc
                FROM orders o
                JOIN order_items oi ON o.order_id = oi.order_id
                JOIN customers c ON o.customer_id = c.customer_id
                WHERE strftime('%Y-%m', o.order_date) != strftime('%Y-%m', c.first_order_date)
                  AND o.order_date >= date('now', '-12 months')
                  AND oi.sku IN ({})
                GROUP BY strftime('%Y-%m', o.order_date)
            )
        """.format(','.join('?' * len(forecast_skus))), list(forecast_skus)).fetchone() if forecast_skus else None

    if not rows:
        return {}, 0.0

    if forecast_skus:
        rows = [r for r in rows if r["sku"] in forecast_skus]

    total = float(sum(r["qty"] for r in rows))
    if total == 0:
        return {}, 0.0

    mix = {r["sku"]: float(r["qty"]) / total for r in rows}
    upc = float(upc_row["upc"] or 0) if upc_row and upc_row["upc"] else 0.0

    return mix, upc


def build_repeat_customer_sku_month_table(waterfall_df, horizon_months=12, forecast_skus=None):
    """
    Repeat Customer SKU × Month forecast using retention curve decay.

    The waterfall computes repeat_units using the ALL-item units-per-customer.
    We scale those down to forecast-SKU-only units using the ratio of
    forecast-SKU UPC to all-item UPC, then distribute across SKUs by
    seasonally-adjusted mix.

    Returns:
        DataFrame: [SKU, Flavor, % of Repeat Sales, month_1, ..., Total]
    """
    if waterfall_df is None or waterfall_df.empty:
        return pd.DataFrame()

    sku_mix, forecast_rep_upc = _get_repeat_customer_sku_mix(forecast_skus=forecast_skus)
    if not sku_mix:
        return pd.DataFrame()

    reweights = _load_sku_seasonal_reweights()

    now = datetime.utcnow()
    current_month = _month_str(now)
    months = [_add_months(current_month, i) for i in range(horizon_months)]

    # The waterfall's repeat_units uses ALL-item rep_upc. Scale to forecast-SKU-only.
    # ratio = forecast_rep_upc / all_item_rep_upc
    from analytics.waterfall import get_aov_and_units
    all_metrics = get_aov_and_units(None)
    all_rep_upc = all_metrics["units_per_repeat_customer"]
    scale = forecast_rep_upc / all_rep_upc if all_rep_upc > 0 and forecast_rep_upc > 0 else 1.0

    monthly_repeat = {}
    for _, row in waterfall_df.iterrows():
        monthly_repeat[row["month"]] = row["repeat_units"] * scale

    rows = {sku: {"SKU": sku, "Flavor": get_flavor(sku), "% of Repeat Sales": round(pct * 100, 1)}
            for sku, pct in sku_mix.items()}
    totals = {sku: 0 for sku in sku_mix}

    for m in months:
        cal_month = datetime.strptime(m, '%Y-%m').month
        adjusted_mix = _seasonal_mix_for_month(sku_mix, reweights, cal_month)
        month_total = monthly_repeat.get(m, 0)
        for sku in sku_mix:
            units = round(month_total * adjusted_mix.get(sku, 0))
            rows[sku][m] = units
            totals[sku] += units

    result = []
    for sku in sku_mix:
        rows[sku]["Total"] = totals[sku]
        result.append(rows[sku])

    df = pd.DataFrame(result)
    if not df.empty:
        df = df.sort_values("Total", ascending=False).reset_index(drop=True)
    return df


# ----------------------------------------------------------------
# Master DTC Rollup (New + Repeat + Amazon)
# ----------------------------------------------------------------
def build_master_rollup_table(new_df, repeat_df, amazon_df, horizon_months=12):
    """
    Combine new customer + repeat customer + Amazon into one master table.

    Returns:
        DataFrame: [SKU, Flavor, % of Sales, month_1, ..., Total]
    """
    now = datetime.utcnow()
    current_month = _month_str(now)
    months = [_add_months(current_month, i) for i in range(horizon_months)]

    # Collect all SKUs across all three tables
    all_skus = set()
    for df in [new_df, repeat_df, amazon_df]:
        if df is not None and not df.empty and "SKU" in df.columns:
            all_skus.update(df["SKU"].tolist())

    if not all_skus:
        return pd.DataFrame()

    # Index each table by SKU for fast lookup
    def _index_by_sku(df):
        if df is None or df.empty or "SKU" not in df.columns:
            return {}
        return {row["SKU"]: row for _, row in df.iterrows()}

    new_idx = _index_by_sku(new_df)
    rep_idx = _index_by_sku(repeat_df)
    amz_idx = _index_by_sku(amazon_df)

    rows = []
    for sku in all_skus:
        row = {"SKU": sku, "Flavor": get_flavor(sku)}
        total = 0
        for m in months:
            new_units = new_idx.get(sku, {}).get(m, 0) if sku in new_idx else 0
            rep_units = rep_idx.get(sku, {}).get(m, 0) if sku in rep_idx else 0
            amz_units = amz_idx.get(sku, {}).get(m, 0) if sku in amz_idx else 0

            # Handle pandas Series/values
            if hasattr(new_units, 'item'):
                new_units = new_units.item()
            if hasattr(rep_units, 'item'):
                rep_units = rep_units.item()
            if hasattr(amz_units, 'item'):
                amz_units = amz_units.item()

            combined = int(new_units or 0) + int(rep_units or 0) + int(amz_units or 0)
            row[m] = combined
            total += combined
        row["Total"] = total
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        grand_total = df["Total"].sum()
        df["% of Sales"] = (df["Total"] / grand_total * 100).round(1) if grand_total > 0 else 0
        df = df.sort_values("Total", ascending=False).reset_index(drop=True)

        # Reorder columns: SKU, Flavor, % of Sales, months..., Total
        month_cols = [c for c in df.columns if c.startswith("20")]
        ordered_cols = ["SKU", "Flavor", "% of Sales"] + month_cols + ["Total"]
        df = df[ordered_cols]

    return df


def build_master_dtc_forecast(
    shopify_waterfall_df,
    shopify_sku_forecast_df,
    amazon_growth_rate=0.0,
    horizon_months=12,
    forecast_skus=None,
    media_plan=None,
    amazon_revenue_forecast=None,
):
    """
    Build the complete DTC demand forecast with all four tables + revenue.

    Returns:
        dict with keys:
            "summary": DataFrame — monthly totals by channel (units + revenue)
            "new_customer_table": DataFrame — new customer SKU × Month
            "repeat_customer_table": DataFrame — repeat customer SKU × Month
            "amazon_table": DataFrame — Amazon SKU × Month
            "rollup_table": DataFrame — master DTC SKU × Month
            "channel_split": dict — totals and percentages
            "revenue": dict — monthly revenue by segment
    """
    now = datetime.utcnow()
    current_month = _month_str(now)
    months = [_add_months(current_month, i) for i in range(horizon_months)]

    # Build the three segment tables
    new_table = build_new_customer_sku_month_table(
        shopify_waterfall_df, horizon_months=horizon_months, forecast_skus=forecast_skus
    )
    repeat_table = build_repeat_customer_sku_month_table(
        shopify_waterfall_df, horizon_months=horizon_months, forecast_skus=forecast_skus
    )
    amazon_table = build_amazon_sku_month_table(
        horizon_months=horizon_months, growth_rate=amazon_growth_rate, forecast_skus=forecast_skus,
        revenue_forecast=amazon_revenue_forecast,
    )

    # Master rollup
    rollup_table = build_master_rollup_table(new_table, repeat_table, amazon_table, horizon_months)

    # --- Revenue computation ---
    from analytics.waterfall import get_aov_and_units
    metrics = get_aov_and_units(None)
    new_customer_rev_per_unit = metrics.get("new_customer_rev_per_unit", 0)
    repeat_rev_per_unit = metrics.get("repeat_rev_per_unit", 0)

    # Amazon rev/unit from historical data
    amz_rev_data = get_amazon_monthly_revenue()
    amz_rev_per_unit = amz_rev_data["rev_per_unit"]

    # New customer revenue: media spend × ROAS per month
    # This is the TOTAL new customer revenue, not just forecast-SKU revenue
    new_rev_by_month = {}
    if media_plan:
        plan_map = {e["month"]: e for e in media_plan}
        for m in months:
            entry = plan_map.get(m)
            if entry and entry.get("spend", 0) > 0:
                new_rev_by_month[m] = round(entry["spend"] * (entry.get("new_customer_roas") or entry.get("roas", 0.7)), 2)
            else:
                new_rev_by_month[m] = 0
    else:
        # Fallback: use new customer rev_per_unit × units
        for m in months:
            new_units = new_table[m].sum() if not new_table.empty and m in new_table.columns else 0
            new_rev_by_month[m] = round(new_units * new_customer_rev_per_unit, 2)

    # Repeat customer revenue: units × rev_per_unit
    repeat_rev_by_month = {}
    for m in months:
        rep_units = repeat_table[m].sum() if not repeat_table.empty and m in repeat_table.columns else 0
        repeat_rev_by_month[m] = round(rep_units * repeat_rev_per_unit, 2)

    # Amazon revenue: use input forecast if provided, otherwise units × rev_per_unit
    amz_rev_by_month = {}
    for m in months:
        if amazon_revenue_forecast and amazon_revenue_forecast.get(m, 0) > 0:
            amz_rev_by_month[m] = round(amazon_revenue_forecast[m], 2)
        else:
            amz_units = amazon_table[m].sum() if not amazon_table.empty and m in amazon_table.columns else 0
            amz_rev_by_month[m] = round(amz_units * amz_rev_per_unit, 2)

    # Summary table (monthly totals by channel)
    summary_rows = []
    for m in months:
        new_total = new_table[m].sum() if not new_table.empty and m in new_table.columns else 0
        rep_total = repeat_table[m].sum() if not repeat_table.empty and m in repeat_table.columns else 0
        amz_total = amazon_table[m].sum() if not amazon_table.empty and m in amazon_table.columns else 0
        shopify_total = new_total + rep_total
        dtc_total = shopify_total + amz_total

        new_rev = new_rev_by_month.get(m, 0)
        rep_rev = repeat_rev_by_month.get(m, 0)
        amz_rev = amz_rev_by_month.get(m, 0)

        summary_rows.append({
            "month": m,
            "shopify_repeat_units": round(rep_total),
            "shopify_new_units": round(new_total),
            "shopify_total_units": round(shopify_total),
            "amazon_units": round(amz_total),
            "total_units": round(dtc_total),
            "shopify_new_rev": round(new_rev),
            "shopify_repeat_rev": round(rep_rev),
            "shopify_total_rev": round(new_rev + rep_rev),
            "amazon_rev": round(amz_rev),
            "total_rev": round(new_rev + rep_rev + amz_rev),
        })

    summary_df = pd.DataFrame(summary_rows)

    # Channel totals
    shopify_total = summary_df["shopify_total_units"].sum() if not summary_df.empty else 0
    amazon_total = summary_df["amazon_units"].sum() if not summary_df.empty else 0
    dtc_total = shopify_total + amazon_total

    shopify_rev_total = summary_df["shopify_total_rev"].sum() if not summary_df.empty else 0
    amazon_rev_total = summary_df["amazon_rev"].sum() if not summary_df.empty else 0
    dtc_rev_total = shopify_rev_total + amazon_rev_total

    channel_split = {
        "shopify_total": round(shopify_total),
        "amazon_total": round(amazon_total),
        "dtc_total": round(dtc_total),
        "shopify_pct": round(shopify_total / dtc_total * 100, 1) if dtc_total > 0 else 0,
        "amazon_pct": round(amazon_total / dtc_total * 100, 1) if dtc_total > 0 else 0,
        "shopify_rev": round(shopify_rev_total),
        "amazon_rev": round(amazon_rev_total),
        "dtc_rev": round(dtc_rev_total),
    }

    # Revenue by segment and month (for display above each table)
    revenue = {
        "new_customer": new_rev_by_month,
        "repeat_customer": repeat_rev_by_month,
        "amazon": amz_rev_by_month,
        "rollup": {m: round(new_rev_by_month.get(m, 0) + repeat_rev_by_month.get(m, 0) + amz_rev_by_month.get(m, 0)) for m in months},
        "metrics": {
            "new_customer_rev_per_unit": new_customer_rev_per_unit,
            "repeat_rev_per_unit": repeat_rev_per_unit,
            "amazon_rev_per_unit": amz_rev_per_unit,
        },
    }

    return {
        "summary": summary_df,
        "new_customer_table": new_table,
        "repeat_customer_table": repeat_table,
        "amazon_table": amazon_table,
        "rollup_table": rollup_table,
        "channel_split": channel_split,
        "revenue": revenue,
    }
