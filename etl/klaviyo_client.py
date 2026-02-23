"""
Klaviyo API integration — fetches campaign/flow metadata and performance metrics.

Uses the official klaviyo-api SDK.

Docs: https://developers.klaviyo.com/en/reference/api-overview
Auth: Private API key via header  Authorization: Klaviyo-API-Key {key}
"""
import logging
import requests as _requests
from klaviyo_api import KlaviyoAPI

logger = logging.getLogger(__name__)


def get_client(api_key):
    """Create a Klaviyo API client."""
    return KlaviyoAPI(api_key)


def test_connection(api_key):
    """Test the Klaviyo API connection. Returns (ok, message)."""
    try:
        client = get_client(api_key)
        # Fetch account info to test the key
        resp = client.Accounts.get_accounts()
        if resp and hasattr(resp, "data") and resp.data:
            contact_info = getattr(resp.data[0].attributes, "contact_information", None)
            name = getattr(contact_info, "organization_name", "Connected") if contact_info else "Connected"
            return True, f"Connected to Klaviyo ({name})"
        return True, "Connected to Klaviyo"
    except Exception as e:
        return False, f"Connection failed: {e}"


def fetch_campaigns(api_key, status="sent", limit=50):
    """
    Fetch email campaigns from Klaviyo.

    Args:
        api_key: Klaviyo private API key
        status: Filter by status ('draft', 'scheduled', 'sent')
        limit: Max campaigns to return

    Returns:
        list of dicts with campaign data
    """
    try:
        client = get_client(api_key)
        campaigns = []
        resp = client.Campaigns.get_campaigns(
            filter="equals(messages.channel,'email')",
        )

        if resp and hasattr(resp, "data"):
            for c in resp.data:
                attrs = c.attributes
                campaigns.append({
                    "id": c.id,
                    "name": getattr(attrs, "name", ""),
                    "status": getattr(attrs, "status", ""),
                    "send_time": getattr(attrs, "send_time", ""),
                    "created_at": getattr(attrs, "created_at", ""),
                    "updated_at": getattr(attrs, "updated_at", ""),
                })

        logger.info(f"Fetched {len(campaigns)} Klaviyo campaigns")
        return campaigns
    except Exception as e:
        logger.error(f"Failed to fetch Klaviyo campaigns: {e}")
        raise


def fetch_flows(api_key, status="live"):
    """
    Fetch flows (automated email sequences) from Klaviyo.

    Returns:
        list of dicts with flow data
    """
    try:
        client = get_client(api_key)
        flows = []
        resp = client.Flows.get_flows(
            filter=f"equals(status,'{status}')",
            sort="name",
        )

        if resp and hasattr(resp, "data"):
            for f in resp.data:
                attrs = f.attributes
                flows.append({
                    "id": f.id,
                    "name": getattr(attrs, "name", ""),
                    "status": getattr(attrs, "status", ""),
                    "created": getattr(attrs, "created", ""),
                    "updated": getattr(attrs, "updated", ""),
                    "trigger_type": getattr(attrs, "trigger_type", ""),
                })

        logger.info(f"Fetched {len(flows)} Klaviyo flows")
        return flows
    except Exception as e:
        logger.error(f"Failed to fetch Klaviyo flows: {e}")
        raise


def fetch_lists(api_key):
    """Fetch all lists/segments for subscriber counts."""
    try:
        client = get_client(api_key)
        lists = []
        resp = client.Lists.get_lists()

        if resp and hasattr(resp, "data"):
            for l in resp.data:
                attrs = l.attributes
                lists.append({
                    "id": l.id,
                    "name": getattr(attrs, "name", ""),
                    "created": getattr(attrs, "created", ""),
                    "updated": getattr(attrs, "updated", ""),
                })

        logger.info(f"Fetched {len(lists)} Klaviyo lists")
        return lists
    except Exception as e:
        logger.error(f"Failed to fetch Klaviyo lists: {e}")
        raise


# ---------------------------------------------------------------------------
# Reporting API (raw HTTP — SDK Pydantic DTOs are brittle)
# ---------------------------------------------------------------------------
_KLAVIYO_API = "https://a.klaviyo.com/api"
_REVISION = "2025-01-15"

_ENGAGEMENT_STATS = [
    "recipients", "delivered", "opens_unique", "clicks_unique",
    "open_rate", "click_rate", "click_to_open_rate",
    "unsubscribes", "unsubscribe_rate", "bounce_rate",
]
_REVENUE_STATS = [
    "revenue", "revenue_per_recipient", "average_order_value", "conversion_rate",
]


def _klaviyo_headers(api_key):
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "revision": _REVISION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def fetch_placed_order_metric_id(api_key):
    """Auto-discover the Klaviyo 'Placed Order' metric ID via raw HTTP.

    Returns metric ID string, or None if not found.
    """
    try:
        resp = _requests.get(
            f"{_KLAVIYO_API}/metrics",
            headers=_klaviyo_headers(api_key),
            params={"filter": "equals(name,'Placed Order')"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data:
            metric_id = data[0]["id"]
            logger.info(f"Found Placed Order metric: {metric_id}")
            return metric_id
        logger.warning("Placed Order metric not found in Klaviyo")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch Placed Order metric: {e}")
        return None


def _build_report_body(report_type, stats, conversion_metric_id):
    """Build the JSON:API body for a Klaviyo reporting endpoint."""
    attrs = {
        "statistics": stats,
        "timeframe": {"key": "last_12_months"},
        "conversion_metric_id": conversion_metric_id,
        "filter": "equals(send_channel,'email')",
    }
    return {"data": {"type": report_type, "attributes": attrs}}


def fetch_campaign_metrics(api_key, conversion_metric_id):
    """Fetch performance metrics for all email campaigns.

    Uses POST /api/campaign-values-reports (1 API call for all campaigns).
    Requires a valid conversion_metric_id.

    Returns dict mapping campaign_id -> {metric_name: value, ...}
    """
    if not conversion_metric_id:
        logger.warning("No conversion_metric_id — skipping campaign metrics")
        return {}

    stats = _ENGAGEMENT_STATS + _REVENUE_STATS

    body = _build_report_body("campaign-values-report", stats, conversion_metric_id)

    resp = _requests.post(
        f"{_KLAVIYO_API}/campaign-values-reports",
        headers=_klaviyo_headers(api_key),
        json=body,
        timeout=30,
    )
    if not resp.ok:
        error_detail = resp.text[:1000]
        logger.error(f"Campaign metrics API {resp.status_code}: {error_detail}")
        raise RuntimeError(f"Klaviyo Reporting API {resp.status_code}: {error_detail}")
    data = resp.json()

    results = {}
    for item in data.get("data", {}).get("attributes", {}).get("results", []):
        campaign_id = item.get("groupings", {}).get("campaign_id")
        if campaign_id:
            results[campaign_id] = item.get("statistics", {})

    logger.info(f"Fetched metrics for {len(results)} campaigns")
    return results


def fetch_flow_metrics(api_key, conversion_metric_id):
    """Fetch performance metrics for all flows.

    Uses POST /api/flow-values-reports (1 API call for all flows).
    Requires a valid conversion_metric_id.

    Returns dict mapping flow_id -> {metric_name: value, ...}
    """
    if not conversion_metric_id:
        logger.warning("No conversion_metric_id — skipping flow metrics")
        return {}

    stats = _ENGAGEMENT_STATS + _REVENUE_STATS

    body = _build_report_body("flow-values-report", stats, conversion_metric_id)

    resp = _requests.post(
        f"{_KLAVIYO_API}/flow-values-reports",
        headers=_klaviyo_headers(api_key),
        json=body,
        timeout=30,
    )
    if not resp.ok:
        error_detail = resp.text[:1000]
        logger.error(f"Flow metrics API {resp.status_code}: {error_detail}")
        raise RuntimeError(f"Klaviyo Reporting API {resp.status_code}: {error_detail}")
    data = resp.json()

    results = {}
    for item in data.get("data", {}).get("attributes", {}).get("results", []):
        flow_id = item.get("groupings", {}).get("flow_id")
        if flow_id:
            results[flow_id] = item.get("statistics", {})

    logger.info(f"Fetched metrics for {len(results)} flows")
    return results
