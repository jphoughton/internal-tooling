"""Stable customer ID generation for Shopify imports."""
import hashlib


def generate_customer_id(customer_data):
    """Generate stable customer ID from Shopify customer data.

    Handles both REST API format (numeric ID) and GraphQL format (GID string).
    Falls back to email MD5 hash if no ID is available.
    """
    if not customer_data:
        return None
    raw_id = customer_data.get("id")
    if raw_id:
        # GraphQL GID: "gid://shopify/Customer/12345" -> extract numeric part
        str_id = str(raw_id)
        if "/" in str_id:
            str_id = str_id.split("/")[-1]
        return f"shp-{str_id}"
    email = customer_data.get("email", "")
    if email:
        return f"shp-{hashlib.md5(email.lower().encode()).hexdigest()[:12]}"
    return None
