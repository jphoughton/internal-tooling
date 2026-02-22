"""
Packiyo 3PL API client.
Fetches real-time inventory levels from the Packiyo WMS.
"""
import requests
import config


def _headers():
    return {
        "Authorization": f"Bearer {config.PACKIYO_API_TOKEN}",
        "Accept": "application/vnd.api+json",
    }


def test_connection():
    """Test the Packiyo API connection. Returns (ok, message)."""
    if not config.PACKIYO_API_TOKEN:
        return False, "No Packiyo API token configured."
    try:
        url = f"{config.PACKIYO_API_URL}/customers/{config.PACKIYO_CUSTOMER_ID}"
        resp = requests.get(url, headers=_headers(), timeout=15)
        if resp.status_code == 200:
            return True, "Connected to Packiyo."
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def get_inventory():
    """
    Fetch all product inventory from Packiyo.

    Returns:
        list of dicts with keys:
            sku, name, quantity_on_hand, quantity_allocated,
            quantity_available, quantity_backordered,
            quantity_inbound, quantity_reserved, barcode, tags
    """
    url = f"{config.PACKIYO_API_URL}/products"
    params = {"customer_id": config.PACKIYO_CUSTOMER_ID}
    resp = requests.get(url, headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    products = data.get("data", [])

    inventory = []
    for p in products:
        attrs = p.get("attributes", {})
        inventory.append({
            "sku": attrs.get("sku", ""),
            "name": attrs.get("name", ""),
            "quantity_on_hand": attrs.get("quantity_on_hand", 0),
            "quantity_allocated": attrs.get("quantity_allocated", 0),
            "quantity_available": attrs.get("quantity_available", 0),
            "quantity_backordered": attrs.get("quantity_backordered", 0),
            "quantity_inbound": attrs.get("quantity_inbound", 0),
            "quantity_reserved": attrs.get("quantity_reserved", 0),
            "barcode": attrs.get("barcode", ""),
            "tags": attrs.get("tags", ""),
        })

    return inventory
