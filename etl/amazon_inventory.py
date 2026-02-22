"""
Amazon FBA Inventory client.
Fetches FBA inventory levels using the SP-API Inventories endpoint.

All seller-SKUs are mapped to master SKUs via ASIN lookup so that
Amazon inventory aligns with Shopify/Packiyo data.
"""
import logging
import time
from sp_api.api import Inventories
from sp_api.base import Marketplaces
from etl.amazon import get_credentials, get_marketplace
from etl.amazon_sku_map import map_amazon_sku, register_seller_sku, ASIN_TO_MASTER_SKU
import config as cfg

log = logging.getLogger(__name__)


def test_connection():
    """Test the Amazon SP-API connection. Returns (ok, message)."""
    creds = get_credentials()
    if not creds.get("refresh_token"):
        return False, "No Amazon refresh token configured."
    try:
        inv_api = Inventories(credentials=creds, marketplace=get_marketplace())
        resp = inv_api.get_inventory_summary_marketplace(
            details=False,
            granularityType="Marketplace",
            granularityId=cfg.AMAZON_MARKETPLACE_ID,
        )
        if resp and resp.payload:
            return True, "Connected to Amazon SP-API."
        return False, "Empty response from Amazon."
    except Exception as e:
        return False, str(e)


def _parse_summary(item):
    """Parse a single inventory summary item into a flat dict."""
    inv_detail = item.get("inventoryDetails", {})
    reserved = inv_detail.get("reservedQuantity", {})

    # FBA inbound quantities
    inbound_working = inv_detail.get("inboundWorkingQuantity", 0) or 0
    inbound_shipped = inv_detail.get("inboundShippedQuantity", 0) or 0
    inbound_receiving = inv_detail.get("inboundReceivingQuantity", 0) or 0

    # fulfillableQuantity lives inside inventoryDetails (not top-level)
    fulfillable = inv_detail.get("fulfillableQuantity", 0) or 0
    total = item.get("totalQuantity", 0) or 0
    reserved_total = reserved.get("totalReservedQuantity", 0) or 0
    unfulfillable = inv_detail.get("unfulfillableQuantity", {})
    unfulfillable_total = unfulfillable.get("totalUnfulfillableQuantity", 0) or 0

    seller_sku = item.get("sellerSku", "")
    asin = item.get("asin", "")
    product_name = item.get("productName", "")

    # Register the seller-SKU → ASIN mapping for future use
    register_seller_sku(seller_sku, asin)

    # Map to master SKU via ASIN
    master_sku = map_amazon_sku(seller_sku, asin, product_name)

    return {
        "sku": master_sku,
        "amazon_seller_sku": seller_sku,
        "asin": asin,
        "product_name": product_name,
        "condition": item.get("condition", ""),
        "fulfillable_quantity": fulfillable,
        "inbound_working": inbound_working,
        "inbound_shipped": inbound_shipped,
        "inbound_receiving": inbound_receiving,
        "total_quantity": total,
        "reserved_quantity": reserved_total,
        "unfulfillable_quantity": unfulfillable_total,
    }


def get_inventory():
    """
    Fetch FBA inventory summaries from Amazon.

    Strategy:
    1. Fetch all inventory summaries via paginated bulk query.
    2. Check for any known ASINs (from ASIN_TO_MASTER_SKU) missing
       from the bulk response.
    3. For missing ASINs, do targeted seller-SKU queries as fallback
       (the bulk endpoint sometimes omits items).

    Returns:
        list of dicts with keys:
            sku, asin, product_name, condition,
            fulfillable_quantity, inbound_working, inbound_shipped,
            inbound_receiving, total_quantity, reserved_quantity,
            unfulfillable_quantity
    """
    creds = get_credentials()
    inv_api = Inventories(credentials=creds, marketplace=get_marketplace())

    inventory = []
    next_token = None
    page = 0

    # ── Step 1: Bulk fetch all inventory (paginated) ───────────────
    while True:
        page += 1
        kwargs = {
            "details": True,
            "granularityType": "Marketplace",
            "granularityId": cfg.AMAZON_MARKETPLACE_ID,
        }
        if next_token:
            kwargs["nextToken"] = next_token

        resp = inv_api.get_inventory_summary_marketplace(**kwargs)
        payload = resp.payload if resp else {}

        summaries = payload.get("inventorySummaries", [])
        log.info("FBA Inventory page %d: %d items", page, len(summaries))

        for item in summaries:
            parsed = _parse_summary(item)
            inventory.append(parsed)

        next_token = payload.get("nextToken")
        if not next_token:
            break

    log.info("FBA Inventory bulk fetch: %d total raw items across %d pages",
             len(inventory), page)

    # ── Step 2: Check for missing known ASINs ──────────────────────
    fetched_asins = {item["asin"] for item in inventory if item.get("asin")}
    known_asins = set(ASIN_TO_MASTER_SKU.keys())
    missing_asins = known_asins - fetched_asins

    if missing_asins:
        log.warning("FBA Inventory: %d known ASINs not in bulk response: %s",
                     len(missing_asins), missing_asins)

        # ── Step 3: Supplementary query with startDateTime ────────
        # The bulk endpoint (no startDateTime) returns max ~50 items
        # without proper pagination. Using startDateTime returns a
        # different result set that often includes items the bulk
        # endpoint misses. We use a 365-day lookback.
        _fetch_with_start_date(inv_api, inventory, fetched_asins)

        # Re-check what's still missing after the supplementary query
        fetched_asins = {item["asin"] for item in inventory if item.get("asin")}
        still_missing = known_asins - fetched_asins

        if still_missing:
            log.warning("FBA Inventory: %d ASINs still missing after "
                        "startDateTime query: %s", len(still_missing),
                        still_missing)

            # ── Step 4: Targeted seller-SKU queries as last resort ─
            _fetch_missing_by_sku(inv_api, still_missing, inventory)
    else:
        log.info("FBA Inventory: all %d known ASINs found in bulk response",
                 len(known_asins & fetched_asins))

    # Deduplicate by ASIN first, then aggregate by master SKU
    deduped = _deduplicate_by_asin(inventory)
    aggregated = _aggregate_by_master_sku(deduped)
    return aggregated


def _fetch_with_start_date(inv_api, inventory, already_fetched_asins):
    """
    Supplementary inventory fetch using startDateTime parameter.

    The bulk FBA inventory endpoint (without startDateTime) returns at
    most ~50 items and often doesn't paginate properly. When called with
    startDateTime, the API returns items that have had inventory changes
    since that date — this often catches items the bulk endpoint misses.

    Items already fetched (by ASIN) are skipped to avoid duplicates.
    """
    from datetime import datetime, timedelta

    start_dt = (datetime.utcnow() - timedelta(days=365)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
    log.info("Supplementary fetch with startDateTime=%s", start_dt)

    next_token = None
    page = 0
    new_count = 0

    while True:
        page += 1
        kwargs = {
            "details": True,
            "granularityType": "Marketplace",
            "granularityId": cfg.AMAZON_MARKETPLACE_ID,
            "startDateTime": start_dt,
        }
        if next_token:
            kwargs["nextToken"] = next_token

        try:
            resp = inv_api.get_inventory_summary_marketplace(**kwargs)
        except Exception as e:
            log.warning("startDateTime query page %d failed: %s", page, e)
            break

        payload = resp.payload if resp else {}
        summaries = payload.get("inventorySummaries", [])
        log.info("startDateTime page %d: %d items", page, len(summaries))

        for item in summaries:
            asin = item.get("asin", "")
            if asin and asin not in already_fetched_asins:
                parsed = _parse_summary(item)
                inventory.append(parsed)
                already_fetched_asins.add(asin)
                new_count += 1

        next_token = payload.get("nextToken")
        if not next_token:
            break
        time.sleep(0.5)

    log.info("startDateTime fetch: %d new items from %d pages", new_count, page)


def _fetch_missing_by_sku(inv_api, missing_asins, inventory):
    """
    Try to fetch inventory for missing ASINs by querying with sellerSkus.

    The SP-API FBA inventory endpoint supports a `sellerSkus` parameter
    that can be used to query specific items. We also try without the
    `details` param in case that's causing items to be filtered.
    """
    from etl.amazon_sku_map import MASTER_SKU_TO_ASIN, _SELLER_SKU_TO_ASIN

    # Build reverse: ASIN → known seller-SKUs
    asin_to_seller_skus = {}
    for sku, asin in _SELLER_SKU_TO_ASIN.items():
        asin_to_seller_skus.setdefault(asin, []).append(sku)

    for asin in missing_asins:
        master_sku = ASIN_TO_MASTER_SKU.get(asin, "")
        seller_skus = asin_to_seller_skus.get(asin, [])

        # Also try the master SKU as a seller SKU (sometimes they match)
        if master_sku and master_sku not in seller_skus:
            seller_skus.append(master_sku)

        log.info("Attempting targeted fetch for ASIN %s (seller SKUs: %s)",
                 asin, seller_skus)

        # Try each seller SKU individually
        found = False
        for sku in seller_skus:
            try:
                time.sleep(0.5)  # Rate limit: SP-API allows ~2 req/s for inventory
                resp = inv_api.get_inventory_summary_marketplace(
                    details=True,
                    granularityType="Marketplace",
                    granularityId=cfg.AMAZON_MARKETPLACE_ID,
                    sellerSkus=[sku],
                )
                payload = resp.payload if resp else {}
                summaries = payload.get("inventorySummaries", [])
                if summaries:
                    for item in summaries:
                        parsed = _parse_summary(item)
                        inventory.append(parsed)
                        log.info("  ✓ Found via sellerSku=%s: ASIN=%s, "
                                 "fulfillable=%d, total=%d",
                                 sku, parsed["asin"],
                                 parsed["fulfillable_quantity"],
                                 parsed["total_quantity"])
                    found = True
                    break
                else:
                    log.info("  ✗ No results for sellerSku=%s", sku)
            except Exception as e:
                log.warning("  ✗ Error querying sellerSku=%s: %s", sku, e)
                time.sleep(2)  # Extra backoff after error

        # If individual SKU queries failed, try without details param
        if not found:
            try:
                time.sleep(0.5)
                resp = inv_api.get_inventory_summary_marketplace(
                    details=False,
                    granularityType="Marketplace",
                    granularityId=cfg.AMAZON_MARKETPLACE_ID,
                    sellerSkus=seller_skus if seller_skus else None,
                )
                payload = resp.payload if resp else {}
                summaries = payload.get("inventorySummaries", [])
                for item in summaries:
                    item_asin = item.get("asin", "")
                    if item_asin == asin:
                        parsed = _parse_summary(item)
                        inventory.append(parsed)
                        log.info("  ✓ Found ASIN %s via no-details query: "
                                 "fulfillable=%d, total=%d",
                                 asin, parsed["fulfillable_quantity"],
                                 parsed["total_quantity"])
                        found = True
                        break
            except Exception as e:
                log.warning("  ✗ Error on no-details query for ASIN %s: %s",
                            asin, e)
                time.sleep(2)

        if not found:
            log.warning("  ✗ ASIN %s (%s) not found via any method. "
                        "It may not be in FBA or may be in a different "
                        "marketplace.", asin, master_sku)


def _deduplicate_by_asin(inventory):
    """
    Deduplicate inventory rows by ASIN.

    Amazon reports the SAME physical inventory under every seller-SKU
    that shares an ASIN. For example, if seller-SKUs '61-1KA0-RX1O' and
    '89-XCSU-1O7X' both have ASIN B07S64FKYS, they'll both show
    Total=2019 — but it's the same 2,019 units, not 4,038.

    We keep one row per ASIN (the first one seen), preserving all
    seller-SKU names for reference.
    """
    by_asin = {}
    for item in inventory:
        asin = item.get("asin", "")
        seller_sku = item.get("amazon_seller_sku", "")

        if asin and asin in by_asin:
            # Same ASIN already seen — just record the extra seller-SKU
            by_asin[asin]["_extra_seller_skus"].append(seller_sku)
            continue

        # First time seeing this ASIN (or no ASIN — keep as-is)
        key = asin if asin else seller_sku  # fallback to seller_sku if no ASIN
        item["_extra_seller_skus"] = []
        by_asin[key] = item

    return list(by_asin.values())


def _aggregate_by_master_sku(inventory):
    """
    Aggregate inventory rows by master SKU.

    After ASIN deduplication, different ASINs may still map to the same
    master SKU (e.g. a parent ASIN and child ASIN for the same product).
    In that case we DO sum quantities since they represent different
    physical inventory pools.
    """
    by_sku = {}
    for item in inventory:
        sku = item["sku"]
        if sku not in by_sku:
            by_sku[sku] = {
                "sku": sku,
                "asin": item["asin"],
                "product_name": item["product_name"],
                "condition": item.get("condition", ""),
                "fulfillable_quantity": 0,
                "inbound_working": 0,
                "inbound_shipped": 0,
                "inbound_receiving": 0,
                "total_quantity": 0,
                "reserved_quantity": 0,
                "unfulfillable_quantity": 0,
                "_seller_skus": [],
            }
        agg = by_sku[sku]
        agg["fulfillable_quantity"] += item["fulfillable_quantity"]
        agg["inbound_working"] += item["inbound_working"]
        agg["inbound_shipped"] += item["inbound_shipped"]
        agg["inbound_receiving"] += item["inbound_receiving"]
        agg["total_quantity"] += item["total_quantity"]
        agg["reserved_quantity"] += item["reserved_quantity"]
        agg["unfulfillable_quantity"] += item["unfulfillable_quantity"]

        # Collect all seller-SKUs (primary + extras from dedup)
        agg["_seller_skus"].append(item.get("amazon_seller_sku", ""))
        for extra in item.get("_extra_seller_skus", []):
            agg["_seller_skus"].append(extra)

    # Clean up internal field
    result = []
    for sku, agg in by_sku.items():
        agg["amazon_seller_skus"] = ", ".join(filter(None, agg.pop("_seller_skus")))
        result.append(agg)

    return result
