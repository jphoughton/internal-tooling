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
