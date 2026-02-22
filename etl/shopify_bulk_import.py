"""
Import orders from a Shopify Bulk Operation JSONL export.

The JSONL format has one JSON object per line. Orders appear first, followed
by their line items (linked via __parentId). Line items follow their parent order.

This is much faster than paginated REST API for large historical backfills.
"""
import json
import hashlib
import requests
import time
import config as cfg
from db import get_db, upsert_customer, upsert_order, upsert_order_item, upsert_sku, rebuild_daily_sales


def poll_bulk_operation(timeout_minutes=30):
    """Poll Shopify for bulk operation completion. Returns download URL or raises."""
    store = cfg.SHOPIFY_STORE_URL.rstrip("/")
    if not store.startswith("http"):
        store = f"https://{store}"

    headers = {
        "X-Shopify-Access-Token": cfg.SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }

    query = """
    {
      currentBulkOperation {
        id
        status
        errorCode
        objectCount
        fileSize
        url
      }
    }
    """

    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        resp = requests.post(
            f"{store}/admin/api/{cfg.SHOPIFY_API_VERSION}/graphql.json",
            headers=headers,
            json={"query": query},
            timeout=15,
        )
        data = resp.json()["data"]["currentBulkOperation"]

        if data is None:
            raise RuntimeError("No bulk operation found")

        status = data["status"]
        obj_count = data.get("objectCount", 0)

        if status == "COMPLETED":
            url = data.get("url")
            file_size = data.get("fileSize")
            print(f"Bulk operation complete: {obj_count} objects, {file_size} bytes")
            return url

        if status in ("FAILED", "CANCELED"):
            raise RuntimeError(f"Bulk operation {status}: {data.get('errorCode')}")

        print(f"  Bulk op status: {status} ({obj_count} objects processed...)")
        time.sleep(10)

    raise TimeoutError(f"Bulk operation did not complete within {timeout_minutes} minutes")


def _generate_customer_id(customer_data):
    """Generate stable customer ID from Shopify customer data."""
    if not customer_data:
        return None
    # Use Shopify's GID (e.g. "gid://shopify/Customer/12345")
    gid = customer_data.get("id")
    if gid:
        # Extract numeric ID from GID
        numeric_id = gid.split("/")[-1]
        return f"shp-{numeric_id}"
    email = customer_data.get("email")
    if email:
        return f"shp-{hashlib.md5(email.lower().encode()).hexdigest()[:12]}"
    return None


def download_and_import(url, on_progress=None):
    """Download the JSONL file and import into database."""
    print("Downloading JSONL file...")
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()

    # Stream and parse line by line
    orders = {}  # gid -> order dict
    line_items = []  # (parent_gid, item dict)
    line_count = 0

    for line in resp.iter_lines():
        if not line:
            continue
        obj = json.loads(line)
        line_count += 1

        parent_id = obj.get("__parentId")

        if parent_id is None:
            # This is an order
            orders[obj["id"]] = obj
        else:
            # This is a line item (child of an order)
            line_items.append((parent_id, obj))

        if line_count % 50000 == 0:
            print(f"  Parsed {line_count:,} lines ({len(orders):,} orders)...")

    print(f"Parsed complete: {len(orders):,} orders, {len(line_items):,} line items")

    # Now insert into database
    imported = 0
    skipped = 0

    with get_db() as conn:
        for order_gid, order in orders.items():
            financial_status = (order.get("displayFinancialStatus") or "").upper()
            if financial_status in ("REFUNDED", "VOIDED"):
                skipped += 1
                continue

            order_date = order["createdAt"][:10]
            money = order.get("totalPriceSet", {}).get("shopMoney", {})
            total = float(money.get("amount", 0))
            currency = money.get("currencyCode", "USD")

            customer_data = order.get("customer")
            customer_id = _generate_customer_id(customer_data)
            customer_email = customer_data.get("email") if customer_data else None

            if customer_id:
                upsert_customer(conn, customer_id, customer_email, "shopify", order_date)

            # Extract numeric order ID from GID
            shopify_order_id = order_gid.split("/")[-1]
            order_id = f"shp-{shopify_order_id}"

            upsert_order(conn, order_id, "shopify", shopify_order_id,
                         customer_id, order_date, total, currency)
            imported += 1

            if imported % 10000 == 0:
                conn.commit()
                if on_progress:
                    on_progress(imported, len(orders))
                print(f"  Imported {imported:,} / {len(orders):,} orders...")

        # Now insert line items
        print(f"Inserting {len(line_items):,} line items...")
        for parent_gid, item in line_items:
            order = orders.get(parent_gid)
            if not order:
                continue

            financial_status = (order.get("displayFinancialStatus") or "").upper()
            if financial_status in ("REFUNDED", "VOIDED"):
                continue

            order_date = order["createdAt"][:10]
            shopify_order_id = parent_gid.split("/")[-1]
            order_id = f"shp-{shopify_order_id}"

            sku = item.get("sku") or "UNKNOWN"
            sku = str(sku)
            product_name = item.get("title", "")
            variant_title = item.get("variantTitle", "")
            if variant_title:
                product_name = f"{product_name} - {variant_title}"

            quantity = int(item.get("quantity", 1))
            unit_price_data = item.get("originalUnitPriceSet", {}).get("shopMoney", {})
            unit_price = float(unit_price_data.get("amount", 0))

            upsert_order_item(conn, order_id, sku, product_name, quantity, unit_price)
            upsert_sku(conn, sku, product_name, None, order_date, "shopify")

        conn.commit()
        print(f"Imported {imported:,} orders, skipped {skipped:,} refunded/voided")

        # Rebuild daily sales aggregates
        print("Rebuilding daily sales aggregates...")
        rebuild_daily_sales(conn)

    return imported


def run_bulk_backfill(on_progress=None):
    """
    Start a bulk operation, wait for it, download and import.
    Returns number of orders imported.
    """
    store = cfg.SHOPIFY_STORE_URL.rstrip("/")
    if not store.startswith("http"):
        store = f"https://{store}"

    headers = {
        "X-Shopify-Access-Token": cfg.SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }

    # Check if there's already a running bulk operation
    check_query = """
    {
      currentBulkOperation {
        id
        status
        url
      }
    }
    """
    resp = requests.post(
        f"{store}/admin/api/{cfg.SHOPIFY_API_VERSION}/graphql.json",
        headers=headers,
        json={"query": check_query},
        timeout=15,
    )
    current = resp.json()["data"]["currentBulkOperation"]

    if current and current["status"] == "COMPLETED" and current.get("url"):
        print("Found completed bulk operation, importing...")
        return download_and_import(current["url"], on_progress=on_progress)

    if current and current["status"] == "RUNNING":
        print("Bulk operation already running, waiting for completion...")
        url = poll_bulk_operation(timeout_minutes=30)
        if url:
            return download_and_import(url, on_progress=on_progress)
        return 0

    # Start new bulk operation
    mutation = '''
    mutation {
      bulkOperationRunQuery(
        query: """
        {
          orders(query: "created_at:>=2020-01-01") {
            edges {
              node {
                id
                createdAt
                totalPriceSet {
                  shopMoney {
                    amount
                    currencyCode
                  }
                }
                displayFinancialStatus
                customer {
                  id
                  email
                }
                lineItems(first: 50) {
                  edges {
                    node {
                      sku
                      title
                      variantTitle
                      quantity
                      originalUnitPriceSet {
                        shopMoney {
                          amount
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
      ) {
        bulkOperation {
          id
          status
        }
        userErrors {
          field
          message
        }
      }
    }
    '''

    print("Starting bulk export from Shopify...")
    resp = requests.post(
        f"{store}/admin/api/{cfg.SHOPIFY_API_VERSION}/graphql.json",
        headers=headers,
        json={"query": mutation},
        timeout=30,
    )
    result = resp.json()
    errors = result["data"]["bulkOperationRunQuery"]["userErrors"]
    if errors:
        raise RuntimeError(f"Bulk operation failed: {errors}")

    op = result["data"]["bulkOperationRunQuery"]["bulkOperation"]
    print(f"Bulk operation {op['id']} status: {op['status']}")

    # Poll for completion
    url = poll_bulk_operation(timeout_minutes=30)
    if url:
        return download_and_import(url, on_progress=on_progress)
    return 0


if __name__ == "__main__":
    from db import init_db
    init_db()
    count = run_bulk_backfill()
    print(f"\nDone! Imported {count:,} orders")
