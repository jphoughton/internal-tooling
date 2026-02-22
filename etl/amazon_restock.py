"""
Amazon Restock Inventory Recommendations Report.

Pulls Amazon's own restock recommendations including:
- Average daily sales (7/30/60/90 day windows)
- Days of supply
- Recommended reorder date
- Recommended quantity

This data is used alongside our own velocity forecast for Amazon demand.
Report type: GET_RESTOCK_INVENTORY_RECOMMENDATIONS_REPORT
"""
import csv
import io
from sp_api.api import Reports
from etl.amazon import get_credentials, get_marketplace, _download_report, _wait_for_report
from etl.amazon_sku_map import map_amazon_sku, register_seller_sku
import config as cfg


def fetch_restock_recommendations():
    """
    Fetch Amazon's restock inventory recommendations.

    Returns:
        list of dicts with keys:
            sku, asin, product_name, avg_daily_sales_30d, avg_daily_sales_90d,
            days_of_supply, recommended_reorder_date, recommended_qty,
            inbound_qty, available_qty
    """
    credentials = get_credentials()
    if not credentials.get("refresh_token"):
        return []

    reports_api = Reports(credentials=credentials, marketplace=get_marketplace())

    try:
        report_response = reports_api.create_report(
            reportType="GET_RESTOCK_INVENTORY_RECOMMENDATIONS_REPORT",
            marketplaceIds=[cfg.AMAZON_MARKETPLACE_ID],
        )
    except Exception as e:
        print(f"Could not request restock report: {e}")
        return []

    report_id = report_response.payload["reportId"]
    print(f"Amazon restock report requested: {report_id}")

    document_id = _wait_for_report(reports_api, report_id, timeout_minutes=10)
    if not document_id:
        return []

    content = _download_report(reports_api, document_id)
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")

    results = []
    for row in reader:
        seller_sku = row.get("sku") or row.get("SKU") or row.get("merchant-sku", "")
        asin = row.get("asin") or row.get("ASIN", "")
        if not seller_sku:
            continue

        # Register and map SKU
        register_seller_sku(seller_sku, asin)
        master_sku = map_amazon_sku(seller_sku, asin, row.get("product-name", ""))

        def _safe_float(val, default=0.0):
            try:
                return float(val) if val else default
            except (ValueError, TypeError):
                return default

        def _safe_int(val, default=0):
            try:
                return int(float(val)) if val else default
            except (ValueError, TypeError):
                return default

        results.append({
            "sku": master_sku,
            "amazon_seller_sku": seller_sku,
            "asin": asin,
            "product_name": row.get("product-name", ""),
            "avg_daily_sales_7d": _safe_float(row.get("sales-last-7-days") or row.get("avg-daily-sales-last-7-days")),
            "avg_daily_sales_30d": _safe_float(row.get("sales-last-30-days") or row.get("avg-daily-sales-last-30-days")),
            "avg_daily_sales_60d": _safe_float(row.get("sales-last-60-days") or row.get("avg-daily-sales-last-60-days")),
            "avg_daily_sales_90d": _safe_float(row.get("sales-last-90-days") or row.get("avg-daily-sales-last-90-days")),
            "days_of_supply": _safe_int(row.get("days-of-supply-at-amazon-fulfillment-network", 0)),
            "recommended_reorder_date": row.get("recommended-order-date") or row.get("reorder-date", ""),
            "recommended_qty": _safe_int(row.get("recommended-order-quantity") or row.get("quantity-to-be-charged", 0)),
            "available_qty": _safe_int(row.get("available") or row.get("quantity-available", 0)),
            "inbound_qty": _safe_int(row.get("inbound") or row.get("quantity-inbound-total", 0)),
        })

    # Deduplicate by master SKU (aggregate if needed)
    by_sku = {}
    for item in results:
        sku = item["sku"]
        if sku not in by_sku:
            by_sku[sku] = item
        else:
            # Sum quantities, keep highest daily sales
            existing = by_sku[sku]
            for key in ["available_qty", "inbound_qty", "recommended_qty"]:
                existing[key] += item[key]
            for key in ["avg_daily_sales_7d", "avg_daily_sales_30d", "avg_daily_sales_60d", "avg_daily_sales_90d"]:
                existing[key] = max(existing[key], item[key])

    return list(by_sku.values())
