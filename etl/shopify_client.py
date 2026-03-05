"""
Shopify Admin API integration.
Fetches orders and line items, normalizes to common schema.
"""
import logging
import requests
import time
from etl.retry import with_retry
from datetime import datetime, timedelta
import config as cfg
from db import upsert_customer, upsert_order, upsert_order_item, upsert_sku
from etl.customer_id import generate_customer_id

logger = logging.getLogger(__name__)

# Shopify store timezone offset (US Eastern = -05:00 / -04:00 DST)
# Used for query boundaries so orders near midnight don't shift dates
_STORE_TZ_OFFSET = "-05:00"


def _parse_shopify_date(ts_str):
    """Extract YYYY-MM-DD from a Shopify ISO timestamp.

    Shopify REST API returns timestamps in the store's timezone,
    so the first 10 chars are already the correct local date.
    Falls back gracefully for empty/malformed strings.
    """
    if not ts_str or len(ts_str) < 10:
        return None
    return ts_str[:10]


def get_base_url():
    store = cfg.SHOPIFY_STORE_URL.rstrip("/")
    if not store.startswith("http"):
        store = f"https://{store}"
    return f"{store}/admin/api/{cfg.SHOPIFY_API_VERSION}"


def get_headers():
    return {
        "X-Shopify-Access-Token": cfg.SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def _refresh_token():
    """Try to get a new access token using client credentials. Returns (ok, message)."""
    if not cfg.SHOPIFY_CLIENT_ID or not cfg.SHOPIFY_CLIENT_SECRET:
        return False, "Client ID and Secret required to refresh token."
    from etl.shopify_oauth import get_access_token
    token, error = get_access_token()
    if token:
        # Reload config so cfg.SHOPIFY_ACCESS_TOKEN is updated
        from config import reload_config
        reload_config()
        return True, "Token refreshed successfully."
    return False, error or "Failed to refresh token."


def check_scopes():
    """Check which API scopes the app has. Returns list of scope handles."""
    store = cfg.SHOPIFY_STORE_URL.rstrip("/")
    if not store.startswith("http"):
        store = f"https://{store}"
    try:
        resp = requests.get(
            f"{store}/admin/oauth/access_scopes.json",
            headers=get_headers(), timeout=10,
        )
        if resp.status_code == 200:
            return [s["handle"] for s in resp.json().get("access_scopes", [])]
    except Exception:
        pass
    return []


def test_connection():
    """
    Test the Shopify API connection. Returns (ok, message).
    If token is expired, tries to auto-refresh using client credentials.
    """
    if not cfg.SHOPIFY_ACCESS_TOKEN:
        # Try to get a token automatically
        if cfg.SHOPIFY_CLIENT_ID and cfg.SHOPIFY_CLIENT_SECRET:
            ok, msg = _refresh_token()
            if not ok:
                return False, f"No access token, and auto-connect failed: {msg}"
        else:
            return False, "No Shopify access token configured. Go to Settings and click 'Connect Shopify'."

    if not cfg.SHOPIFY_STORE_URL:
        return False, "No Shopify store URL configured. Go to Settings to enter it."

    try:
        base_url = get_base_url()
        headers = get_headers()
        resp = requests.get(f"{base_url}/shop.json", headers=headers, timeout=10)

        if resp.status_code == 401:
            # Token expired — try to refresh
            if cfg.SHOPIFY_CLIENT_ID and cfg.SHOPIFY_CLIENT_SECRET:
                ok, msg = _refresh_token()
                if ok:
                    # Retry with new token
                    headers = get_headers()
                    resp = requests.get(f"{base_url}/shop.json", headers=headers, timeout=10)
                    if resp.status_code == 200:
                        shop = resp.json().get("shop", {})
                        name = shop.get("name", cfg.SHOPIFY_STORE_URL)
                        return True, f"Connected to {name} (token refreshed)"
                return False, (
                    "Access token expired and refresh failed. "
                    "Go to Settings and click 'Connect Shopify' to reconnect."
                )
            return False, (
                "Shopify returned 401 Unauthorized. Your access token is invalid or expired. "
                "Go to Settings and click 'Connect Shopify' to reconnect."
            )

        if resp.status_code == 403:
            return False, "Shopify returned 403 Forbidden. The access token may lack required permissions."
        resp.raise_for_status()
        shop = resp.json().get("shop", {})
        name = shop.get("name", cfg.SHOPIFY_STORE_URL)

        # Check for read_all_orders scope
        scopes = check_scopes()
        scope_warning = ""
        if "read_all_orders" not in scopes:
            scope_warning = (
                " (Warning: app lacks 'read_all_orders' scope — "
                "only last 60 days of orders are accessible. "
                "Request this scope in the Shopify Partner Dashboard for full history.)"
            )

        return True, f"Connected to {name}{scope_warning}"
    except requests.exceptions.ConnectionError:
        return False, f"Could not connect to {cfg.SHOPIFY_STORE_URL}. Check the store URL."
    except Exception as e:
        return False, f"Connection test failed: {e}"


@with_retry
def _get_orders_page(url, headers, params):
    """Fetch a single page of Shopify orders."""
    return requests.get(url, headers=headers, params=params, timeout=30)


def _replay_order(conn, order):
    """Re-execute upserts for a single order (used after deadlock rollback)."""
    shopify_order_id = str(order["id"])
    order_date = _parse_shopify_date(order["created_at"])
    total = float(order.get("total_price", 0))
    currency = order.get("currency", "USD")
    fin_status = order.get("financial_status", "paid") or "paid"
    customer_data = order.get("customer")
    customer_id = generate_customer_id(customer_data)
    customer_email = customer_data.get("email") if customer_data else None
    customer_first_date = (
        _parse_shopify_date(customer_data.get("created_at", "")) if customer_data else None
    ) or order_date
    if customer_id:
        upsert_customer(conn, customer_id, customer_email, "shopify", customer_first_date)
    order_id = f"shp-{shopify_order_id}"
    upsert_order(conn, order_id, "shopify", shopify_order_id,
                 customer_id, order_date, total, currency,
                 financial_status=fin_status)
    for item in order.get("line_items", []):
        sku = str(item.get("sku") or item.get("variant_id") or "UNKNOWN")
        product_name = item.get("title", "")
        variant_title = item.get("variant_title", "")
        if variant_title:
            product_name = f"{product_name} - {variant_title}"
        quantity = int(item.get("quantity", 1))
        gross_price = float(item.get("price", 0))
        line_discount = sum(float(d.get("amount", 0)) for d in item.get("discount_allocations", []))
        net_total = (quantity * gross_price) - line_discount
        unit_price = net_total / quantity if quantity else gross_price
        upsert_order_item(conn, order_id, sku, product_name, quantity, unit_price)
        upsert_sku(conn, sku, product_name, None, order_date, "shopify")


def fetch_orders(conn, since_date=None, until_date=None, on_progress=None,
                 commit_per_page=False):
    """
    Fetch orders from Shopify Admin API and insert into database.

    Args:
        conn: Database connection
        since_date: ISO date string (YYYY-MM-DD). Defaults to 30 days ago.
        until_date: ISO date string. Defaults to today.
        on_progress: Optional callback(orders_so_far, page_number) for progress reporting.
        commit_per_page: If True, commit after each page of 250 orders (for backfills).

    Returns:
        Number of orders fetched.
    """
    if not cfg.SHOPIFY_ACCESS_TOKEN:
        raise ValueError(
            "Shopify access token not configured. "
            "Go to Settings and click 'Connect Shopify' to connect your store."
        )

    if since_date is None:
        since_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    if until_date is None:
        until_date = datetime.utcnow().strftime("%Y-%m-%d")

    base_url = get_base_url()
    headers = get_headers()
    order_count = 0

    # Shopify uses cursor-based pagination via Link headers
    url = f"{base_url}/orders.json"
    params = {
        "created_at_min": f"{since_date}T00:00:00{_STORE_TZ_OFFSET}",
        "created_at_max": f"{until_date}T23:59:59{_STORE_TZ_OFFSET}",
        "status": "any",
        "limit": 250,
        "fields": "id,created_at,total_price,currency,customer,line_items,financial_status",
    }

    page_number = 0
    while url:
        response = _get_orders_page(url, headers, params)

        if response.status_code == 429:
            # Rate limited — Shopify uses leaky bucket
            retry_after = float(response.headers.get("Retry-After", 2))
            logger.warning('Shopify rate limit hit, waiting %ss', retry_after)
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        data = response.json()
        orders = data.get("orders", [])
        page_number += 1

        for order in orders:
            shopify_order_id = str(order["id"])
            order_date = _parse_shopify_date(order["created_at"])
            total = float(order.get("total_price", 0))
            currency = order.get("currency", "USD")
            fin_status = order.get("financial_status", "paid") or "paid"

            # Customer — use Shopify's customer created_at as first_order_date
            # so returning customers are attributed to their true cohort, not
            # the first order we happened to sync.
            customer_data = order.get("customer")
            customer_id = generate_customer_id(customer_data)
            customer_email = customer_data.get("email") if customer_data else None
            customer_first_date = (
                _parse_shopify_date(customer_data.get("created_at", "")) if customer_data else None
            ) or order_date

            if customer_id:
                upsert_customer(conn, customer_id, customer_email, "shopify", customer_first_date)

            # Order
            order_id = f"shp-{shopify_order_id}"
            upsert_order(conn, order_id, "shopify", shopify_order_id,
                         customer_id, order_date, total, currency,
                         financial_status=fin_status)

            # Line items
            for item in order.get("line_items", []):
                sku = item.get("sku") or item.get("variant_id") or "UNKNOWN"
                sku = str(sku)
                product_name = item.get("title", "")
                variant_title = item.get("variant_title", "")
                if variant_title:
                    product_name = f"{product_name} - {variant_title}"

                quantity = int(item.get("quantity", 1))
                gross_price = float(item.get("price", 0))
                line_discount = sum(
                    float(d.get("amount", 0))
                    for d in item.get("discount_allocations", [])
                )
                net_total = (quantity * gross_price) - line_discount
                unit_price = net_total / quantity if quantity else gross_price

                upsert_order_item(conn, order_id, sku, product_name, quantity, unit_price)
                upsert_sku(conn, sku, product_name, None, order_date, "shopify")

            order_count += 1

        if commit_per_page:
            for _attempt in range(5):
                try:
                    conn.commit()
                    break
                except Exception as e:
                    if 'deadlock' in str(e).lower() and _attempt < 4:
                        logger.warning('Deadlock on commit (attempt %d), retrying in %ds...', _attempt + 1, _attempt + 1)
                        conn.rollback()
                        time.sleep(_attempt + 1)
                        # Re-execute the page's upserts after rollback
                        for order in orders:
                            _replay_order(conn, order)
                    else:
                        raise

        if on_progress:
            on_progress(order_count, page_number)

        # Pagination: check for next page link
        url = _get_next_page_url(response)
        params = None  # Params are encoded in the next URL
        time.sleep(0.5)  # Be nice to the API

    # Warn if data range is much shorter than requested (60-day scope issue)
    if order_count > 0 and since_date:
        try:
            requested_start = datetime.strptime(since_date, "%Y-%m-%d")
            oldest = conn.execute("SELECT MIN(order_date) FROM orders").fetchone()[0]
            if oldest:
                actual_start = datetime.strptime(oldest, "%Y-%m-%d")
                actual_end = datetime.strptime(until_date, "%Y-%m-%d")
                requested_days = (actual_end - requested_start).days
                actual_days = (actual_end - actual_start).days
                if requested_days > 90 and actual_days < 70:
                    logger.warning(
                        'Requested orders from %s but oldest is %s (~%d days). '
                        "App may lack 'read_all_orders' scope.",
                        since_date, oldest, actual_days,
                    )
        except Exception:
            pass

    return order_count


def _get_next_page_url(response):
    """Extract next page URL from Shopify's Link header."""
    link_header = response.headers.get("Link", "")
    if not link_header:
        return None

    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            url = part.split(";")[0].strip().strip("<>")
            return url

    return None
