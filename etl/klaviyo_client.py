"""
Klaviyo API integration — fetches campaign and flow performance metrics.

Uses the official klaviyo-api SDK.

Docs: https://developers.klaviyo.com/en/reference/api-overview
Auth: Private API key via header  Authorization: Klaviyo-API-Key {key}
"""
import logging
from datetime import datetime, timedelta
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
            name = resp.data[0].attributes.get("contact_information", {}).get("organization_name", "Connected")
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
            filter=f'equals(messages.channel,"email"),equals(status,"{status}")',
            sort="-send_time",
            page_size=min(limit, 50),
        )

        if resp and hasattr(resp, "data"):
            for c in resp.data:
                attrs = c.attributes
                campaigns.append({
                    "id": c.id,
                    "name": attrs.get("name", ""),
                    "status": attrs.get("status", ""),
                    "send_time": attrs.get("send_time", ""),
                    "created_at": attrs.get("created_at", ""),
                    "updated_at": attrs.get("updated_at", ""),
                })

        logger.info(f"Fetched {len(campaigns)} Klaviyo campaigns")
        return campaigns
    except Exception as e:
        logger.error(f"Failed to fetch Klaviyo campaigns: {e}")
        return []


def fetch_campaign_metrics(api_key, campaign_id):
    """
    Fetch performance metrics for a specific campaign.

    Returns dict with opens, clicks, revenue, etc.
    """
    try:
        client = get_client(api_key)
        # Use the Reporting API for campaign stats
        resp = client.Reporting.query_campaign_values(
            body={
                "data": {
                    "type": "campaign-values-report",
                    "attributes": {
                        "statistics": [
                            "opens", "unique_opens", "open_rate",
                            "clicks", "unique_clicks", "click_rate",
                            "recipients", "bounces", "bounce_rate",
                            "unsubscribes", "unsubscribe_rate",
                            "revenue", "conversion_value",
                            "conversions", "conversion_rate",
                        ],
                        "timeframe": {"key": "last_365_days"},
                        "conversion_metric_id": "Placed Order",
                        "filter": f'equals(campaign_id,"{campaign_id}")',
                    },
                }
            }
        )

        if resp and hasattr(resp, "data") and resp.data:
            result = resp.data[0].attributes.get("statistics", {})
            return result

        return {}
    except Exception as e:
        logger.error(f"Failed to fetch metrics for campaign {campaign_id}: {e}")
        return {}


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
            filter=f'equals(status,"{status}")',
            sort="name",
            page_size=50,
        )

        if resp and hasattr(resp, "data"):
            for f in resp.data:
                attrs = f.attributes
                flows.append({
                    "id": f.id,
                    "name": attrs.get("name", ""),
                    "status": attrs.get("status", ""),
                    "created": attrs.get("created", ""),
                    "updated": attrs.get("updated", ""),
                    "trigger_type": attrs.get("trigger_type", ""),
                })

        logger.info(f"Fetched {len(flows)} Klaviyo flows")
        return flows
    except Exception as e:
        logger.error(f"Failed to fetch Klaviyo flows: {e}")
        return []


def fetch_flow_metrics(api_key, flow_id):
    """Fetch performance metrics for a specific flow."""
    try:
        client = get_client(api_key)
        resp = client.Reporting.query_flow_values(
            body={
                "data": {
                    "type": "flow-values-report",
                    "attributes": {
                        "statistics": [
                            "opens", "unique_opens", "open_rate",
                            "clicks", "unique_clicks", "click_rate",
                            "recipients", "bounces", "bounce_rate",
                            "unsubscribes", "unsubscribe_rate",
                            "revenue", "conversion_value",
                            "conversions", "conversion_rate",
                        ],
                        "timeframe": {"key": "last_365_days"},
                        "conversion_metric_id": "Placed Order",
                        "filter": f'equals(flow_id,"{flow_id}")',
                    },
                }
            }
        )

        if resp and hasattr(resp, "data") and resp.data:
            return resp.data[0].attributes.get("statistics", {})
        return {}
    except Exception as e:
        logger.error(f"Failed to fetch metrics for flow {flow_id}: {e}")
        return {}


def fetch_metrics_overview(api_key, days=90):
    """
    Fetch high-level email metrics for the last N days.

    Returns a dict of aggregate metrics.
    """
    try:
        client = get_client(api_key)

        # Get aggregate metrics via the Metrics API
        resp = client.Metrics.get_metrics(
            page_size=50,
        )

        metrics_map = {}
        if resp and hasattr(resp, "data"):
            for m in resp.data:
                metrics_map[m.attributes.get("name", "")] = m.id

        logger.info(f"Found {len(metrics_map)} Klaviyo metrics")
        return metrics_map
    except Exception as e:
        logger.error(f"Failed to fetch Klaviyo metrics overview: {e}")
        return {}


def fetch_lists(api_key):
    """Fetch all lists/segments for subscriber counts."""
    try:
        client = get_client(api_key)
        lists = []
        resp = client.Lists.get_lists(page_size=50)

        if resp and hasattr(resp, "data"):
            for l in resp.data:
                attrs = l.attributes
                lists.append({
                    "id": l.id,
                    "name": attrs.get("name", ""),
                    "created": attrs.get("created", ""),
                    "updated": attrs.get("updated", ""),
                })

        logger.info(f"Fetched {len(lists)} Klaviyo lists")
        return lists
    except Exception as e:
        logger.error(f"Failed to fetch Klaviyo lists: {e}")
        return []
