"""
Amazon SP-API integration.

Data strategy (two report types pulled daily):
1. SALES: Sales & Traffic report (aggregated daily by ASIN).
   Inserts directly into daily_sku_sales for demand forecasting.
   ASINs are mapped to master SKUs via etl.amazon_sku_map.
   Fallback: Flat-file All Orders report if S&T isn't available.

2. RETENTION: FBA Customer Shipment Sales report (GET_FBA_FULFILLMENT_CUSTOMER_SHIPMENT_SALES_DATA).
   Contains hashed buyer emails (consistent across orders), order IDs, SKUs,
   and quantities — enough to build Amazon-side retention cohorts.
   The hashed emails are stable so we can track repeat purchases.
"""
import csv
import gzip
import io
import json
import time
from datetime import datetime, timedelta

from sp_api.api import Reports
from sp_api.base import Marketplaces

import config as cfg
from db import upsert_sku, upsert_customer, upsert_order, upsert_order_item
from etl.amazon_sku_map import map_amazon_sku


def get_credentials():
    """Build SP-API credentials dict. Only LWA fields are required."""
    creds = {
        "refresh_token": cfg.AMAZON_REFRESH_TOKEN,
        "lwa_app_id": cfg.AMAZON_LWA_CLIENT_ID,
        "lwa_client_secret": cfg.AMAZON_LWA_CLIENT_SECRET,
    }
    # Include AWS/IAM fields only if set (optional for most setups)
    if getattr(cfg, "AMAZON_AWS_ACCESS_KEY", ""):
        creds["aws_access_key"] = cfg.AMAZON_AWS_ACCESS_KEY
    if getattr(cfg, "AMAZON_AWS_SECRET_KEY", ""):
        creds["aws_secret_key"] = cfg.AMAZON_AWS_SECRET_KEY
    if getattr(cfg, "AMAZON_ROLE_ARN", ""):
        creds["role_arn"] = cfg.AMAZON_ROLE_ARN
    return creds


def get_marketplace():
    marketplace_map = {
        "ATVPDKIKX0DER": Marketplaces.US,
        "A2EUQ1WTGCTBG2": Marketplaces.CA,
        "A1AM78C64UM0Y8": Marketplaces.MX,
        "A1F83G8C2ARO7P": Marketplaces.UK,
        "A1PA6795UKMFR9": Marketplaces.DE,
    }
    return marketplace_map.get(cfg.AMAZON_MARKETPLACE_ID, Marketplaces.US)


def _download_report(reports_api, document_id):
    """
    Download and decompress an SP-API report document.
    Returns the text content (handles GZIP compression automatically).
    """
    import requests as req

    doc_response = reports_api.get_report_document(document_id)
    payload = doc_response.payload
    report_url = payload["url"]
    compression = payload.get("compressionAlgorithm", "")

    raw = req.get(report_url)

    if compression == "GZIP" or raw.content[:2] == b"\x1f\x8b":
        return gzip.decompress(raw.content).decode("utf-8")
    else:
        return raw.text


def fetch_sales_report(conn, since_date=None, until_date=None):
    """
    PRIMARY METHOD: Fetch Sales & Traffic report (daily aggregated by ASIN).
    ASINs are mapped to master SKUs via amazon_sku_map.
    Inserts directly into daily_sku_sales — no order-level data needed.

    The S&T report is JSON with salesAndTrafficByAsin and salesAndTrafficByDate.
    Each entry has parentAsin (the ASIN string) and childAsin.

    Returns number of daily-ASIN records inserted.
    """
    credentials = get_credentials()
    if not credentials["refresh_token"]:
        raise ValueError("Amazon SP-API credentials not configured. Set AMAZON_REFRESH_TOKEN in .env")

    if since_date is None:
        since_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    if until_date is None:
        until_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    reports_api = Reports(credentials=credentials, marketplace=get_marketplace())

    # Request report — aggregated daily by ASIN (CHILD level)
    report_response = reports_api.create_report(
        reportType="GET_SALES_AND_TRAFFIC_REPORT",
        dataStartTime=f"{since_date}T00:00:00Z",
        dataEndTime=f"{until_date}T23:59:59Z",
        reportOptions={
            "dateGranularity": "DAY",
            "asinGranularity": "CHILD",
        },
        marketplaceIds=[cfg.AMAZON_MARKETPLACE_ID],
    )

    report_id = report_response.payload["reportId"]
    print(f"Amazon Sales & Traffic report requested: {report_id}")

    document_id = _wait_for_report(reports_api, report_id)
    if not document_id:
        print("Report generation failed or timed out. Falling back to flat-file.")
        return fetch_flat_file_orders(conn, since_date, until_date)

    # Download and decompress report
    text = _download_report(reports_api, document_id)
    report_data = json.loads(text)

    # Get date range from report spec
    spec = report_data.get("reportSpecification", {})
    report_date = spec.get("dataStartTime", since_date)[:10]

    record_count = 0
    for entry in report_data.get("salesAndTrafficByAsin", []):
        # parentAsin and childAsin are ASIN strings (not nested objects)
        asin = entry.get("childAsin") or entry.get("parentAsin", "")
        if not asin:
            continue

        sales = entry.get("salesByAsin", {})
        units = int(sales.get("unitsOrdered", 0))
        revenue = float(sales.get("orderedProductSales", {}).get("amount", 0))

        # Use the report date (S&T reports are typically per-day)
        date = entry.get("date", report_date)[:10]

        # Map ASIN to master SKU
        master_sku = map_amazon_sku(asin, asin)  # ASIN is both the identifier

        conn.execute("""
            INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
            VALUES (?, ?, 'amazon', ?, ?, 0)
            ON CONFLICT(sale_date, sku, source) DO UPDATE SET
                units_sold = excluded.units_sold,
                revenue = excluded.revenue
        """, (date, master_sku, units, revenue))

        upsert_sku(conn, master_sku, None, None, date, "amazon")
        record_count += 1

    return record_count


def fetch_flat_file_orders(conn, since_date=None, until_date=None):
    """
    FALLBACK METHOD: Flat-file All Orders report (tab-separated).
    Aggregates order-level data into daily_sku_sales.
    Used when Sales & Traffic report isn't available (e.g., new accounts).

    Returns number of line-item records processed.
    """
    credentials = get_credentials()
    if not credentials["refresh_token"]:
        raise ValueError("Amazon SP-API credentials not configured.")

    if since_date is None:
        since_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    if until_date is None:
        until_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    reports_api = Reports(credentials=credentials, marketplace=get_marketplace())

    report_response = reports_api.create_report(
        reportType="GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
        dataStartTime=f"{since_date}T00:00:00Z",
        dataEndTime=f"{until_date}T23:59:59Z",
        marketplaceIds=[cfg.AMAZON_MARKETPLACE_ID],
    )

    report_id = report_response.payload["reportId"]
    print(f"Amazon flat-file order report requested: {report_id}")

    document_id = _wait_for_report(reports_api, report_id)
    if not document_id:
        return 0

    content = _download_report(reports_api, document_id)

    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    record_count = 0

    for row in reader:
        seller_sku = row.get("sku", "")
        if not seller_sku:
            continue

        asin = row.get("asin", "")
        product_name = row.get("product-name", "")

        # Map Amazon seller-SKU to master SKU via ASIN
        master_sku = map_amazon_sku(seller_sku, asin, product_name)

        date = row.get("purchase-date", "")[:10]
        if not date:
            continue

        # Column name varies: "quantity-purchased" or "quantity"
        qty_str = row.get("quantity-purchased", "") or row.get("quantity", "")
        try:
            quantity = int(float(qty_str)) if qty_str else 1
        except (ValueError, TypeError):
            quantity = 1

        price_str = row.get("item-price", "")
        try:
            price = float(price_str) if price_str else 0.0
        except (ValueError, TypeError):
            price = 0.0

        # Skip rows with no quantity data (header-only or cancelled)
        if quantity <= 0:
            quantity = 1  # Default to 1 unit for orders without qty

        # Upsert-aggregate into daily sales
        conn.execute("""
            INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
            VALUES (?, ?, 'amazon', ?, ?, 1)
            ON CONFLICT(sale_date, sku, source) DO UPDATE SET
                units_sold = daily_sku_sales.units_sold + excluded.units_sold,
                revenue = daily_sku_sales.revenue + excluded.revenue,
                order_count = daily_sku_sales.order_count + 1
        """, (date, master_sku, quantity, price))

        upsert_sku(conn, master_sku, product_name, None, date, "amazon")
        record_count += 1

    return record_count


def fetch_fulfillment_data(conn, since_date=None, until_date=None):
    """
    Fetch FBA Customer Shipment Sales data for retention analysis.
    This report contains hashed buyer emails (buyer-email column) that are
    consistent across orders, letting us track Amazon repeat purchases.

    Inserts into orders, order_items, and customers tables for cohort analysis.
    Returns number of records processed.
    """
    credentials = get_credentials()
    if not credentials["refresh_token"]:
        raise ValueError("Amazon SP-API credentials not configured.")

    if since_date is None:
        since_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    if until_date is None:
        until_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    reports_api = Reports(credentials=credentials, marketplace=get_marketplace())

    report_response = reports_api.create_report(
        reportType="GET_FBA_FULFILLMENT_CUSTOMER_SHIPMENT_SALES_DATA",
        dataStartTime=f"{since_date}T00:00:00Z",
        dataEndTime=f"{until_date}T23:59:59Z",
        marketplaceIds=[cfg.AMAZON_MARKETPLACE_ID],
    )

    report_id = report_response.payload["reportId"]
    print(f"Amazon fulfillment report requested: {report_id}")

    document_id = _wait_for_report(reports_api, report_id)
    if not document_id:
        return 0

    content = _download_report(reports_api, document_id)

    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    record_count = 0

    for row in reader:
        # The hashed buyer email is stable across orders
        buyer_email_hash = row.get("buyer-email", "")
        if not buyer_email_hash:
            continue

        customer_id = f"amz-{buyer_email_hash[:16]}"
        amazon_order_id = row.get("amazon-order-id", "")
        shipment_date = row.get("shipment-date", "")[:10]
        seller_sku = row.get("sku", "")
        asin = row.get("asin", "")
        try:
            quantity = int(float(row.get("quantity-shipped", 0) or 0))
        except (ValueError, TypeError):
            quantity = 1
        try:
            price_per_unit = float(row.get("item-price-per-unit", 0) or 0)
        except (ValueError, TypeError):
            price_per_unit = 0.0
        product_name = row.get("product-name", "")

        if not seller_sku or not amazon_order_id:
            continue

        # Map Amazon seller-SKU to master SKU via ASIN
        master_sku = map_amazon_sku(seller_sku, asin, product_name)

        # Customer (use hash as email placeholder — it's not a real email)
        upsert_customer(conn, customer_id, None, "amazon", shipment_date)

        # Order
        order_id = f"amz-{amazon_order_id}"
        total = round(quantity * price_per_unit, 2)
        upsert_order(conn, order_id, "amazon", amazon_order_id,
                      customer_id, shipment_date, total)

        # Order item
        upsert_order_item(conn, order_id, master_sku, product_name, quantity, price_per_unit)
        upsert_sku(conn, master_sku, product_name, None, shipment_date, "amazon")
        record_count += 1

    return record_count


def _wait_for_report(reports_api, report_id, timeout_minutes=20):
    """Poll report status until complete. Returns document ID or None."""
    deadline = time.time() + (timeout_minutes * 60)

    while time.time() < deadline:
        status_response = reports_api.get_report(report_id)
        status = status_response.payload.get("processingStatus")

        if status == "DONE":
            return status_response.payload.get("reportDocumentId")
        elif status in ("CANCELLED", "FATAL"):
            print(f"Report {report_id} failed: {status}")
            return None

        time.sleep(30)

    print(f"Report {report_id} timed out after {timeout_minutes}m")
    return None
