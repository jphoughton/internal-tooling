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
    r'\b[A-Z0-9]{8,}\b',           # long alphanumeric IDs
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
    except Exception:
        pass

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

    updated = 0
    for mapping in mappings:
        pattern = mapping['match_pattern']
        # Find transactions whose normalized summary matches this pattern
        txs = read_sql(
            "SELECT tx_id, summary FROM cashflow_transactions",
            conn,
        )
        if txs.empty:
            continue

        for _, tx in txs.iterrows():
            norm = normalize_summary(tx['summary'] or '')
            if norm == pattern:
                conn.execute(
                    "UPDATE cashflow_transactions SET category = %s, subcategory = %s, "
                    "is_transfer = %s, is_duplicate = %s WHERE tx_id = %s",
                    (mapping['category'], mapping['subcategory'],
                     mapping['is_transfer'], mapping['is_duplicate'], tx['tx_id']),
                )
                updated += 1

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
    """Get actual transaction totals for each week, keyed by week_start string."""
    if not weeks:
        return {}
    first_start = str(weeks[0][0])
    last_end = str(weeks[-1][1])

    df = read_sql("""
        SELECT tx_date, SUM(amount) as total
        FROM cashflow_transactions
        WHERE category = %s
            AND tx_date >= %s AND tx_date <= %s
            AND is_transfer = 0 AND is_duplicate = 0
        GROUP BY tx_date
    """, conn, params=(category, first_start, last_end))

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


def _is_amazon_disbursement_week(week_start: date, week_end: date) -> dict:
    """Detect Amazon disbursement events within a week.

    Amazon disburses biweekly, typically landing around the 7th-10th and
    23rd-25th of each month.  A single week can span two calendar months,
    so we check every day in the range and return a dict of
    {month_key: count_of_disbursement_events} (0, 1, or rarely 2).
    """
    EARLY_WINDOW = range(7, 11)   # days 7, 8, 9, 10
    LATE_WINDOW = range(23, 26)   # days 23, 24, 25

    disbursements = {}  # {YYYY-MM: number of disbursement events}
    # Track which (month, window) pairs we have already counted so a
    # multi-day window landing entirely inside one week only counts once.
    seen = set()

    d = week_start
    while d <= week_end:
        month_key = d.strftime('%Y-%m')
        for label, window in (('early', EARLY_WINDOW), ('late', LATE_WINDOW)):
            if d.day in window and (month_key, label) not in seen:
                seen.add((month_key, label))
                disbursements[month_key] = disbursements.get(month_key, 0) + 1
        d += timedelta(days=1)

    return disbursements


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
        # Amazon disburses biweekly (~7th-10th and ~23rd-25th of each
        # month).  Non-disbursement weeks should show $0 so the cash
        # flow table reflects actual deposit timing.
        monthly_rev = ctx.get('amazon_monthly_revenue', {})
        ratio = ctx.get('amazon_payout_ratio', 0.62)

        disbursements = _is_amazon_disbursement_week(week_start, week_end)
        if not disbursements:
            return 0.0

        # Each disbursement event = half the month's payout
        total = 0.0
        for month_key, count in disbursements.items():
            monthly = monthly_rev.get(month_key, 0)
            total += (monthly * ratio / 2) * count
        return total

    else:
        # Trailing average for other revenue categories
        avg = compute_trailing_avg(conn, category, lookback_weeks=8)
        return avg


def _project_expense_week(
    conn: ConnectionWrapper,
    category: str,
    week_start: date,
    week_end: date,
    ctx: dict,
) -> float:
    """Project expense for a single future week."""
    method = CASHFLOW_CATEGORIES.get(category, {}).get('method', 'trailing_avg')

    if method == 'media_plan':
        # Use media spend plan from DB
        month_key = week_start.strftime('%Y-%m')
        monthly_media = ctx.get('monthly_media_spend', {})
        monthly = monthly_media.get(month_key, CASHFLOW_SEED_DEFAULTS.get(category, 0) * 4.33)
        return monthly / 4.33

    elif method == 'biweekly_schedule':
        # Payroll: detect last amount and schedule
        last_amount = ctx.get('last_payroll_amount', CASHFLOW_SEED_DEFAULTS.get('payroll', 8000))
        # Biweekly = every other week averages to half per week
        return last_amount

    elif method == 'revenue_pct':
        # COGS: percentage of total revenue
        cogs_pct = ctx.get('cogs_pct', 0.25)
        total_rev = sum(
            _project_revenue_week(conn, cat, week_start, week_end, ctx)
            for cat in ('dtc_revenue', 'amazon_revenue')
        )
        return total_rev * cogs_pct

    elif method == 'quarterly_detect':
        # Sales tax: detect from actuals, project quarterly
        avg = compute_trailing_avg(conn, category, lookback_weeks=13)
        # Quarterly payments happen in specific months
        month = week_start.month
        if month in (1, 4, 7, 10):  # tax quarter months
            return avg * 3  # lump sum in quarter month
        return avg * 0.1  # small ongoing payments

    elif method == 'schedule':
        # Loan: use trailing average from actuals
        avg = compute_trailing_avg(conn, category, lookback_weeks=8)
        return avg if avg > 0 else CASHFLOW_SEED_DEFAULTS.get(category, 0)

    else:
        # trailing_avg (default)
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
        wf = build_waterfall(media_plan, source_filter='shopify', horizon_months=12)
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
    except Exception:
        ctx['amazon_monthly_revenue'] = {}

    # Media spend plan (monthly)
    try:
        ctx['monthly_media_spend'] = {r['month']: r['spend'] for r in media_plan}
    except Exception:
        ctx['monthly_media_spend'] = {}

    # Payroll detection
    try:
        last_payroll = conn.execute("""
            SELECT amount FROM cashflow_transactions
            WHERE category = 'payroll' AND direction = 'debit'
            ORDER BY tx_date DESC LIMIT 1
        """).fetchone()
        ctx['last_payroll_amount'] = float(last_payroll['amount']) if last_payroll else 8000
    except Exception:
        ctx['last_payroll_amount'] = 8000

    # --- Get opening balance ---
    try:
        latest_balance = conn.execute("""
            SELECT balance_after FROM cashflow_transactions
            WHERE balance_after IS NOT NULL
            ORDER BY tx_date DESC, created_at DESC LIMIT 1
        """).fetchone()
        opening_balance = float(latest_balance['balance_after']) if latest_balance else 153000
    except Exception:
        opening_balance = 153000  # seed from Cash Flow Model

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
    except Exception:
        pass

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
            if override_key in overrides:
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
                    # Prorate: actual portion + projected remainder
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
            if override_key in overrides:
                val = overrides[override_key]
            elif is_past:
                val = actuals_cache.get(cat, {}).get(ws_str, 0)
            elif is_current:
                actual = actuals_cache.get(cat, {}).get(ws_str, 0)
                days_elapsed = (today - ws).days + 1
                days_total = 7
                if days_elapsed < days_total:
                    projected_full = _project_expense_week(conn, cat, ws, we, ctx)
                    projected_full = _apply_scenario(projected_full, 'expense', scenario)
                    val = actual + projected_full * (days_total - days_elapsed) / days_total
                else:
                    val = actual
            else:
                val = _project_expense_week(conn, cat, ws, we, ctx)
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

    current_cash = forecast_df.iloc[0]['opening_balance'] if not forecast_df.empty else 0

    # 13-week projected
    if len(forecast_df) >= 13:
        projected_13w = forecast_df.iloc[12]['closing_balance']
    else:
        projected_13w = forecast_df.iloc[-1]['closing_balance'] if not forecast_df.empty else 0

    # 52-week projected
    projected_52w = forecast_df.iloc[-1]['closing_balance'] if not forecast_df.empty else 0

    # Monthly burn (trailing 4 weeks average net outflow)
    recent = forecast_df[forecast_df['is_actual'] == True]  # noqa: E712
    if len(recent) >= 4:
        monthly_burn = -recent.tail(4)['net_cashflow'].mean() * 4.33
    else:
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

    return {
        'current_cash': current_cash,
        'projected_13w': projected_13w,
        'projected_52w': projected_52w,
        'monthly_burn': monthly_burn,
        'runway_weeks': runway_weeks,
        'min_cash_threshold': min_threshold,
        'alert_week': alert_week,
    }
