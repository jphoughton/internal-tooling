"""
SKU → Flavor mapping for the dashboard.

Uses a static lookup for the 17 core forecast SKUs (hand-verified),
then falls back to extracting the flavor from product names using
the variant text after the last ' / '.
"""

# Hand-verified flavor mapping for core SKUs
_CORE_FLAVORS = {
    "ENBP-BP0030-LEB0": "Lemon Squeeze",
    "HYBP-BP0030-CHLB0": "Chili Lime",
    "HYBP-BP0030-GFB0": "Grapefruit",
    "HYBP-BP0030-ICLEB0": "Iced Tea Lemonade",
    "HYBP-BP0050-NAB0": "Unflavored",
    "SLBP-BP0030-CHB0": "Cosmic Cherry",
    "ENPO-ST0030-RSLEB0": "Raspberry Lemonade",
    "HYPO-ST0030-BOB0": "Blood Orange",
    "HYPO-ST0030-FRPUB0": "Fruit Punch",
    "HYPO-ST0030-LELMB0": "Lemon Lime",
    "HYPO-ST0030-VPBFLB0": "Variety Pack",
    "IMPO-ST0030-ELB0": "Elderberry Immunity",
    "NSPO-ST0030-BEB0": "Berry Burst",
    "NSPO-ST0030-LEB0": "Lemonade",
    "NSPO-ST0030-VPWLBB0": "Zero Sugar Variety Pack",
    "NSPO-ST0030-WALEB0": "Watermelonade",
    "SLPO-ST0030-ELB0": "Elderberry Sleep",
}

# SKU suffix → flavor hints (used as fallback when no product name available)
_SUFFIX_HINTS = {
    "LEB": "Lemonade",
    "CHLB": "Chili Lime",
    "GFB": "Grapefruit",
    "ICLEB": "Iced Tea Lemonade",
    "NAB": "Unflavored",
    "CHB": "Cosmic Cherry",
    "RSLEB": "Raspberry Lemonade",
    "BOB": "Blood Orange",
    "FRPUB": "Fruit Punch",
    "LELMB": "Lemon Lime",
    "VPBFLB": "Variety Pack",
    "ELB": "Elderberry",
    "BEB": "Berry Burst",
    "VPWLBB": "Zero Sugar Variety Pack",
    "WALEB": "Watermelonade",
    "NAFB": "Unflavored",
    "MYSB": "Mystery Box",
    "ENSLB": "Energy + Sleep",
    "ENSTB": "Energy Starter",
    "HYENB": "Hydrate + Energy",
    "HYSLB": "Hydrate + Sleep",
    "HYSTB": "Hydrate Starter",
    "SLSTB": "Sleep Starter",
    "VPRAB": "Hydrate + Energy Bundle",
    "ZSVP2B": "Zero Sugar BOGO",
    "HYVIPB": "Hydrate VIP",
    "MULTI": "Multi",
    "WH1": "White",
    "CLR": "Clear",
    "WH0": "White",
    "GIFT": "Gift Card",
}

# Cleanup patterns to strip from extracted flavors
_STRIP_SUFFIXES = [
    " (Bulk Powder)", " (Bulk Pouch)", " Electrolyte",
    " Pouch", " Electrolyte Powder",
]


def _extract_flavor_from_name(product_name):
    """Extract flavor from the variant portion of a product name."""
    if not product_name:
        return ""

    # The variant is after the last ' / '
    if " / " in product_name:
        flavor = product_name.rsplit(" / ", 1)[-1].strip()
    elif " - " in product_name:
        flavor = product_name.rsplit(" - ", 1)[-1].strip()
    else:
        return ""

    # Clean up common suffixes
    for suffix in _STRIP_SUFFIXES:
        if flavor.endswith(suffix):
            flavor = flavor[: -len(suffix)].strip()

    # Strip leading "Low Sugar " or "Zero Sugar " prefix for cleaner display
    # (the SKU prefix already tells you the sugar type)
    for prefix in ["Low Sugar ", "Zero Sugar "]:
        if flavor.startswith(prefix):
            flavor = flavor[len(prefix):]

    return flavor.strip()


def _extract_flavor_from_sku(sku):
    """Extract flavor hint from SKU suffix code."""
    if not sku:
        return ""

    # The flavor code is between the last '-' and the trailing digit(s)
    # e.g. HYPO-ST0030-BOB0 → BOB
    parts = sku.split("-")
    if len(parts) >= 3:
        suffix = parts[-1]
        # Strip trailing digits
        code = suffix.rstrip("0123456789")
        if code in _SUFFIX_HINTS:
            return _SUFFIX_HINTS[code]

    return ""


def get_flavor(sku, product_name=None):
    """
    Get the display flavor for a SKU.

    Priority:
    1. Core SKU static map (hand-verified)
    2. Extract from product name variant text
    3. Decode from SKU suffix code
    4. Empty string
    """
    # 1. Static lookup
    if sku in _CORE_FLAVORS:
        return _CORE_FLAVORS[sku]

    # 2. Extract from product name
    if product_name:
        flavor = _extract_flavor_from_name(product_name)
        if flavor:
            return flavor

    # 3. SKU suffix
    flavor = _extract_flavor_from_sku(sku)
    if flavor:
        return flavor

    return ""


# ---------------------------------------------------------------------------
# SKU sales rank — used to sort all SKU displays best-seller → least-seller
# ---------------------------------------------------------------------------
import time as _time

_rank_cache = {}
_RANK_CACHE_TTL = 600  # 10 minutes


def get_sku_sales_rank(lookback_days=90):
    """
    Return {sku: rank} dict ordered by total units sold over the last
    ``lookback_days`` days.  Rank 0 = best seller.

    Results are cached for 10 minutes so every page render doesn't re-query.
    """
    cache_key = f"rank_{lookback_days}"
    now = _time.time()
    if cache_key in _rank_cache:
        val, ts = _rank_cache[cache_key]
        if now - ts < _RANK_CACHE_TTL:
            return val

    from db import get_db
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT sku, SUM(units_sold) as total
            FROM daily_sku_sales
            WHERE sale_date >= %s
            GROUP BY sku
            ORDER BY total DESC
        """, (cutoff,)).fetchall()

    rank = {r["sku"]: idx for idx, r in enumerate(rows)}
    _rank_cache[cache_key] = (rank, now)
    return rank


def sort_df_by_best_seller(df, sku_col="SKU", lookback_days=90):
    """
    Sort a DataFrame by best-seller rank.  SKUs not in the rank dict
    are placed at the end (alphabetically).
    """
    rank = get_sku_sales_rank(lookback_days)
    max_rank = len(rank)
    df = df.copy()
    df["_sales_rank"] = df[sku_col].map(lambda s: rank.get(s, max_rank))
    df = df.sort_values(["_sales_rank", sku_col]).drop(columns=["_sales_rank"]).reset_index(drop=True)
    return df
