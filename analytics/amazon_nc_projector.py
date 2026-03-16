"""
Amazon NC Projector — estimate yesterday's New Customer count and NC Revenue.

Amazon's Fulfilled Shipments report (the source of customer-level data) lags
behind the Sales & Traffic report (revenue only). This module projects NC
metrics for days where order-level data hasn't arrived yet.

Algorithm v2 — three independent approaches, median-blended:
1. Ridge Regression on engineered features (ML approach)
2. Same-DOW EWMA with bias correction (time series approach)
3. Revenue-scaled NC fraction with DOW adjustment (ratio approach)

Final projection = median of available methods (robust to one being way off).
NC Revenue = NC count × DOW-specific trailing AOV.
"""
import logging
import warnings
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from db import get_db, read_sql
from analytics.daily_metrics import first_order_cte

log = logging.getLogger(__name__)
warnings.filterwarnings('ignore', category=FutureWarning)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_daily_nc_history(conn, lookback_days=120, as_of_date=None):
    """Load daily Amazon NC metrics from orders + customers tables."""
    end = as_of_date or date.today()
    start = end - timedelta(days=lookback_days)

    _cte, _join, _fdate, _p = first_order_cte('amazon')
    df = read_sql(
        f"SELECT DATE(o.order_date) AS sale_date, "
        f"  COUNT(DISTINCT o.customer_id) AS total_customers, "
        f"  COUNT(DISTINCT CASE WHEN {_fdate} = DATE(o.order_date) "
        f"    THEN o.customer_id END) AS new_customers, "
        f"  SUM(oi.total_price) AS total_rev, "
        f"  SUM(CASE WHEN {_fdate} = DATE(o.order_date) "
        f"    THEN oi.total_price ELSE 0 END) AS nc_rev "
        f"FROM orders o "
        f"{_join} "
        f"JOIN order_items oi ON o.order_id = oi.order_id "
        f"WHERE o.source = %s AND o.order_date >= %s AND o.order_date <= %s "
        f"GROUP BY DATE(o.order_date) "
        f"ORDER BY sale_date",
        conn, params=_p + ('amazon', str(start), str(end)),
    )
    if not df.empty:
        df['sale_date'] = pd.to_datetime(df['sale_date']).dt.date
        for col in ['total_customers', 'new_customers']:
            df[col] = df[col].astype(int)
        for col in ['total_rev', 'nc_rev']:
            df[col] = df[col].astype(float)
    return df


def _load_daily_revenue(conn, lookback_days=120, as_of_date=None):
    """Load daily Amazon revenue from daily_sku_sales (arrives before NC data)."""
    end = as_of_date or date.today()
    start = end - timedelta(days=lookback_days)

    df = read_sql(
        "SELECT sale_date, SUM(revenue) AS amz_revenue "
        "FROM daily_sku_sales WHERE source = %s "
        "AND sale_date >= %s AND sale_date <= %s "
        "GROUP BY sale_date ORDER BY sale_date",
        conn, params=('amazon', str(start), str(end)),
    )
    if not df.empty:
        df['sale_date'] = pd.to_datetime(df['sale_date']).dt.date
        df['amz_revenue'] = df['amz_revenue'].astype(float)
    return df


def _load_dtc_daily_spend(conn, lookback_days=120, as_of_date=None):
    """Load daily DTC blended ad spend from google_sheet_data (spend only).
    NOTE: Only uses spend data. All customer/NC data comes from the main DB.
    """
    end = as_of_date or date.today()
    start = end - timedelta(days=lookback_days)

    df = read_sql(
        "SELECT date, blended_ad_spend FROM google_sheet_data "
        "WHERE date IS NOT NULL",
        conn,
    )
    if df.empty:
        return pd.DataFrame(columns=['sale_date', 'dtc_spend'])

    df['sale_date'] = pd.to_datetime(df['date'], format='mixed', dayfirst=False).dt.date
    df['dtc_spend'] = (
        df['blended_ad_spend']
        .astype(str)
        .str.replace('$', '', regex=False)
        .str.replace(',', '', regex=False)
        .apply(pd.to_numeric, errors='coerce')
        .fillna(0)
    )
    df = df[(df['sale_date'] >= start) & (df['sale_date'] <= end)]
    return df[['sale_date', 'dtc_spend']].sort_values('sale_date')


def _load_dtc_daily_nc(conn, lookback_days=120, as_of_date=None):
    """Load daily Shopify (DTC) NC count from the main DB."""
    end = as_of_date or date.today()
    start = end - timedelta(days=lookback_days)

    _cte, _join, _fdate, _p = first_order_cte('shopify')
    df = read_sql(
        f"WITH {_cte} "
        f"SELECT DATE(o.order_date) AS sale_date, "
        f"  COUNT(DISTINCT CASE WHEN {_fdate} = DATE(o.order_date) "
        f"    THEN o.customer_id END) AS dtc_nc "
        f"FROM orders o "
        f"{_join} "
        f"WHERE o.source = 'shopify' AND o.order_date >= %s AND o.order_date <= %s "
        f"GROUP BY DATE(o.order_date) "
        f"ORDER BY sale_date",
        conn, params=_p + (str(start), str(end)),
    )
    if not df.empty:
        df['sale_date'] = pd.to_datetime(df['sale_date']).dt.date
        df['dtc_nc'] = df['dtc_nc'].astype(int)
    return df


def _load_daily_spend(conn, lookback_days=120, as_of_date=None):
    """Load daily Amazon ad spend from amazon_daily_rollup."""
    end = as_of_date or date.today()
    start = end - timedelta(days=lookback_days)

    df = read_sql(
        "SELECT date AS sale_date, spend AS amz_spend "
        "FROM amazon_daily_rollup "
        "WHERE date >= %s AND date <= %s ORDER BY date",
        conn, params=(str(start), str(end)),
    )
    if not df.empty:
        df['sale_date'] = pd.to_datetime(df['sale_date']).dt.date
        df['amz_spend'] = df['amz_spend'].astype(float)
    return df


# ---------------------------------------------------------------------------
# Feature engineering (builds the feature matrix for ML + other methods)
# ---------------------------------------------------------------------------

def _build_feature_df(nc_hist, rev_hist, spend_hist, dtc_spend_hist, dtc_nc_hist):
    """Merge all data sources into a single feature DataFrame.

    Each row = one day. Features computed per-row from trailing windows.
    """
    df = nc_hist.copy()
    if df.empty:
        return df

    # Merge external signals
    for src, col in [(rev_hist, 'amz_revenue'), (spend_hist, 'amz_spend'),
                     (dtc_spend_hist, 'dtc_spend'), (dtc_nc_hist, 'dtc_nc')]:
        if not src.empty:
            df = df.merge(src, on='sale_date', how='left')
        if col not in df.columns:
            df[col] = np.nan

    df = df.sort_values('sale_date').reset_index(drop=True)

    # Time features
    dates = pd.to_datetime(df['sale_date'])
    df['dow'] = dates.dt.dayofweek
    df['month'] = dates.dt.month
    df['dom'] = dates.dt.day  # day of month
    df['is_weekend'] = (df['dow'] >= 5).astype(int)
    df['is_month_end'] = (df['dom'] >= 28).astype(int)

    # Rolling features (trailing — no data leakage)
    for window in [7, 14, 28]:
        df[f'nc_ma{window}'] = df['new_customers'].shift(1).rolling(window, min_periods=3).mean()
        df[f'nc_std{window}'] = df['new_customers'].shift(1).rolling(window, min_periods=3).std()
        df[f'rev_ma{window}'] = df['total_rev'].shift(1).rolling(window, min_periods=3).mean()

    # EWMA (exponential weighted — reacts faster to recent changes)
    df['nc_ewma7'] = df['new_customers'].shift(1).ewm(span=7, min_periods=3).mean()
    df['nc_ewma14'] = df['new_customers'].shift(1).ewm(span=14, min_periods=3).mean()

    # Same-DOW trailing average (last 4 same weekdays)
    df['nc_same_dow'] = np.nan
    for dow in range(7):
        mask = df['dow'] == dow
        dow_vals = df.loc[mask, 'new_customers'].shift(1)
        df.loc[mask, 'nc_same_dow'] = dow_vals.rolling(4, min_periods=1).mean()

    # NC fraction of total revenue (trailing)
    nc_frac = (df['nc_rev'].shift(1) / df['total_rev'].shift(1)).replace([np.inf, -np.inf], np.nan)
    df['nc_frac_l7'] = nc_frac.rolling(7, min_periods=3).mean()

    # NC-to-DTC-NC ratio (trailing)
    ratio = (df['new_customers'].shift(1) / df['dtc_nc'].shift(1)).replace([np.inf, -np.inf], np.nan)
    df['amz_dtc_nc_ratio'] = ratio.rolling(14, min_periods=5).mean()

    # Trend: L7D / L14D ratio
    df['trend_7_14'] = df['nc_ma7'] / df['nc_ma14']

    # Seasonal index
    from utils.constants import DEFAULT_SEASONAL_INDICES
    df['seasonal_idx'] = df['month'].map(DEFAULT_SEASONAL_INDICES)

    return df


# ---------------------------------------------------------------------------
# Method 1: Ridge Regression (learns optimal feature weights from data)
# ---------------------------------------------------------------------------

def _method_ridge(feature_df, target_idx):
    """Train a Ridge regression on all days before target, predict target day.

    Uses rolling retraining — trains on all available history before each
    prediction. This means the model adapts to changing patterns.
    """
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler

    feature_cols = [
        'nc_ma7', 'nc_ma14', 'nc_ma28', 'nc_ewma7', 'nc_ewma14',
        'nc_same_dow', 'trend_7_14', 'seasonal_idx',
        'is_weekend', 'is_month_end',
    ]
    # Add optional signal features if they have data
    for col in ['amz_revenue', 'dtc_nc', 'dtc_spend', 'amz_spend',
                'nc_frac_l7', 'amz_dtc_nc_ratio']:
        if col in feature_df.columns and feature_df[col].notna().sum() > 10:
            feature_cols.append(col)

    train = feature_df.iloc[:target_idx].dropna(subset=['new_customers'] + feature_cols)
    if len(train) < 14:
        return None, 0.0

    target_row = feature_df.iloc[target_idx]
    if any(pd.isna(target_row.get(c)) for c in feature_cols):
        # Fill NaN in target with column medians from training
        for c in feature_cols:
            if pd.isna(target_row.get(c)):
                target_row = target_row.copy()
                target_row[c] = train[c].median()

    X_train = train[feature_cols].values
    y_train = train['new_customers'].values
    X_pred = np.array([target_row[c] for c in feature_cols]).reshape(1, -1)

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_pred_s = scaler.transform(X_pred)

    model = Ridge(alpha=10.0)  # strong regularization to prevent overfitting
    model.fit(X_train_s, y_train)
    pred = model.predict(X_pred_s)[0]

    # Don't let ML go negative
    pred = max(pred, 0)

    # Confidence based on recent training R²
    recent_X = scaler.transform(train[feature_cols].tail(14).values)
    recent_y = train['new_customers'].tail(14).values
    recent_pred = model.predict(recent_X)
    ss_res = np.sum((recent_y - recent_pred) ** 2)
    ss_tot = np.sum((recent_y - recent_y.mean()) ** 2)
    r2 = max(0, 1 - ss_res / ss_tot) if ss_tot > 0 else 0
    confidence = 0.4 + r2 * 0.5  # scale R² into 0.4-0.9 range

    return pred, confidence


# ---------------------------------------------------------------------------
# Method 2: Same-DOW EWMA with bias correction
# ---------------------------------------------------------------------------

def _method_ewma_dow(feature_df, target_idx):
    """Exponentially weighted same-DOW average with bias correction.

    Instead of simple average of last 4 same weekdays, uses exponential
    weighting (recent same-DOWs matter more) and corrects for systematic
    bias by tracking recent prediction errors.
    """
    target_row = feature_df.iloc[target_idx]
    train = feature_df.iloc[:target_idx]
    target_dow = int(target_row['dow'])

    # Get same-DOW history
    same_dow = train[train['dow'] == target_dow].sort_values('sale_date', ascending=False)
    if len(same_dow) < 2:
        return None, 0.0

    # EWMA on same-DOW values (span=3 = last ~3 same weekdays weighted)
    vals = same_dow['new_customers'].values[:6]  # last 6 same weekdays (~42 days)
    alpha = 0.5  # heavy recency weight
    weights = np.array([(1 - alpha) ** i for i in range(len(vals))])
    weights /= weights.sum()
    ewma_pred = np.dot(weights, vals)

    # Bias correction: check how our EWMA would have performed on recent same-DOW days
    if len(same_dow) >= 4:
        errors = []
        for i in range(1, min(4, len(same_dow))):
            actual = same_dow.iloc[i - 1]['new_customers']
            hist_vals = same_dow.iloc[i:i + 5]['new_customers'].values
            if len(hist_vals) >= 2:
                w = np.array([(1 - alpha) ** j for j in range(len(hist_vals))])
                w /= w.sum()
                hist_pred = np.dot(w, hist_vals)
                errors.append(hist_pred - actual)
        if errors:
            avg_bias = np.mean(errors)
            # Correct 70% of the bias (don't overcorrect)
            ewma_pred -= avg_bias * 0.7

    # Trend adjustment from overall EWMA
    nc_ewma7 = target_row.get('nc_ewma7')
    nc_ma14 = target_row.get('nc_ma14')
    if pd.notna(nc_ewma7) and pd.notna(nc_ma14) and nc_ma14 > 0:
        trend = nc_ewma7 / nc_ma14
        trend_adj = 1.0 + (trend - 1.0) * 0.3  # dampen
        trend_adj = max(0.85, min(1.15, trend_adj))
        ewma_pred *= trend_adj

    ewma_pred = max(ewma_pred, 0)
    confidence = min(0.85, 0.5 + len(same_dow) * 0.05)

    return ewma_pred, confidence


# ---------------------------------------------------------------------------
# Method 3: Revenue-scaled NC with DOW-specific ratios
# ---------------------------------------------------------------------------

def _method_revenue_scaled(feature_df, target_idx):
    """If total Amazon revenue is known for the target day, scale by
    DOW-specific NC-to-revenue ratio.

    Key insight: the NC fraction of total revenue varies by DOW
    (weekends have different mix). Using DOW-specific ratios is more accurate.
    """
    target_row = feature_df.iloc[target_idx]
    train = feature_df.iloc[:target_idx]

    target_rev = target_row.get('amz_revenue')
    if pd.isna(target_rev) or target_rev <= 0:
        # Try total_rev from orders table instead
        target_rev = target_row.get('total_rev')
        if pd.isna(target_rev) or target_rev <= 0:
            return None, 0.0

    target_dow = int(target_row['dow'])

    # DOW-specific NC-per-dollar and NC-rev-fraction
    same_dow = train[train['dow'] == target_dow].sort_values('sale_date', ascending=False).head(8)
    if len(same_dow) < 3:
        same_dow = train.sort_values('sale_date', ascending=False).head(30)

    valid = same_dow[same_dow['total_rev'] > 0]
    if len(valid) < 3:
        return None, 0.0

    nc_per_dollar = valid['new_customers'].sum() / valid['total_rev'].sum()
    projected_nc = target_rev * nc_per_dollar

    projected_nc = max(projected_nc, 0)
    confidence = 0.70 if len(same_dow) >= 4 else 0.55

    return projected_nc, confidence


# ---------------------------------------------------------------------------
# Method 4: DTC NC ratio (Shopify NC as leading indicator)
# ---------------------------------------------------------------------------

def _method_dtc_nc(feature_df, target_idx):
    """Use same-day Shopify NC (available in real-time) to estimate Amazon NC.

    Uses DOW-specific Amazon-to-DTC NC ratio.
    """
    target_row = feature_df.iloc[target_idx]
    train = feature_df.iloc[:target_idx]

    dtc_nc = target_row.get('dtc_nc')
    if pd.isna(dtc_nc) or dtc_nc <= 0:
        return None, 0.0

    target_dow = int(target_row['dow'])

    # DOW-specific Amazon/DTC ratio
    same_dow = train[(train['dow'] == target_dow) & (train['dtc_nc'] > 0)].sort_values(
        'sale_date', ascending=False
    ).head(8)
    if len(same_dow) < 3:
        same_dow = train[train['dtc_nc'] > 0].sort_values(
            'sale_date', ascending=False
        ).head(30)

    if len(same_dow) < 5:
        return None, 0.0

    ratio = same_dow['new_customers'].sum() / same_dow['dtc_nc'].sum()
    projected_nc = dtc_nc * ratio

    projected_nc = max(projected_nc, 0)
    confidence = 0.60

    return projected_nc, confidence


# ---------------------------------------------------------------------------
# DOW-specific AOV
# ---------------------------------------------------------------------------

def _dow_nc_aov(train_df, target_dow):
    """Compute DOW-specific NC AOV for revenue estimation."""
    same_dow = train_df[train_df['dow'] == target_dow].sort_values(
        'sale_date', ascending=False
    ).head(8)
    if len(same_dow) >= 3:
        total_nc = same_dow['new_customers'].sum()
        total_rev = same_dow['nc_rev'].sum()
        if total_nc > 0:
            return total_rev / total_nc

    # Fallback: all-days AOV
    recent = train_df.sort_values('sale_date', ascending=False).head(30)
    total_nc = recent['new_customers'].sum()
    total_rev = recent['nc_rev'].sum()
    return total_rev / total_nc if total_nc > 0 else 40.0


# ---------------------------------------------------------------------------
# Ensemble: median-blend with outlier detection
# ---------------------------------------------------------------------------

def project_amazon_nc(target_date=None, conn=None):
    """Project Amazon NC customers and NC revenue for a target date.

    Returns dict with projected_nc_customers, projected_nc_revenue,
    confidence, methods_used, method_details, actual_nc_customers/revenue.
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    if conn is None:
        with get_db() as c:
            return _project_amazon_nc_inner(target_date, c)
    return _project_amazon_nc_inner(target_date, conn)


def _project_amazon_nc_inner(target_date, conn):
    """Inner projection logic using feature-engineered ensemble."""
    # Load all data sources
    nc_hist = _load_daily_nc_history(conn, lookback_days=120, as_of_date=target_date)
    rev_hist = _load_daily_revenue(conn, lookback_days=120, as_of_date=target_date)
    spend_hist = _load_daily_spend(conn, lookback_days=120, as_of_date=target_date)
    dtc_spend_hist = _load_dtc_daily_spend(conn, lookback_days=120, as_of_date=target_date)
    dtc_nc_hist = _load_dtc_daily_nc(conn, lookback_days=120, as_of_date=target_date)

    # Build unified feature DataFrame
    feature_df = _build_feature_df(nc_hist, rev_hist, spend_hist, dtc_spend_hist, dtc_nc_hist)
    if feature_df.empty:
        return _empty_result(target_date)

    # Find the target date's row index
    target_mask = feature_df['sale_date'] == target_date
    if not target_mask.any():
        # Target date not in feature_df — append a synthetic row
        feature_df = _append_target_row(feature_df, target_date, rev_hist,
                                         dtc_spend_hist, dtc_nc_hist, spend_hist)
        target_mask = feature_df['sale_date'] == target_date
        if not target_mask.any():
            return _empty_result(target_date)

    target_idx = feature_df[target_mask].index[0]

    # Check for actual data
    actual_nc = None
    actual_rev = None
    target_row = feature_df.iloc[target_idx]
    if target_row['new_customers'] > 0:
        actual_nc = int(target_row['new_customers'])
        actual_rev = float(target_row['nc_rev'])

    # Run all methods
    methods = {}

    try:
        nc1, conf1 = _method_ridge(feature_df, target_idx)
        if nc1 is not None:
            methods['ridge_ml'] = {'nc': nc1, 'confidence': conf1}
    except Exception as e:
        log.debug('Ridge method failed: %s', e)

    nc2, conf2 = _method_ewma_dow(feature_df, target_idx)
    if nc2 is not None:
        methods['ewma_dow'] = {'nc': nc2, 'confidence': conf2}

    nc3, conf3 = _method_revenue_scaled(feature_df, target_idx)
    if nc3 is not None:
        methods['revenue_scaled'] = {'nc': nc3, 'confidence': conf3}

    nc4, conf4 = _method_dtc_nc(feature_df, target_idx)
    if nc4 is not None:
        methods['dtc_nc_ratio'] = {'nc': nc4, 'confidence': conf4}

    if not methods:
        return _empty_result(target_date, actual_nc, actual_rev)

    # Ensemble: use MEDIAN of method predictions (robust to one outlier method)
    nc_predictions = [m['nc'] for m in methods.values()]
    blended_nc = float(np.median(nc_predictions))

    # Also compute confidence-weighted mean for comparison
    total_weight = sum(m['confidence'] for m in methods.values())
    weighted_nc = sum(m['nc'] * m['confidence'] for m in methods.values()) / total_weight

    # If median and weighted agree within 15%, use median; otherwise average them
    if abs(blended_nc - weighted_nc) / max(blended_nc, 1) < 0.15:
        final_nc = blended_nc
    else:
        final_nc = (blended_nc + weighted_nc) / 2

    # Guardrails: IQR-based capping
    train = feature_df.iloc[:target_idx]
    recent = train.sort_values('sale_date', ascending=False).head(14)
    if len(recent) >= 7:
        p25 = recent['new_customers'].quantile(0.20)
        p75 = recent['new_customers'].quantile(0.80)
        iqr = p75 - p25
        floor_nc = max(p25 - 1.5 * iqr, 0)
        ceil_nc = p75 + 1.5 * iqr
        final_nc = np.clip(final_nc, floor_nc, ceil_nc)

    # Revenue: DOW-specific AOV × NC count
    target_dow = int(target_row['dow'])
    aov = _dow_nc_aov(train, target_dow)
    final_rev = final_nc * aov

    # Store per-method details
    for name, m in methods.items():
        m['rev'] = m['nc'] * aov

    blended_conf = np.mean([m['confidence'] for m in methods.values()])

    return {
        'target_date': target_date,
        'projected_nc_customers': round(final_nc, 1),
        'projected_nc_revenue': round(final_rev, 2),
        'confidence': round(min(blended_conf, 1.0), 3),
        'methods_used': list(methods.keys()),
        'method_details': methods,
        'actual_nc_customers': actual_nc,
        'actual_nc_revenue': actual_rev,
    }


def _append_target_row(feature_df, target_date, rev_hist, dtc_spend_hist,
                        dtc_nc_hist, spend_hist):
    """Append a row for target_date with available signal data but 0 NC."""
    row = {'sale_date': target_date, 'total_customers': 0, 'new_customers': 0,
           'total_rev': 0, 'nc_rev': 0}

    # Fill in available signals
    for hist, date_col, val_col in [
        (rev_hist, 'sale_date', 'amz_revenue'),
        (dtc_spend_hist, 'sale_date', 'dtc_spend'),
        (dtc_nc_hist, 'sale_date', 'dtc_nc'),
        (spend_hist, 'sale_date', 'amz_spend'),
    ]:
        if not hist.empty:
            match = hist[hist[date_col] == target_date]
            if not match.empty:
                row[val_col] = float(match[val_col].iloc[0])

    new_row = pd.DataFrame([row])
    combined = pd.concat([feature_df, new_row], ignore_index=True)
    return _build_feature_df_incremental(combined, feature_df)


def _build_feature_df_incremental(combined, original_df):
    """Rebuild features for a combined DataFrame (original + new target row)."""
    # Simpler approach: just rebuild from scratch
    # (This is called once per projection, not in a hot loop)
    df = combined.sort_values('sale_date').reset_index(drop=True)

    dates = pd.to_datetime(df['sale_date'])
    df['dow'] = dates.dt.dayofweek
    df['month'] = dates.dt.month
    df['dom'] = dates.dt.day
    df['is_weekend'] = (df['dow'] >= 5).astype(int)
    df['is_month_end'] = (df['dom'] >= 28).astype(int)

    for window in [7, 14, 28]:
        df[f'nc_ma{window}'] = df['new_customers'].shift(1).rolling(window, min_periods=3).mean()
        df[f'nc_std{window}'] = df['new_customers'].shift(1).rolling(window, min_periods=3).std()
        df[f'rev_ma{window}'] = df['total_rev'].shift(1).rolling(window, min_periods=3).mean()

    df['nc_ewma7'] = df['new_customers'].shift(1).ewm(span=7, min_periods=3).mean()
    df['nc_ewma14'] = df['new_customers'].shift(1).ewm(span=14, min_periods=3).mean()

    df['nc_same_dow'] = np.nan
    for dow in range(7):
        mask = df['dow'] == dow
        dow_vals = df.loc[mask, 'new_customers'].shift(1)
        df.loc[mask, 'nc_same_dow'] = dow_vals.rolling(4, min_periods=1).mean()

    nc_frac = (df['nc_rev'].shift(1) / df['total_rev'].shift(1)).replace([np.inf, -np.inf], np.nan)
    df['nc_frac_l7'] = nc_frac.rolling(7, min_periods=3).mean()

    if 'dtc_nc' in df.columns:
        ratio = (df['new_customers'].shift(1) / df['dtc_nc'].shift(1)).replace([np.inf, -np.inf], np.nan)
        df['amz_dtc_nc_ratio'] = ratio.rolling(14, min_periods=5).mean()

    df['trend_7_14'] = df['nc_ma7'] / df['nc_ma14']

    from utils.constants import DEFAULT_SEASONAL_INDICES
    df['seasonal_idx'] = df['month'].map(DEFAULT_SEASONAL_INDICES)

    return df


def _empty_result(target_date, actual_nc=None, actual_rev=None):
    return {
        'target_date': target_date,
        'projected_nc_customers': 0,
        'projected_nc_revenue': 0.0,
        'confidence': 0.0,
        'methods_used': [],
        'method_details': {},
        'actual_nc_customers': actual_nc,
        'actual_nc_revenue': actual_rev,
    }


# ---------------------------------------------------------------------------
# Backtest harness
# ---------------------------------------------------------------------------

def backtest(days=60, conn=None):
    """Run the projector against the last N days and compare to actuals."""
    if conn is None:
        with get_db() as c:
            return backtest(days=days, conn=c)

    results = []
    end_date = date.today() - timedelta(days=1)

    for i in range(days):
        target = end_date - timedelta(days=i)
        proj = _project_amazon_nc_inner(target, conn)

        if proj['actual_nc_customers'] is None or proj['actual_nc_customers'] == 0:
            continue

        actual_nc = proj['actual_nc_customers']
        actual_rev = proj['actual_nc_revenue']
        pred_nc = proj['projected_nc_customers']
        pred_rev = proj['projected_nc_revenue']

        nc_err = pred_nc - actual_nc
        nc_pct = abs(nc_err / actual_nc) * 100 if actual_nc > 0 else 0
        rev_err = pred_rev - actual_rev
        rev_pct = abs(rev_err / actual_rev) * 100 if actual_rev > 0 else 0

        results.append({
            'date': target,
            'dow': target.strftime('%a'),
            'dow_num': target.weekday(),
            'projected_nc': pred_nc,
            'actual_nc': actual_nc,
            'nc_error': round(nc_err, 1),
            'nc_pct_error': round(nc_pct, 1),
            'projected_rev': round(pred_rev, 2),
            'actual_rev': round(actual_rev, 2),
            'rev_error': round(rev_err, 2),
            'rev_pct_error': round(rev_pct, 1),
            'confidence': proj['confidence'],
            'methods_used': ','.join(proj['methods_used']),
        })

    return pd.DataFrame(results)
