"""
Demand forecasting using Facebook Prophet.
Per-SKU time series forecasting with seasonality, confidence intervals,
and reorder quantity recommendations.
"""
import pandas as pd
from prophet import Prophet
import logging
from db import get_db, read_sql
from config import FORECAST_HORIZON_DAYS, MIN_HISTORY_DAYS, LEAD_TIME_DAYS, SAFETY_STOCK_MULTIPLIER

# Suppress Prophet's verbose logging
logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)


def get_sku_daily_sales(sku=None):
    """Get daily sales time series, optionally filtered by SKU."""
    with get_db() as conn:
        if sku:
            df = read_sql("""
                SELECT sale_date as ds, SUM(units_sold) as y
                FROM daily_sku_sales
                WHERE sku = %s
                GROUP BY sale_date
                ORDER BY sale_date
            """, conn, params=[sku])
        else:
            df = read_sql("""
                SELECT sale_date as ds, sku, SUM(units_sold) as y
                FROM daily_sku_sales
                GROUP BY sale_date, sku
                ORDER BY sku, sale_date
            """, conn)

    if not df.empty:
        df["ds"] = pd.to_datetime(df["ds"])
    return df


def fill_missing_dates(df):
    """Fill in zero-sales days so Prophet has a complete time series."""
    if df.empty:
        return df
    date_range = pd.date_range(start=df["ds"].min(), end=df["ds"].max(), freq="D")
    full = pd.DataFrame({"ds": date_range})
    merged = full.merge(df, on="ds", how="left").fillna(0)
    return merged


def forecast_sku(sku, horizon_days=None):
    """
    Run Prophet forecast for a single SKU.

    Returns:
        dict with keys:
            - sku: str
            - history: DataFrame (ds, y)
            - forecast: DataFrame (ds, yhat, yhat_lower, yhat_upper)
            - method: 'prophet' or 'moving_average'
            - monthly_forecast: DataFrame (month, predicted_units)
            - reorder_info: dict with demand and reorder recommendations
    """
    if horizon_days is None:
        horizon_days = FORECAST_HORIZON_DAYS

    df = get_sku_daily_sales(sku)
    if df.empty:
        return None

    df = fill_missing_dates(df)
    history_days = (df["ds"].max() - df["ds"].min()).days

    result = {
        "sku": sku,
        "history": df.copy(),
    }

    if history_days < MIN_HISTORY_DAYS:
        # Fallback: simple moving average
        result["method"] = "moving_average"
        avg_daily = df["y"].mean()
        future_dates = pd.date_range(
            start=df["ds"].max() + pd.Timedelta(days=1),
            periods=horizon_days,
            freq="D"
        )
        forecast_df = pd.DataFrame({
            "ds": future_dates,
            "yhat": avg_daily,
            "yhat_lower": avg_daily * 0.7,
            "yhat_upper": avg_daily * 1.3,
        })
        result["forecast"] = forecast_df
    else:
        # Prophet forecast
        model = Prophet(
            daily_seasonality=False,
            weekly_seasonality=True,
            yearly_seasonality=True if history_days > 180 else False,
            changepoint_prior_scale=0.05,
            seasonality_prior_scale=10,
            interval_width=0.80,
        )
        model.fit(df[["ds", "y"]])

        future = model.make_future_dataframe(periods=horizon_days)
        forecast = model.predict(future)

        # Only keep the future portion
        future_forecast = forecast[forecast["ds"] > df["ds"].max()].copy()
        future_forecast["yhat"] = future_forecast["yhat"].clip(lower=0)
        future_forecast["yhat_lower"] = future_forecast["yhat_lower"].clip(lower=0)
        future_forecast["yhat_upper"] = future_forecast["yhat_upper"].clip(lower=0)

        result["method"] = "prophet"
        result["forecast"] = future_forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]]

    # Monthly aggregation
    fc = result["forecast"].copy()
    fc["month"] = fc["ds"].dt.to_period("M")
    monthly = fc.groupby("month").agg(
        predicted_units=("yhat", "sum"),
        lower_bound=("yhat_lower", "sum"),
        upper_bound=("yhat_upper", "sum"),
    ).reset_index()
    monthly["month"] = monthly["month"].astype(str)
    monthly["predicted_units"] = monthly["predicted_units"].round(0).astype(int)
    monthly["lower_bound"] = monthly["lower_bound"].round(0).astype(int)
    monthly["upper_bound"] = monthly["upper_bound"].round(0).astype(int)
    result["monthly_forecast"] = monthly

    # Reorder recommendations
    lead_time_demand = fc.head(LEAD_TIME_DAYS)["yhat"].sum()
    avg_daily_demand = fc["yhat"].mean()
    safety_stock = avg_daily_demand * LEAD_TIME_DAYS * (SAFETY_STOCK_MULTIPLIER - 1)

    result["reorder_info"] = {
        "avg_daily_demand": round(avg_daily_demand, 1),
        "lead_time_days": LEAD_TIME_DAYS,
        "lead_time_demand": round(lead_time_demand, 0),
        "safety_stock": round(safety_stock, 0),
        "reorder_point": round(lead_time_demand + safety_stock, 0),
        "suggested_order_qty_30d": round(avg_daily_demand * 30, 0),
        "suggested_order_qty_90d": round(avg_daily_demand * 90, 0),
    }

    return result


