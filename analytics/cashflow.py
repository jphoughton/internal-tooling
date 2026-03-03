"""
Cash Flow forecasting: transaction normalization, classification, and projection engine.

CFO perspective: This module powers the 52-week cash flow forecast by:
1. Normalizing bank transaction summaries into consistent patterns
2. Auto-classifying transactions using user-defined mappings
3. Computing auto-calibrated payout ratios from actuals
4. Building weekly cash flow projections with confidence intervals
"""
import logging
import re
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import numpy as np

from db import get_db, read_sql, ConnectionWrapper, DatabaseError
from utils.constants import (
    CASHFLOW_CATEGORIES, CASHFLOW_SEED_DEFAULTS,
    DTC_DOW_WEIGHTS, CASHFLOW_CONFIDENCE_WEEKLY_GROWTH,
    CASHFLOW_CONFIDENCE_MAX,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transaction normalization
# ---------------------------------------------------------------------------
# Patterns to strip from summaries to create stable matching keys
_STRIP_PATTERNS = [
    # Numeric IDs (funding IDs, invoice numbers, reference numbers)
    r'\b[a-z0-9]{8,}\b',           # long alphanumeric IDs
    r'\b\d{4,}\b',                  # 4+ digit numbers
    r'#\S+',                        # anything after #
    r'ID:\S*',                      # ID:xxx
    r'REF:\S*',                     # REF:xxx
    r'TRACE:\S*',                   # TRACE:xxx
    r'SEQ:\S*',                     # SEQ:xxx
    # Dollar amounts
    r'\$[\d,]+\.?\d*',
    # Dates in various formats
    r'\d{1,2}/\d{1,2}/\d{2,4}',
    r'\d{4}-\d{2}-\d{2}',
    # Trailing/leading whitespace and extra spaces
]

_COMPILED_STRIP = [re.compile(p) for p in _STRIP_PATTERNS]


def normalize_summary(raw: str) -> str:
    """Normalize a transaction summary into a stable pattern for matching.

    Strips variable parts (IDs, amounts, dates) so that the same vendor
    always maps to the same pattern regardless of invoice number.
    """
    if not raw:
        return ''
    text = raw.lower().strip()
    for pat in _COMPILED_STRIP:
        text = pat.sub('', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Strip trailing punctuation
    text = text.rstrip(':;,.-')
    return text


# ---------------------------------------------------------------------------
# Category suggestion (basic regex hints for pre-filling mappings)
# ---------------------------------------------------------------------------
_SUGGESTIONS = [
    # Revenue
    (r'shopify|shpfy', 'dtc_revenue', 'shopify_payout'),
    (r'stripe', 'dtc_revenue', 'stripe_payout'),
    (r'paypal|braintree', 'dtc_revenue', 'paypal_payout'),
    (r'tiktok.*payout|tiktok.*deposit', 'tiktok_revenue', None),
    (r'amazon.*disburse|amzn.*disburse|amazon.*deposit', 'amazon_revenue', None),
    (r'faire', 'wholesale_revenue', 'faire'),
    (r'interest|reward|cashback', 'interest_income', None),
    # Expenses
    (r'meta\s|facebook|fb\s', 'media', 'meta_ads'),
    (r'google\s*ads|adwords', 'media', 'google_ads'),
    (r'tiktok.*ads|tiktok.*market', 'media', 'tiktok_ads'),
    (r'applovin|app\s*lovin', 'media', 'applovin'),
    (r'brand\s*expand', 'media', 'brand_expand'),
    (r'postscript|klaviyo', 'media', 'email_sms'),
    (r'justworks|payroll', 'payroll', None),
    (r'jameson', 'loan', None),
    (r'avenue\s*shop|packiyo', 'fulfillment', None),
    (r'stikpak|mars\s*print|manufactur', 'production', None),
    (r'tax\s*payment|state\s*of|dept.*revenue|franchise\s*tax', 'sales_tax', None),
    (r'clickup|wydor|figma|intelligems|recharge|gorgias|shipbob|notion|slack|zapier|airtable|hubspot|google\s*workspace', 'software', None),
    (r'passport|unishippers|ups\b|fedex', 'shipping', None),
    (r'tones\s*digital|oddduck|gunell', 'agency', None),
    (r'launch\s*cpa|intuit|quickbooks', 'accounting', None),
    (r'insurance|hartford|next\s*insurance', 'insurance', None),
    # Transfers
    (r'hydrant.*transfer|transfer.*hydrant|internal.*transfer', 'internal_transfer', None),
    (r'ramp.*payment|ramp.*autopay', 'duplicate', None),
]

_COMPILED_SUGGESTIONS = [(re.compile(p, re.IGNORECASE), cat, sub) for p, cat, sub in _SUGGESTIONS]


def suggest_category(normalized: str) -> tuple:
    """Return (category, subcategory) suggestion based on regex hints, or ('unmapped', None)."""
    for pat, cat, sub in _COMPILED_SUGGESTIONS:
        if pat.search(normalized):
            return (cat, sub)
    return ('unmapped', None)


# ---------------------------------------------------------------------------
# Transaction classification (DB-backed)
# ---------------------------------------------------------------------------
def classify_transaction(
    conn: ConnectionWrapper,
    summary: str,
    direction: str,
    amount: float,
    account: str,
) -> tuple:
    """Classify a transaction using DB mappings.

    Returns (category, subcategory, is_transfer, is_duplicate).
    Falls back to suggestion engine, then 'unmapped'.
    """
    normalized = normalize_summary(summary)
    if not normalized:
        return ('unmapped', None, 0, 0)

    # Look up in category_mappings
    try:
        row = conn.execute(
            "SELECT category, subcategory, is_transfer, is_duplicate "
            "FROM category_mappings WHERE match_pattern = %s",
            (normalized,)
        ).fetchone()
        if row:
            return (row['category'], row['subcategory'],
                    int(row['is_transfer'] or 0), int(row['is_duplicate'] or 0))
    except Exception as e:
        log.warning('classify_transaction DB lookup failed: %s', e)

    # Fall back to regex suggestions
    cat, sub = suggest_category(normalized)
    is_transfer = 1 if cat == 'internal_transfer' else 0
    is_dup = 1 if cat == 'duplicate' else 0
    return (cat, sub, is_transfer, is_dup)


def get_unmapped_patterns(conn: ConnectionWrapper) -> pd.DataFrame:
    """Get all unique normalized patterns that haven't been mapped yet."""
    return read_sql("""
        SELECT
            ct.normalized_pattern,
            MIN(ct.summary) as example,
            COUNT(*) as tx_count,
            ct.direction,
            SUM(ct.amount) as total_amount
        FROM (
            SELECT
                summary,
                direction,
                amount,
                category,
                LOWER(COALESCE(summary, '')) as normalized_pattern
            FROM cashflow_transactions
            WHERE category = 'unmapped' OR category IS NULL
        ) ct
        GROUP BY ct.normalized_pattern, ct.direction
        ORDER BY tx_count DESC
    """, conn)


def get_mapping_stats(conn: ConnectionWrapper) -> dict:
    """Get mapping coverage stats."""
    total = conn.execute(
        "SELECT COUNT(DISTINCT summary) as cnt FROM cashflow_transactions"
    ).fetchone()
    mapped = conn.execute(
        "SELECT COUNT(*) as cnt FROM category_mappings"
    ).fetchone()
    unmapped_tx = conn.execute(
        "SELECT COUNT(*) as cnt FROM cashflow_transactions WHERE category = 'unmapped' OR category IS NULL"
    ).fetchone()
    total_tx = conn.execute(
        "SELECT COUNT(*) as cnt FROM cashflow_transactions"
    ).fetchone()
    return {
        'unique_patterns': total['cnt'] if total else 0,
        'mapped_patterns': mapped['cnt'] if mapped else 0,
        'unmapped_tx_count': unmapped_tx['cnt'] if unmapped_tx else 0,
        'total_tx_count': total_tx['cnt'] if total_tx else 0,
    }


def reclassify_all_transactions(conn: ConnectionWrapper) -> int:
    """Re-classify all transactions using current mappings. Returns count updated."""
    # Get all mappings
    mappings = conn.execute(
        "SELECT match_pattern, category, subcategory, is_transfer, is_duplicate "
        "FROM category_mappings"
    ).fetchall()
    if not mappings:
        return 0

    # Load all transactions once and build normalized-pattern → tx_ids lookup
    txs = read_sql(
        "SELECT tx_id, summary FROM cashflow_transactions",
        conn,
    )
    if txs.empty:
        return 0

    pattern_to_txids = {}
    for _, tx in txs.iterrows():
        norm = normalize_summary(tx['summary'] or '')
        pattern_to_txids.setdefault(norm, []).append(tx['tx_id'])

    updated = 0
    for mapping in mappings:
        tx_ids = pattern_to_txids.get(mapping['match_pattern'], [])
        if tx_ids:
            placeholders = ','.join(['%s'] * len(tx_ids))
            conn.execute(
                f"UPDATE cashflow_transactions SET category = %s, subcategory = %s, "
                f"is_transfer = %s, is_duplicate = %s "
                f"WHERE tx_id IN ({placeholders})",
                (mapping['category'], mapping['subcategory'],
                 mapping['is_transfer'], mapping['is_duplicate'], *tx_ids),
            )
            updated += len(tx_ids)

    return updated


# ---------------------------------------------------------------------------
# Auto-calibrated payout ratios
# ---------------------------------------------------------------------------
def compute_payout_ratio(
    conn: ConnectionWrapper,
    channel: str,
    lookback_weeks: int = 12,
) -> Optional[float]:
    """Compute payout ratio from DB revenue vs actual bank deposits.

    DTC: daily_sku_sales(source='shopify') revenue vs cashflow_transactions(category='dtc_revenue')
    Amazon: daily_sku_sales(source='amazon') vs cashflow_transactions(category='amazon_revenue')

    Uses EWMA to favor recent weeks. Returns None if insufficient data.
    """
    if channel == 'dtc':
        source = 'shopify'
        category = 'dtc_revenue'
        lag_days = 3  # DTC settles in ~3 days
    elif channel == 'amazon':
        source = 'amazon'
        category = 'amazon_revenue'
        lag_days = 21  # Amazon 21-day settlement lag
    else:
        return None

    end_date = date.today()
    start_date = end_date - timedelta(weeks=lookback_weeks)

    # Get weekly DB revenue
    revenue_df = read_sql("""
        SELECT
            DATE_TRUNC('week', sale_date::date)::text as week_start,
            SUM(revenue) as revenue
        FROM daily_sku_sales
        WHERE source = %s AND sale_date >= %s AND sale_date <= %s
        GROUP BY DATE_TRUNC('week', sale_date::date)
        ORDER BY week_start
    """, conn, params=(source, str(start_date), str(end_date)))

    # Get weekly bank deposits (shifted by lag)
    deposit_start = start_date + timedelta(days=lag_days)
    deposit_end = end_date + timedelta(days=lag_days)
    deposit_df = read_sql("""
        SELECT
            DATE_TRUNC('week', tx_date::date)::text as week_start,
            SUM(amount) as deposits
        FROM cashflow_transactions
        WHERE category = %s AND direction = 'credit'
            AND tx_date >= %s AND tx_date <= %s
            AND is_transfer = 0 AND is_duplicate = 0
        GROUP BY DATE_TRUNC('week', tx_date::date)
        ORDER BY week_start
    """, conn, params=(category, str(deposit_start), str(deposit_end)))

    if revenue_df.empty or deposit_df.empty:
        return None

    # Merge on week and compute ratio
    merged = revenue_df.merge(deposit_df, on='week_start', how='inner')
    if len(merged) < 4:
        return None

    merged['ratio'] = merged['deposits'] / merged['revenue'].replace(0, np.nan)
    merged = merged.dropna(subset=['ratio'])
    if merged.empty:
        return None

    # EWMA with span proportional to lookback
    ratios = merged['ratio'].values
    span = min(len(ratios), 8)
    weights = pd.Series(ratios).ewm(span=span).mean()
    return float(weights.iloc[-1])


# ---------------------------------------------------------------------------
# Trailing average helper
# ---------------------------------------------------------------------------
def compute_trailing_avg(
    conn: ConnectionWrapper,
    category: str,
    lookback_weeks: int = 8,
) -> float:
    """Compute trailing weekly average spend for a given category."""
    end_date = date.today()
    start_date = end_date - timedelta(weeks=lookback_weeks)

    # Determine direction from category group (expenses are debits, revenue is credits)
    cat_group = CASHFLOW_CATEGORIES.get(category, {}).get('group', '')
    if cat_group == 'expense':
        result = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM cashflow_transactions
            WHERE category = %s
                AND tx_date >= %s AND tx_date <= %s
                AND is_transfer = 0 AND is_duplicate = 0
                AND direction = 'debit'
        """, (category, str(start_date), str(end_date))).fetchone()
    else:
        result = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM cashflow_transactions
            WHERE category = %s
                AND tx_date >= %s AND tx_date <= %s
                AND is_transfer = 0 AND is_duplicate = 0
        """, (category, str(start_date), str(end_date))).fetchone()

    total = float(result['total'] or 0)
    return total / max(lookback_weeks, 1)


# ---------------------------------------------------------------------------
# 52-Week Cash Flow Projection Engine
# ---------------------------------------------------------------------------

def _get_week_boundaries(start_date: date, weeks: int) -> list:
    """Generate list of (week_start, week_end) tuples from start_date."""
    # Align to Monday
    days_since_monday = start_date.weekday()
    week_start = start_date - timedelta(days=days_since_monday)
    result = []
    for _ in range(weeks):
        week_end = week_start + timedelta(days=6)
        result.append((week_start, week_end))
        week_start = week_end + timedelta(days=1)
    return result


def _get_actual_weekly_totals(conn: ConnectionWrapper, category: str, weeks: list) -> dict:
    """Get actual transaction totals for each week, keyed by week_start string.

    Filters by direction based on category group: revenue categories only
    count credits, expense categories only count debits.  This prevents
    misclassified transactions (e.g. a debit mapped to a revenue category)
    from corrupting actuals.
    """
    if not weeks:
        return {}
    first_start = str(weeks[0][0])
    last_end = str(weeks[-1][1])

    # Determine expected transaction direction from category group
    group = CASHFLOW_CATEGORIES.get(category, {}).get('group', 'expense')
    direction_filter = 'credit' if group == 'revenue' else 'debit'

    df = read_sql("""
        SELECT tx_date, SUM(amount) as total
        FROM cashflow_transactions
        WHERE category = %s
            AND direction = %s
            AND tx_date >= %s AND tx_date <= %s
            AND is_transfer = 0 AND is_duplicate = 0
        GROUP BY tx_date
    """, conn, params=(category, direction_filter, first_start, last_end))

    if df.empty:
        return {}

    df['tx_date'] = pd.to_datetime(df['tx_date'])
    result = {}
    for ws, we in weeks:
        ws_dt = pd.Timestamp(ws)
        we_dt = pd.Timestamp(we)
        mask = (df['tx_date'] >= ws_dt) & (df['tx_date'] <= we_dt)
        result[str(ws)] = float(df.loc[mask, 'total'].sum())
    return result


def _build_amazon_disbursement_schedule(week_list: list) -> dict:
    """Pre-compute Amazon disbursement events across ALL weeks at once.

    Amazon disburses biweekly, typically around day 8 (early) and day 24
    (late) of each month.  Each disbursement is assigned to exactly one
    week — the week that contains the midpoint date — eliminating the
    double-counting bug where a disbursement window spanning two weeks
    was detected by both.

    Returns dict keyed by week_start string: {ws_str: {month_key: count}}.
    """
    if not week_list:
        return {}

    first_ws = week_list[0][0]
    last_we = week_list[-1][1]

    # Generate all disbursement midpoint dates in the date range.
    # Early window midpoint = 8th, late window midpoint = 24th.
    disbursement_dates = []  # list of (date, month_key)
    y, m = first_ws.year, first_ws.month
    last_y, last_m = last_we.year, last_we.month
    while (y, m) <= (last_y, last_m):
        month_key = f'{y:04d}-{m:02d}'
        for mid_day in (8, 24):
            try:
                d = date(y, m, mid_day)
            except ValueError:
                continue  # shouldn't happen for day 8 or 24
            if first_ws <= d <= last_we:
                disbursement_dates.append((d, month_key))
        m += 1
        if m > 12:
            m = 1
            y += 1

    # Assign each disbursement to the week that contains its midpoint date.
    schedule = {}  # {ws_str: {month_key: count}}
    for disb_date, month_key in disbursement_dates:
        for ws, we in week_list:
            if ws <= disb_date <= we:
                ws_str = str(ws)
                if ws_str not in schedule:
                    schedule[ws_str] = {}
                schedule[ws_str][month_key] = schedule[ws_str].get(month_key, 0) + 1
                break  # assigned — move to next disbursement

    return schedule


def _project_revenue_week(
    conn: ConnectionWrapper,
    category: str,
    week_start: date,
    week_end: date,
    ctx: dict,
) -> float:
    """Project revenue for a single future week."""
    if category == 'dtc_revenue':
        # Use waterfall forecast monthly revenue, distribute to weeks.
        # Apply DTC_DOW_WEIGHTS to handle weeks that span two months:
        # each day's share of the weekly revenue is weighted by its
        # day-of-week payout weight, attributed to the correct month.
        monthly_rev = ctx.get('dtc_monthly_revenue', {})
        ratio = ctx.get('dtc_payout_ratio', 0.94)

        # Accumulate weighted daily revenue across the week
        total = 0.0
        weight_sum = sum(DTC_DOW_WEIGHTS.values())
        if weight_sum <= 0:
            weight_sum = 1.0

        d = week_start
        while d <= week_end:
            dow_weight = DTC_DOW_WEIGHTS.get(d.weekday(), 0.0)
            month_key = d.strftime('%Y-%m')
            monthly = monthly_rev.get(month_key, 0)
            # Daily share = monthly / ~30.44 days, then scaled by DOW weight
            # relative to the average daily weight (weight_sum / 7)
            daily_share = (monthly / 30.44) * (dow_weight / (weight_sum / 7))
            total += daily_share
            d += timedelta(days=1)

        return total * ratio

    elif category == 'amazon_revenue':
        # Amazon disburses biweekly (~8th and ~24th of each month).
        # Non-disbursement weeks show $0 so the table reflects actual
        # deposit timing.  Uses pre-computed schedule from ctx to avoid
        # the per-week double-counting bug.
        monthly_rev = ctx.get('amazon_monthly_revenue', {})
        ratio = ctx.get('amazon_payout_ratio', 0.62)

        schedule = ctx.get('_amazon_disbursements', {})
        week_disb = schedule.get(str(week_start), {})
        if not week_disb:
            return 0.0

        # Each disbursement event = half the month's payout
        total = 0.0
        for month_key, count in week_disb.items():
            monthly = monthly_rev.get(month_key, 0)
            total += (monthly * ratio / 2) * count
        return total

    else:
        # Trailing average for other revenue categories
        avg = compute_trailing_avg(conn, category, lookback_weeks=8)
        return avg


def _detect_expense_schedule(
    conn: ConnectionWrapper,
    category: str,
    lookback_months: int = 6,
) -> dict:
    """Detect when payments typically hit for a category from bank history.

    Analyzes inter-payment gaps to determine frequency (weekly, biweekly,
    monthly, quarterly) and predicts future payment dates.

    Returns dict with: has_data, frequency, avg_gap_days, last_payment_date,
    last_payment_amount, avg_amount, monthly_total, payment_dates (day-of-month list).
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_months * 30)

    df = read_sql("""
        SELECT tx_date, amount
        FROM cashflow_transactions
        WHERE category = %s AND direction = 'debit'
            AND tx_date >= %s AND tx_date <= %s
            AND is_transfer = 0 AND is_duplicate = 0
        ORDER BY tx_date ASC
    """, conn, params=(category, str(start_date), str(end_date)))

    if df.empty or len(df) < 2:
        return {'has_data': False}

    df['tx_date'] = pd.to_datetime(df['tx_date'])
    df = df.sort_values('tx_date')

    # Compute inter-payment gaps
    gaps = df['tx_date'].diff().dt.days.dropna()
    # Filter out same-day duplicates (gap=0)
    gaps = gaps[gaps > 0]

    if gaps.empty:
        return {'has_data': False}

    avg_gap = float(gaps.mean())
    last_date = df['tx_date'].iloc[-1].date()
    last_amount = float(df['amount'].iloc[-1])
    avg_amount = float(df['amount'].mean())
    monthly_total = float(df['amount'].sum()) / max(lookback_months, 1)

    # Classify frequency
    if avg_gap <= 5:
        frequency = 'daily'
    elif avg_gap <= 10:
        frequency = 'weekly'
    elif avg_gap <= 18:
        frequency = 'biweekly'
    elif avg_gap <= 45:
        frequency = 'monthly'
    else:
        frequency = 'quarterly'

    # Detect common payment days-of-month
    payment_days = sorted(df['tx_date'].dt.day.value_counts().head(3).index.tolist())

    return {
        'has_data': True,
        'frequency': frequency,
        'avg_gap_days': avg_gap,
        'last_payment_date': last_date,
        'last_payment_amount': last_amount,
        'avg_amount': avg_amount,
        'monthly_total': monthly_total,
        'payment_days': payment_days,
    }


def _project_next_payments(
    schedule: dict,
    week_start: date,
    week_end: date,
) -> float:
    """Project how much hits in a specific week based on schedule detection.

    Walks forward from last known payment using detected gap to predict
    which future payment dates fall within the target week.
    """
    if not schedule.get('has_data'):
        return 0.0

    last_date = schedule['last_payment_date']
    avg_gap = schedule['avg_gap_days']
    avg_amount = schedule['avg_amount']

    if avg_gap <= 0:
        return 0.0

    # Walk forward from last payment date, finding payments in target week
    total = 0.0
    next_payment = last_date + timedelta(days=int(round(avg_gap)))

    # Safety: walk up to 400 days into the future
    max_date = last_date + timedelta(days=400)
    while next_payment <= min(week_end, max_date):
        if next_payment >= week_start:
            total += avg_amount
        next_payment += timedelta(days=int(round(avg_gap)))

    return total


def _project_expense_week(
    conn: ConnectionWrapper,
    category: str,
    week_start: date,
    week_end: date,
    ctx: dict,
) -> float:
    """Project expense for a single future week.

    Priority order:
    1. COGS (revenue_pct) — always a % of gross revenue
    2. Method-based timing (media_plan, biweekly_schedule, quarterly_detect)
    3. Schedule detection from bank actuals (trailing_avg, schedule methods)
    4. Final fallback to trailing average or seed defaults
    """
    method = CASHFLOW_CATEGORIES.get(category, {}).get('method', 'trailing_avg')

    # COGS is always a % of GROSS revenue, no schedule detection needed.
    # Must gross-up payout amounts to recover pre-fee revenue.
    if method == 'revenue_pct':
        cogs_pct = ctx.get('cogs_pct', 0.25)
        dtc_payout = _project_revenue_week(conn, 'dtc_revenue', week_start, week_end, ctx)
        amz_payout = _project_revenue_week(conn, 'amazon_revenue', week_start, week_end, ctx)
        dtc_ratio = ctx.get('dtc_payout_ratio', 0.94)
        amz_ratio = ctx.get('amazon_payout_ratio', 0.62)
        # Gross up: payout / ratio = gross revenue
        dtc_gross = dtc_payout / dtc_ratio if dtc_ratio > 0 else dtc_payout
        amz_gross = amz_payout / amz_ratio if amz_ratio > 0 else amz_payout
        return (dtc_gross + amz_gross) * cogs_pct

    # --- Method-based projection FIRST for categories with explicit timing ---
    # These methods encode business-specific timing (biweekly payroll, monthly
    # media bills, quarterly tax) and must NOT be overridden by schedule
    # detection from trailing bank data averages.

    if method == 'media_plan':
        month_key = week_start.strftime('%Y-%m')
        monthly_media = ctx.get('monthly_media_spend', {})
        monthly = monthly_media.get(month_key, CASHFLOW_SEED_DEFAULTS.get(category, 0) * 4.33)
        # Media typically bills monthly near end of month
        if week_end.day >= 25 or week_start.day >= 25:
            return monthly
        return 0.0

    elif method == 'biweekly_schedule':
        last_amount = ctx.get('last_payroll_amount', CASHFLOW_SEED_DEFAULTS.get('payroll', 8000))
        # Payroll hits ~10th and ~25th of month
        for d_offset in range(7):
            d = week_start + timedelta(days=d_offset)
            if d <= week_end and d.day in (9, 10, 11, 24, 25, 26):
                return last_amount
        return 0.0

    elif method == 'quarterly_detect':
        avg = compute_trailing_avg(conn, category, lookback_weeks=13)
        month = week_start.month
        # Quarterly tax payments in Jan, Apr, Jul, Oct — mid-month
        if month in (1, 4, 7, 10) and 10 <= week_start.day <= 20:
            return avg * 13  # full quarter in one week
        return 0.0

    # --- Fulfillment: scale with DTC revenue volume ---
    # Fulfillment costs (3PL fees) scale with order volume. Instead of using
    # a flat trailing average, calculate the historical fulfillment-to-DTC ratio
    # and apply it to projected DTC revenue.
    if category == 'fulfillment':
        trailing_fulfillment = compute_trailing_avg(conn, 'fulfillment', lookback_weeks=8)
        trailing_dtc = compute_trailing_avg(conn, 'dtc_revenue', lookback_weeks=8)
        if trailing_dtc > 0:
            fulfill_ratio = trailing_fulfillment / trailing_dtc
            projected_dtc = _project_revenue_week(conn, 'dtc_revenue', week_start, week_end, ctx)
            return projected_dtc * fulfill_ratio
        return trailing_fulfillment if trailing_fulfillment > 0 else CASHFLOW_SEED_DEFAULTS.get(category, 0)

    # --- Schedule detection for trailing_avg and schedule methods ---
    # Only use pre-detected schedule from bank actuals for categories that
    # don't have an explicit timing method above.
    sched_key = f'_schedule_{category}'
    schedule = ctx.get(sched_key)

    if schedule and schedule.get('has_data'):
        projected = _project_next_payments(schedule, week_start, week_end)
        if projected > 0:
            return projected

        # For weekly/daily frequency, use monthly average / 4.33
        freq = schedule.get('frequency', '')
        if freq in ('daily', 'weekly'):
            return schedule['monthly_total'] / 4.33

        # No payment this week for biweekly/monthly/quarterly — return 0
        # This is intentional: payments show when they hit
        if freq in ('biweekly', 'monthly', 'quarterly'):
            return 0.0

    # --- Final fallback ---
    if method == 'schedule':
        avg = compute_trailing_avg(conn, category, lookback_weeks=8)
        seed = CASHFLOW_SEED_DEFAULTS.get(category, 0)
        val = avg if avg > 0 else seed
        # Loan payments typically monthly near end of month
        if week_end.day >= 25 or week_start.day >= 25:
            return val * 4.33  # monthly lump
        return 0.0

    else:
        # trailing_avg (default) — spread evenly
        avg = compute_trailing_avg(conn, category, lookback_weeks=8)
        return avg if avg > 0 else CASHFLOW_SEED_DEFAULTS.get(category, 0)


def _apply_scenario(amount: float, category_group: str, scenario: str) -> float:
    """Adjust amount based on scenario."""
    if scenario == 'base':
        return amount
    elif scenario == 'conservative':
        if category_group == 'revenue':
            return amount * 0.85  # -15%
        elif category_group == 'expense':
            return amount * 1.10  # +10%
    elif scenario == 'aggressive':
        if category_group == 'revenue':
            return amount * 1.10  # +10%
        elif category_group == 'expense':
            return amount * 0.95  # -5%
    return amount


def _compute_confidence(week_index: int, net_flow: float) -> tuple:
    """Compute confidence interval for a projected week."""
    pct = min(week_index * CASHFLOW_CONFIDENCE_WEEKLY_GROWTH, CASHFLOW_CONFIDENCE_MAX)
    if week_index <= 4:
        pct = pct * 0.5  # tighter in near term
    magnitude = abs(net_flow) * pct if net_flow != 0 else 1000
    return (net_flow - magnitude, net_flow + magnitude)


def build_cashflow_forecast(
    conn: ConnectionWrapper,
    start_date: date = None,
    weeks: int = 52,
    scenario: str = 'base',
) -> pd.DataFrame:
    """Build a 52-week cash flow forecast.

    Returns weekly DataFrame with columns for each category plus totals,
    opening/closing balance, confidence intervals, and actuals flag.

    CFO note: This is the core model. Revenue comes from waterfall forecasts
    and Amazon projections. Expenses come from categorized bank actuals with
    trailing averages. Everything auto-calibrates from real data -- the seed
    defaults are just bootstrapping values for the first run.
    """
    if start_date is None:
        start_date = date.today()

    week_list = _get_week_boundaries(start_date, weeks)
    today = date.today()

    # --- Build context dict with all the data the projectors need ---
    ctx = {}

    # DTC payout ratio (auto-calibrated or seed)
    from db import get_cashflow_setting
    dtc_ratio = compute_payout_ratio(conn, 'dtc')
    if dtc_ratio is None:
        dtc_ratio = float(get_cashflow_setting(conn, 'dtc_payout_ratio', '0.94'))
    ctx['dtc_payout_ratio'] = dtc_ratio

    # Amazon payout ratio
    amz_ratio = compute_payout_ratio(conn, 'amazon')
    if amz_ratio is None:
        amz_ratio = float(get_cashflow_setting(conn, 'amazon_payout_ratio', '0.62'))
    ctx['amazon_payout_ratio'] = amz_ratio

    # COGS percentage
    ctx['cogs_pct'] = float(get_cashflow_setting(conn, 'cogs_pct', '0.25'))

    # DTC monthly revenue from waterfall
    try:
        from db import get_media_spend, get_amazon_revenue_forecast
        media_plan = get_media_spend(conn, source='All Sources')

        # Build waterfall for DTC monthly revenue
        from analytics.waterfall import build_waterfall
        seasonal_df = read_sql('SELECT month_num, index_value FROM seasonal_indices', conn)
        seasonal_dict = dict(zip(seasonal_df['month_num'], seasonal_df['index_value'])) if not seasonal_df.empty else None
        wf = build_waterfall(media_plan, source_filter='shopify', horizon_months=12, seasonal_indices=seasonal_dict)
        if wf is not None and not wf.empty and 'month' in wf.columns:
            rev_col = None
            for col in ['total_revenue', 'revenue', 'total_units_revenue']:
                if col in wf.columns:
                    rev_col = col
                    break
            if rev_col:
                ctx['dtc_monthly_revenue'] = dict(zip(wf['month'], wf[rev_col]))
            else:
                ctx['dtc_monthly_revenue'] = {}
        else:
            ctx['dtc_monthly_revenue'] = {}
    except Exception as exc:
        log.warning('Could not build waterfall for cash flow: %s', exc)
        ctx['dtc_monthly_revenue'] = {}

    # Amazon monthly revenue from forecast table
    try:
        amz_forecast = get_amazon_revenue_forecast(conn)
        ctx['amazon_monthly_revenue'] = {r['month']: r['revenue'] for r in amz_forecast}
    except Exception as e:
        log.error('Failed to load Amazon revenue forecast: %s', e)
        ctx['amazon_monthly_revenue'] = {}

    # Media spend plan (monthly)
    try:
        ctx['monthly_media_spend'] = {r['month']: r['spend'] for r in media_plan}
    except Exception as e:
        log.error('Failed to load media spend plan: %s', e)
        ctx['monthly_media_spend'] = {}

    # Payroll detection
    try:
        last_payroll = conn.execute("""
            SELECT amount FROM cashflow_transactions
            WHERE category = 'payroll' AND direction = 'debit'
            ORDER BY tx_date DESC LIMIT 1
        """).fetchone()
        ctx['last_payroll_amount'] = float(last_payroll['amount']) if last_payroll else 8000
    except Exception as e:
        log.warning('Payroll schedule detection failed: %s', e)
        ctx['last_payroll_amount'] = 8000

    # --- Pre-compute Amazon disbursement schedule (once, not per-week) ---
    ctx['_amazon_disbursements'] = _build_amazon_disbursement_schedule(week_list)

    # --- Pre-detect expense schedules (once, not per-week) ---
    expense_cats_list = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'expense']
    for cat in expense_cats_list:
        sched_key = f'_schedule_{cat}'
        if sched_key not in ctx:
            try:
                ctx[sched_key] = _detect_expense_schedule(conn, cat)
            except Exception as e:
                log.warning('Expense schedule detection failed for %s: %s', cat, e)
                ctx[sched_key] = {'has_data': False}

    # --- Get opening balance (sum latest balance across bank accounts) ---
    try:
        # Get all distinct accounts, then find the latest balance for each.
        # Exclude negative balances (credit card liabilities).
        accounts = conn.execute(
            "SELECT DISTINCT account FROM cashflow_transactions WHERE balance_after IS NOT NULL"
        ).fetchall()
        opening_balance = 0.0
        for acct_row in (accounts or []):
            acct = acct_row['account']
            latest = conn.execute("""
                SELECT balance_after FROM cashflow_transactions
                WHERE account = %s AND balance_after IS NOT NULL
                ORDER BY tx_date DESC, created_at DESC LIMIT 1
            """, (acct,)).fetchone()
            if latest and float(latest['balance_after']) > 0:
                opening_balance += float(latest['balance_after'])
        if opening_balance <= 0:
            opening_balance = 153000  # seed from Cash Flow Model
    except Exception as e:
        log.error('Failed to compute opening balance, using seed: %s', e)
        opening_balance = 153000  # seed from Cash Flow Model

    # --- Reconstruct historical opening balance ---
    # The bank balance above is as-of today, but row 0 starts at week_list[0]
    # (typically 4 weeks ago).  Subtract the net of actual transactions from
    # start_date to today so the opening balance represents what it was at
    # start_date — not today.  Without this, the 4 weeks of actuals that have
    # already been reflected in the current bank balance get replayed, causing
    # double-counting (~$110K overstatement).
    try:
        model_start = str(week_list[0][0])
        net_since_start = read_sql("""
            SELECT COALESCE(SUM(CASE WHEN direction='credit' THEN amount
                                     ELSE -amount END), 0) as net
            FROM cashflow_transactions
            WHERE tx_date >= %s AND is_transfer = 0 AND is_duplicate = 0
        """, conn, params=(model_start,))
        if not net_since_start.empty:
            opening_balance -= float(net_since_start.iloc[0]['net'])
    except Exception:
        log.warning('Could not reconstruct historical opening balance')

    # --- Revenue and expense categories ---
    revenue_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'revenue']
    expense_cats = [k for k, v in CASHFLOW_CATEGORIES.items() if v['group'] == 'expense']

    # --- Pre-fetch actuals for all categories ---
    actuals_cache = {}
    for cat in revenue_cats + expense_cats:
        actuals_cache[cat] = _get_actual_weekly_totals(conn, cat, week_list)

    # --- Fetch overrides ---
    overrides = {}
    try:
        override_rows = read_sql(
            "SELECT line_item, week_start, override_amount FROM cashflow_overrides",
            conn,
        )
        for _, row in override_rows.iterrows():
            key = (row['line_item'], row['week_start'])
            overrides[key] = float(row['override_amount'])
    except Exception as e:
        log.warning('Failed to load cashflow overrides: %s', e)

    # --- Build weekly rows ---
    rows = []
    balance = opening_balance

    for i, (ws, we) in enumerate(week_list):
        ws_str = str(ws)
        is_past = we < today
        is_current = ws <= today <= we
        is_actual = is_past  # current week is blended

        row = {
            'week_start': ws_str,
            'week_end': str(we),
            'week_num': i + 1,
            'is_actual': is_actual,
            'opening_balance': balance,
        }

        # Revenue
        total_inflows = 0
        for cat in revenue_cats:
            override_key = (cat, ws_str)
            if not is_past and override_key in overrides:
                val = overrides[override_key]
            elif is_past:
                val = actuals_cache.get(cat, {}).get(ws_str, 0)
            elif is_current:
                # Blended: actual-to-date + projected remainder
                actual = actuals_cache.get(cat, {}).get(ws_str, 0)
                days_elapsed = (today - ws).days + 1
                days_total = 7
                if days_elapsed < days_total:
                    projected_full = _project_revenue_week(conn, cat, ws, we, ctx)
                    projected_full = _apply_scenario(projected_full, 'revenue', scenario)
                    if cat == 'dtc_revenue':
                        # DOW-weighted proration: DTC is Tuesday-heavy
                        remaining_weight = 0.0
                        for d_off in range(days_total):
                            d = ws + timedelta(days=d_off)
                            if d > today and d <= we:
                                remaining_weight += DTC_DOW_WEIGHTS.get(d.weekday(), 0)
                        total_weight = sum(DTC_DOW_WEIGHTS.values()) or 1.0
                        val = actual + projected_full * (remaining_weight / total_weight)
                    else:
                        val = actual + projected_full * (days_total - days_elapsed) / days_total
                else:
                    val = actual
            else:
                val = _project_revenue_week(conn, cat, ws, we, ctx)
                val = _apply_scenario(val, 'revenue', scenario)

            row[cat] = round(val, 2)
            total_inflows += val

        row['total_inflows'] = round(total_inflows, 2)

        # Expenses
        total_outflows = 0
        for cat in expense_cats:
            override_key = (cat, ws_str)
            if not is_past and override_key in overrides:
                val = overrides[override_key]
            elif is_past:
                val = actuals_cache.get(cat, {}).get(ws_str, 0)
            elif is_current:
                actual = actuals_cache.get(cat, {}).get(ws_str, 0)
                days_elapsed = (today - ws).days + 1
                days_total = 7
                if days_elapsed < days_total:
                    projected_full = _project_expense_week(conn, cat, ws, we, ctx)
                    # Skip expense scenario multiplier for revenue_pct categories
                    # (e.g. production/COGS) — they already inherit the revenue
                    # scenario adjustment through their revenue inputs.
                    cat_method = CASHFLOW_CATEGORIES.get(cat, {}).get('method', '')
                    if cat_method != 'revenue_pct':
                        projected_full = _apply_scenario(projected_full, 'expense', scenario)
                    val = actual + projected_full * (days_total - days_elapsed) / days_total
                else:
                    val = actual
            else:
                val = _project_expense_week(conn, cat, ws, we, ctx)
                # Skip expense scenario multiplier for revenue_pct categories
                cat_method = CASHFLOW_CATEGORIES.get(cat, {}).get('method', '')
                if cat_method != 'revenue_pct':
                    val = _apply_scenario(val, 'expense', scenario)

            row[cat] = round(val, 2)
            total_outflows += val

        row['total_outflows'] = round(total_outflows, 2)

        # Net and balance
        net = total_inflows - total_outflows
        row['net_cashflow'] = round(net, 2)
        row['closing_balance'] = round(balance + net, 2)

        # Confidence interval (only for future weeks)
        if is_actual:
            row['confidence_lower'] = row['closing_balance']
            row['confidence_upper'] = row['closing_balance']
        else:
            weeks_out = max(i - sum(1 for ws2, we2 in week_list[:i] if we2 < today), 1)
            lower, upper = _compute_confidence(weeks_out, net)
            row['confidence_lower'] = round(balance + lower, 2)
            row['confidence_upper'] = round(balance + upper, 2)

        balance = row['closing_balance']
        rows.append(row)

    df = pd.DataFrame(rows)
    log.info(
        'Cash flow forecast built: %d weeks, scenario=%s, opening=$%,.0f, closing=$%,.0f',
        weeks, scenario, opening_balance, balance,
    )
    return df


def get_cashflow_kpis(conn: ConnectionWrapper, forecast_df: pd.DataFrame) -> dict:
    """Extract key KPIs from a cash flow forecast DataFrame.

    Returns dict with: current_cash, projected_13w, projected_52w,
    monthly_burn, runway_weeks, min_cash_threshold, alert_week.
    """
    from db import get_cashflow_setting

    min_threshold = float(get_cashflow_setting(conn, 'min_cash_threshold', '100000'))

    today = date.today()
    today_str = str(today)

    # Find the current week's row (not row 0, which is 4 weeks ago)
    current_idx = 0
    for idx, row in forecast_df.iterrows():
        if row['week_start'] <= today_str and row['week_end'] >= today_str:
            current_idx = idx
            break
    else:
        # Fallback: last actual row
        actuals = forecast_df[forecast_df['is_actual'] == True]  # noqa: E712
        if not actuals.empty:
            current_idx = actuals.index[-1]

    current_cash = forecast_df.loc[current_idx, 'opening_balance'] if not forecast_df.empty else 0

    # 13-week projected (relative to current week)
    target_13w = current_idx + 13
    if target_13w < len(forecast_df):
        projected_13w = forecast_df.iloc[target_13w]['closing_balance']
    else:
        projected_13w = forecast_df.iloc[-1]['closing_balance'] if not forecast_df.empty else 0

    # 52-week projected
    projected_52w = forecast_df.iloc[-1]['closing_balance'] if not forecast_df.empty else 0

    # Monthly burn (trailing actuals only — minimum 2 weeks)
    recent = forecast_df[forecast_df['is_actual'] == True]  # noqa: E712
    if len(recent) >= 2:
        monthly_burn = -recent.tail(min(4, len(recent)))['net_cashflow'].mean() * 4.33
    else:
        # Not enough actuals — use projected as fallback
        monthly_burn = -forecast_df.head(4)['net_cashflow'].mean() * 4.33

    # Runway
    weekly_burn = monthly_burn / 4.33 if monthly_burn > 0 else 0
    if weekly_burn > 0:
        runway_weeks = int(current_cash / weekly_burn)
        runway_weeks = min(runway_weeks, 52)
    else:
        runway_weeks = 52

    # Alert: first week below threshold
    alert_week = None
    for _, row in forecast_df.iterrows():
        if row['closing_balance'] < min_threshold:
            alert_week = row['week_num']
            break

    # Data freshness
    balance_freshness = None
    try:
        latest_tx = conn.execute(
            "SELECT MAX(tx_date) as latest FROM cashflow_transactions"
        ).fetchone()
        if latest_tx and latest_tx['latest']:
            balance_freshness = str(latest_tx['latest'])
    except Exception as e:
        log.warning('Balance freshness query failed: %s', e)

    return {
        'current_cash': current_cash,
        'projected_13w': projected_13w,
        'projected_52w': projected_52w,
        'monthly_burn': monthly_burn,
        'runway_weeks': runway_weeks,
        'min_cash_threshold': min_threshold,
        'alert_week': alert_week,
        'balance_freshness_date': balance_freshness,
    }
