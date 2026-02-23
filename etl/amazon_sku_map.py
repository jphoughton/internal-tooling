"""
Amazon ASIN → Master SKU mapping.

Amazon Seller Central uses its own seller-SKU codes that don't match
the canonical/master SKU system used by Shopify and Packiyo.
This module maps Amazon ASINs to the correct master SKUs so all
inventory and sales data is unified across channels.

The ASIN is the stable identifier — Amazon seller-SKUs can vary
(e.g. different FBA shipments may create different seller-SKU codes
for the same product). The ASIN is always consistent.
"""

# ASIN → Master SKU mapping (hand-verified from Seller Central)
# Master SKU is derived from the primary Amazon seller-SKU pattern.
ASIN_TO_MASTER_SKU = {
    # ── Bulk Powders (BP) ──────────────────────────────────────────
    "B0DP1BCX7T": "ENBP-BP0030-LEB0",          # Energy Bulk Lemon 30srv
    "B0DK61KKNP": "HYBP-BP0030-CHLB0",         # Hydrate Bulk Chili Lime 30srv
    "B0CVR3BV2F": "HYBP-BP0030-GFB0",          # Hydrate Bulk Grapefruit 30srv
    "B0DH93VY5M": "HYBP-BP0030-ICLEB0",        # Hydrate Bulk Iced Tea Lemonade 30srv
    "B0CVR251NH": "HYBP-BP0050-NAB0",           # Hydrate Bulk Unflavored 50srv
    "B0DPJGNNW7": "SLBP-BP0030-CHB0",          # Sleep Bulk Tart Cherry 30srv
    "B0DK635GT5": "HYBP-BP0050-FSB0",          # Hydrate Bulk Fasting 50srv Sugar-Free

    # ── Stick Packs – Hydrate (30ct) ──────────────────────────────
    "B07SB6DRRM": "HYPO-ST0030-BOB0",          # Hydrate Blood Orange 30pk
    "B08XMTTJRH": "HYPO-ST0030-FRPUB0",        # Hydrate Fruit Punch 30pk
    "B097987PYH": "HYPO-ST0030-LELMB0",         # Hydrate Lemon Lime 30pk
    "B07S64FKYS": "HYPO-ST0030-VPBFLB0",       # Hydrate Variety Pack 30pk

    # ── Stick Packs – Energy (30ct) ───────────────────────────────
    "B08GP68YFT": "ENPO-ST0030-RSLEB0",        # Energy Raspberry Lemonade 30pk

    # ── Stick Packs – Immunity (30ct) ─────────────────────────────
    "B08W8MG5YR": "IMPO-ST0030-ELB0",          # Immunity Elderberry 30pk

    # ── Stick Packs – Sleep / Nighttime (30ct) ────────────────────
    "B08N488X3T": "NSPO-ST0030-LEB0",          # Sleep Lemon 30pk
    "B0D4ZKWSGK": "NSPO-ST0030-VPWLBB0",       # Sleep Variety Pack 30pk
    "B08W9GF9HD": "NSPO-ST0030-WALEB0",        # Sleep Watermelon 30pk
    "B08VFBBX9S": "SLPO-ST0030-ELB0",          # Sleep Elderberry 30pk
    "B0D4ZJV28Y": "NSPO-ST0030-BEB0",          # Sleep Berry 30pk

    # ── Canister / Caddy (8ct / 12ct) ─────────────────────────────
    "B0D828YZDP": "HYCD-ST0008-BOB0",          # Hydrate Blood Orange 8pk caddy
    "B08GV3TL58": "HYCD-ST0012-BOB0",          # Hydrate Blood Orange 12pk caddy
    "B08GV4SH4N": "ENCD-ST0012-RSLEB0",        # Energy Raspberry Lemonade 12pk caddy

    # ── Canister / Caddy – Sugar Free (8ct) ──────────────────────
    "B0D82HNFH5": "NSCD-ST0008-LEB0",          # NS Hydrate Lemonade 8pk Sugar-Free caddy
    "B0FCNN52CC": "NSCD-ST0008-BEB0",           # NS Hydrate Berry Burst 8pk Sugar-Free caddy
}

# Reverse lookup: Master SKU → ASIN
MASTER_SKU_TO_ASIN = {v: k for k, v in ASIN_TO_MASTER_SKU.items()}

# Amazon Seller SKU → ASIN mapping (observed from inventory API)
# These are the messy seller-SKU codes Amazon assigns for FBA shipments.
# We map them through ASIN since that's the stable identifier.
# This is populated dynamically from inventory data, but we seed known ones.
#
# Seeding is important because the bulk FBA Inventory API sometimes returns
# only ~50 items without pagination. For missing items, we do targeted
# queries by seller-SKU, so we need to know the seller-SKU upfront.
_SELLER_SKU_TO_ASIN = {
    # ── Bulk Powders ──────────────────────────────────────────────
    "5G-WLX0-V5VZ": "B0DP1BCX7T",                # Energy Bulk Lemon
    "EK-8L4Z-OLVW": "B0CVR3BV2F",                # Hydrate Bulk Grapefruit
    "BK-F74E-7EXG": "B0DH93VY5M",                # Hydrate Bulk Iced Tea Lemonade
    "30-YAMH-0XAB": "B0CVR251NH",                 # Hydrate Bulk Unflavored 50srv
    "AG-VMJN-BXWH": "B0DPJGNNW7",                # Sleep Bulk Tart Cherry
    "27-PU5D-X5J3": "B0DK635GT5",                # Hydrate Bulk Fasting 50srv

    # ── Stick Packs – Hydrate ─────────────────────────────────────
    "HYDT-DRPOSP-ST0030-FRPU0": "B08XMTTJRH",   # Fruit Punch 30pk
    "61-1KA0-RX1O": "B07S64FKYS",                # Variety Pack 30pk
    "89-XCSU-1O7X": "B07S64FKYS",                # Variety Pack 30pk (alt SKU)

    # ── Stick Packs – Energy ──────────────────────────────────────
    "ENPO-ST0030-RSLE2": "B08GP68YFT",           # Energy Raspberry Lemonade 30pk
    "HYDT-DRPCSP-ST0030-RSLE0": "B08GP68YFT",    # Energy Raspberry Lemonade (alt)
    "HYDT-DRPCSP-ST0030-RSLE1": "B08GP68YFT",    # Energy Raspberry Lemonade (alt)
    "HYDT-DRPCSP-ST0030-RSLE1-V3": "B08GP68YFT", # Energy Raspberry Lemonade (alt)

    # ── Stick Packs – Immunity ────────────────────────────────────
    "HYDT-DRPISP-ST0030-ELDE0-FBA": "B08W8MG5YR",  # Immunity Elderberry 30pk

    # ── Canister / Caddy ──────────────────────────────────────────
    "HYCD-ST0008-BOB0": "B0D828YZDP",            # Blood Orange 8pk caddy
    "HYCD-ST0012-BO2": "B08GV3TL58",             # Blood Orange 12pk caddy
    "HYCD-ST0012-BOB0": "B08GV3TL58",            # Blood Orange 12pk caddy (alt)
    "ENCD-ST0012-RSLE2": "B08GV4SH4N",           # Energy Raspberry Lemonade 12pk
    "ENCD-ST0012-RSLEB0": "B08GV4SH4N",          # Energy Raspberry Lemonade 12pk (alt)

    # ── Sugar-Free Caddy (8ct) ────────────────────────────────────
    "NSCD-ST0008-LEB0": "B0D82HNFH5",            # NS Lemonade 8pk SF caddy
    "NSCD-ST0008-BEB0": "B0FCNN52CC",             # NS Berry Burst 8pk SF caddy
}


def register_seller_sku(seller_sku, asin):
    """Register a seller-SKU → ASIN mapping (learned from API responses)."""
    if seller_sku and asin:
        _SELLER_SKU_TO_ASIN[seller_sku] = asin


def get_master_sku(seller_sku=None, asin=None):
    """
    Resolve an Amazon seller-SKU or ASIN to the canonical master SKU.

    Priority:
    1. Direct ASIN lookup (most reliable)
    2. Seller-SKU → ASIN → Master SKU
    3. If seller-SKU already IS a master SKU, return it
    4. None if unmapped

    Args:
        seller_sku: The Amazon seller-SKU (e.g. "27-PU5D-X5J3")
        asin: The Amazon ASIN (e.g. "B0CVR3BV2F")

    Returns:
        str: Master SKU (e.g. "HYBP-BP0030-GFB0") or None
    """
    # 1. Direct ASIN lookup
    if asin and asin in ASIN_TO_MASTER_SKU:
        return ASIN_TO_MASTER_SKU[asin]

    # 2. Seller-SKU → ASIN → Master
    if seller_sku:
        resolved_asin = _SELLER_SKU_TO_ASIN.get(seller_sku)
        if resolved_asin and resolved_asin in ASIN_TO_MASTER_SKU:
            return ASIN_TO_MASTER_SKU[resolved_asin]

    # 3. Check if seller_sku is already a master SKU
    if seller_sku and seller_sku in MASTER_SKU_TO_ASIN:
        return seller_sku

    return None


def map_amazon_sku(seller_sku, asin=None, product_name=None):
    """
    Map an Amazon SKU to master SKU, with registration side-effect.

    If both seller_sku and asin are provided, registers the mapping
    for future lookups. Always returns the best available master SKU.

    Args:
        seller_sku: Amazon's seller-SKU
        asin: Amazon ASIN (optional but highly recommended)
        product_name: Product name (for logging unmapped SKUs)

    Returns:
        str: Master SKU, or original seller_sku if no mapping found
    """
    # Register the seller-SKU → ASIN mapping
    if seller_sku and asin:
        register_seller_sku(seller_sku, asin)

    master = get_master_sku(seller_sku=seller_sku, asin=asin)
    if master:
        return master

    # Fallback: return original seller_sku (will show as unmapped in dashboard)
    return seller_sku
