"""
Retention analytics:
1. Customer repurchase cohort analysis
2. SKU lifecycle (sales decay/growth) curves
"""
import pandas as pd
from db import get_db, read_sql


def get_customer_cohort_data(sku_filter=None, source_filter=None):
    """
    Build customer repurchase cohort matrix.
    Groups customers by their first-purchase month, then tracks what %
    made another purchase in months 1, 2, 3... 12 after.

    Returns:
        DataFrame with index=cohort month, columns=months since first purchase,
        values=retention rate (0-1).
    """
    with get_db() as conn:
        query = """
            SELECT
                o.customer_id,
                DATE(o.order_date) as order_date,
                c.first_order_date
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            JOIN order_items oi ON o.order_id = oi.order_id
            WHERE 1=1
        """
        params = []
        if sku_filter:
            query += " AND oi.sku = ?"
            params.append(sku_filter)
        if source_filter:
            query += " AND o.source = ?"
            params.append(source_filter)

        df = read_sql(query, conn, params=params)

    if df.empty:
        return pd.DataFrame()

    df["order_date"] = pd.to_datetime(df["order_date"])
    df["first_order_date"] = pd.to_datetime(df["first_order_date"])

    # Cohort = month of first purchase
    df["cohort"] = df["first_order_date"].dt.to_period("M")
    df["order_month"] = df["order_date"].dt.to_period("M")
    df["months_since_first"] = (df["order_month"] - df["cohort"]).apply(lambda x: x.n)

    # Get unique customers per cohort
    cohort_sizes = df.groupby("cohort")["customer_id"].nunique().rename("cohort_size")

    # Get unique customers per cohort × month offset
    retention = (
        df.groupby(["cohort", "months_since_first"])["customer_id"]
        .nunique()
        .reset_index()
        .rename(columns={"customer_id": "customers"})
    )

    retention = retention.merge(cohort_sizes, on="cohort")
    retention["retention_rate"] = retention["customers"] / retention["cohort_size"]

    # Pivot to matrix form (NaN for months that haven't elapsed yet)
    matrix = retention.pivot_table(
        index="cohort",
        columns="months_since_first",
        values="retention_rate",
    )

    # Convert period index to strings for display
    matrix.index = matrix.index.astype(str)

    return matrix


def get_cohort_sizes():
    """Get the size of each monthly cohort."""
    with get_db() as conn:
        df = read_sql("""
            SELECT
                strftime('%Y-%m', first_order_date) as cohort,
                COUNT(*) as cohort_size
            FROM customers
            GROUP BY strftime('%Y-%m', first_order_date)
            ORDER BY cohort
        """, conn)
    return df


def get_sku_lifecycle_data(sku=None, normalize=True):
    """
    Track sales volume over time since each SKU's first sale.

    Args:
        sku: Optional specific SKU to return. If None, returns all.
        normalize: If True, normalize to weeks-since-launch.

    Returns:
        DataFrame with columns: sku, period (week number or date), units_sold
    """
    with get_db() as conn:
        query = """
            SELECT
                ds.sku,
                ds.sale_date,
                sm.first_sale_date,
                SUM(ds.units_sold) as units_sold,
                SUM(ds.revenue) as revenue
            FROM daily_sku_sales ds
            JOIN sku_master sm ON ds.sku = sm.sku
        """
        params = []
        if sku:
            query += " WHERE ds.sku = ?"
            params.append(sku)

        query += " GROUP BY ds.sku, ds.sale_date, sm.first_sale_date ORDER BY ds.sku, ds.sale_date"

        df = read_sql(query, conn, params=params)

    if df.empty:
        return pd.DataFrame()

    df["sale_date"] = pd.to_datetime(df["sale_date"])
    df["first_sale_date"] = pd.to_datetime(df["first_sale_date"])

    if normalize:
        df["days_since_launch"] = (df["sale_date"] - df["first_sale_date"]).dt.days
        df["week"] = df["days_since_launch"] // 7

        # Aggregate by week
        weekly = (
            df.groupby(["sku", "week"])
            .agg({"units_sold": "sum", "revenue": "sum"})
            .reset_index()
        )
        return weekly
    else:
        return df


def classify_sku_trend(sku):
    """
    Classify a SKU as 'growing', 'declining', or 'stable' based on
    recent vs. earlier sales velocity.
    """
    lifecycle = get_sku_lifecycle_data(sku=sku, normalize=True)
    if lifecycle.empty or len(lifecycle) < 4:
        return "insufficient_data"

    midpoint = len(lifecycle) // 2
    first_half_avg = lifecycle.iloc[:midpoint]["units_sold"].mean()
    second_half_avg = lifecycle.iloc[midpoint:]["units_sold"].mean()

    if first_half_avg == 0:
        return "new"

    ratio = second_half_avg / first_half_avg
    if ratio > 1.15:
        return "growing"
    elif ratio < 0.85:
        return "declining"
    else:
        return "stable"
