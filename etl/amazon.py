"""
Amazon SP-API integration.

Data strategy (two report types pulled daily):
1. SALES: Sales & Traffic report (aggregated daily by ASIN).
   Inserts directly into daily_sku_sales for demand forecasting.
   ASINs are mapped to master SKUs via etl.amazon_sku_map.

2. RETENTION: Fulfilled Shipments report (GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL).
   Contains buyer-email (stable marketplace alias per buyer), order IDs, SKUs,
   and pricing — enough to build Amazon-side retention cohorts.
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
from etl.retry import with_retry

logger = logging.getLogger(__name__)


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


@with_retry
def _create_report(reports_api, **kwargs):
    """Submit a single SP-API CreateReport request."""
    return reports_api.create_report(**kwargs)


@with_retry
def _get_report_status(reports_api, report_id):
    """Single status poll for an SP-API report."""
    return reports_api.get_report(report_id)


@with_retry
def _get_report_document(reports_api, document_id):
    """Fetch SP-API report document metadata (signed URL + compression type)."""
    return reports_api.get_report_document(document_id)


@with_retry
def _fetch_report_url(url):
    """Download raw report content from a pre-signed S3 URL."""
    import requests as req
    resp = req.get(url, timeout=60)
    resp.raise_for_status()
    return resp


def _download_report(reports_api, document_id):
    """
    Download and decompress an SP-API report document.
    Returns the text content (handles GZIP compression automatically).
    """
    doc_response = _get_report_document(reports_api, document_id)
    payload = doc_response.payload
    report_url = payload["url"]
    compression = payload.get("compressionAlgorithm", "")

    raw = _fetch_report_url(report_url)

    if compression == "GZIP" or raw.content[:2] == b"\x1f\x8b":
        return gzip.decompress(raw.content).decode("utf-8")
    else:
        return raw.text


def fetch_sales_report(conn, since_date=None, until_date=None):
    """
    Fetch Sales & Traffic reports one day at a time (by ASIN).

    The S&T report's salesAndTrafficByAsin section aggregates across the
    entire date range and has no per-row date field.  Requesting one day at
    a time gives us accurate daily-SKU granularity.

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
    record_count = 0

    current = datetime.strptime(since_date, "%Y-%m-%d")
    end = datetime.strptime(until_date, "%Y-%m-%d")

    while current <= end:
        day_str = current.strftime("%Y-%m-%d")
        logger.info('Requesting S&T for %s', day_str)

        try:
            report_response = _create_report(
                reports_api,
                reportType="GET_SALES_AND_TRAFFIC_REPORT",
                dataStartTime=f"{day_str}T00:00:00Z",
                dataEndTime=f"{day_str}T23:59:59Z",
                reportOptions={
                    "dateGranularity": "DAY",
                    "asinGranularity": "CHILD",
                },
                marketplaceIds=[cfg.AMAZON_MARKETPLACE_ID],
            )

            report_id = report_response.payload["reportId"]
            document_id = _wait_for_report(reports_api, report_id)
            if not document_id:
                logger.warning('Skipping %s — report failed', day_str)
                current += timedelta(days=1)
                continue

            text = _download_report(reports_api, document_id)
            report_data = json.loads(text)

            day_count = 0
            for entry in report_data.get("salesAndTrafficByAsin", []):
                asin = entry.get("childAsin") or entry.get("parentAsin", "")
                if not asin:
                    continue

                sales = entry.get("salesByAsin", {})
                units = int(sales.get("unitsOrdered", 0))
                revenue = float(sales.get("orderedProductSales", {}).get("amount", 0))

                master_sku = map_amazon_sku(asin, asin)

                conn.execute("""
                    INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
                    VALUES (?, ?, 'amazon', ?, ?, 0)
                    ON CONFLICT(sale_date, sku, source) DO UPDATE SET
                        units_sold = excluded.units_sold,
                        revenue = excluded.revenue
                """, (day_str, master_sku, units, revenue))

                upsert_sku(conn, master_sku, None, None, day_str, "amazon")
                day_count += 1

            record_count += day_count
            logger.info('%s: %d SKUs, done', day_str, day_count)

        except Exception as e:
            err_str = str(e)
            if 'QuotaExceeded' in err_str:
                logger.warning('%s: Rate limited — waiting 60s', day_str)
                time.sleep(60)
                continue  # Retry same day
            logger.error('%s: ERROR — %s', day_str, e)

        current += timedelta(days=1)

    return record_count


def fetch_fulfillment_data(conn, since_date=None, until_date=None):
    """
    Fetch Amazon Fulfilled Shipments data for retention analysis.

    Uses GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL which provides
    buyer-email (hashed marketplace alias, stable per buyer), order IDs,
    SKUs, and pricing — enough to build Amazon-side retention cohorts.

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

    report_response = _create_report(
        reports_api,
        reportType="GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL",
        dataStartTime=f"{since_date}T00:00:00Z",
        dataEndTime=f"{until_date}T23:59:59Z",
        marketplaceIds=[cfg.AMAZON_MARKETPLACE_ID],
    )

    report_id = report_response.payload["reportId"]
    logger.info('Amazon fulfillment report requested: %s', report_id)

    document_id = _wait_for_report(reports_api, report_id)
    if not document_id:
        return 0

    content = _download_report(reports_api, document_id)

    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    record_count = 0

    for row in reader:
        # buyer-email is a stable marketplace alias (e.g. abc123@marketplace.amazon.com)
        buyer_email_hash = row.get("buyer-email", "")
        if not buyer_email_hash:
            continue

        customer_id = f"amz-{buyer_email_hash[:16]}"
        amazon_order_id = row.get("amazon-order-id", "")
        # Use purchase-date for cohort assignment (when the order was placed)
        order_date_raw = row.get("purchase-date", "") or row.get("shipment-date", "")
        order_date = order_date_raw[:10]
        seller_sku = row.get("sku", "")
        try:
            quantity = int(float(row.get("quantity-shipped", 0) or 0))
        except (ValueError, TypeError):
            quantity = 1
        # item-price is the total line-item price (not per-unit)
        try:
            item_price = float(row.get("item-price", 0) or 0)
        except (ValueError, TypeError):
            item_price = 0.0
        price_per_unit = round(item_price / quantity, 2) if quantity > 0 else 0.0
        product_name = row.get("product-name", "")

        if not seller_sku or not amazon_order_id:
            continue

        # Map Amazon seller-SKU to master SKU (no ASIN in this report)
        master_sku = map_amazon_sku(seller_sku, "", product_name)

        # Customer (use marketplace alias as identifier)
        upsert_customer(conn, customer_id, None, "amazon", order_date)

        # Order
        order_id = f"amz-{amazon_order_id}"
        total = round(item_price, 2)
        upsert_order(conn, order_id, "amazon", amazon_order_id,
                      customer_id, order_date, total)

        # Order item
        upsert_order_item(conn, order_id, master_sku, product_name, quantity, price_per_unit)
        upsert_sku(conn, master_sku, product_name, None, order_date, "amazon")
        record_count += 1

    return record_count


def _wait_for_report(reports_api, report_id, timeout_minutes=20):
    """Poll report status until complete. Returns document ID or None."""
    deadline = time.time() + (timeout_minutes * 60)

    poll_count = 0
    while time.time() < deadline:
        status_response = _get_report_status(reports_api, report_id)
        status = status_response.payload.get("processingStatus")
        poll_count += 1
        elapsed = int(time.time() - (deadline - timeout_minutes * 60))
        logger.info('Report %s: %s (%ds elapsed)', report_id, status, elapsed)

        if status == "DONE":
            return status_response.payload.get("reportDocumentId")
        elif status in ("CANCELLED", "FATAL"):
            logger.error('Report %s failed: %s', report_id, status)
            return None

        time.sleep(30)

    logger.warning('Report %s timed out after %dm', report_id, timeout_minutes)
    return None
