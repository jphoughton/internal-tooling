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
        _filter = "equals(messages.channel,'email')"
        resp = client.Campaigns.get_campaigns(filter=_filter)

        while resp:
            if hasattr(resp, "data") and resp.data:
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
            # Follow cursor to next page
            if hasattr(resp, "links") and resp.links and resp.links.next:
                resp = client.Campaigns.get_campaigns(
                    filter=_filter, page_cursor=resp.links.next,
                )
            else:
                break

        logger.info(f"Fetched {len(campaigns)} Klaviyo campaigns")
        return campaigns
    except Exception as e:
        logger.error(f"Failed to fetch Klaviyo campaigns: {e}")
        raise


def fetch_flows(api_key):
    """
    Fetch all flows from Klaviyo (no status filter).

    Returns:
        list of dicts with flow data
    """
    try:
        client = get_client(api_key)
        flows = []
        resp = client.Flows.get_flows(sort="name")

        while resp:
            if hasattr(resp, "data") and resp.data:
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
            if hasattr(resp, "links") and resp.links and resp.links.next:
                resp = client.Flows.get_flows(
                    sort="name", page_cursor=resp.links.next,
                )
            else:
                break

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

        while resp:
            if hasattr(resp, "data") and resp.data:
                for l in resp.data:
                    attrs = l.attributes
                    lists.append({
                        "id": l.id,
                        "name": getattr(attrs, "name", ""),
                        "created": getattr(attrs, "created", ""),
                        "updated": getattr(attrs, "updated", ""),
                    })
            if hasattr(resp, "links") and resp.links and resp.links.next:
                resp = client.Lists.get_lists(page_cursor=resp.links.next)
            else:
                break

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
    "conversion_value", "revenue_per_recipient", "average_order_value", "conversion_rate",
]


def _klaviyo_headers(api_key):
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "revision": _REVISION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _remap_conversion_value(stats):
    """Remap conversion_value -> revenue so DB column names match."""
    out = dict(stats)
    if "conversion_value" in out:
        out["revenue"] = out.pop("conversion_value")
    return out


# Count stats that should be summed across flow messages
_SUM_STATS = {
    "recipients", "delivered", "opens_unique", "clicks_unique",
    "unsubscribes", "conversion_value",
}
# Rate stats that should be weighted-averaged by recipients
_RATE_STATS = {
    "open_rate", "click_rate", "click_to_open_rate",
    "unsubscribe_rate", "bounce_rate",
    "revenue_per_recipient", "average_order_value", "conversion_rate",
}


def _aggregate_message_stats(items):
    """Aggregate per-message stats into per-flow stats.

    Sums count metrics; weighted-averages rate metrics by recipients.
    """
    aggregated = {}
    total_recipients = 0
    for item in items:
        s = item.get("statistics", {})
        recip = s.get("recipients", 0) or 0
        total_recipients += recip
        for k in _SUM_STATS:
            aggregated[k] = aggregated.get(k, 0) + (s.get(k, 0) or 0)
        for k in _RATE_STATS:
            aggregated[k] = aggregated.get(k, 0) + (s.get(k, 0) or 0) * recip

    # Finish weighted averages
    if total_recipients > 0:
        for k in _RATE_STATS:
            aggregated[k] = aggregated[k] / total_recipients

    return aggregated


def _fetch_all_metrics(api_key):
    """Fetch all metrics from Klaviyo, paginating through results."""
    all_metrics = []
    url = f"{_KLAVIYO_API}/metrics"
    while url:
        resp = _requests.get(url, headers=_klaviyo_headers(api_key), timeout=15)
        resp.raise_for_status()
        body = resp.json()
        all_metrics.extend(body.get("data", []))
        url = body.get("links", {}).get("next")
    return all_metrics


def fetch_placed_order_metric_id(api_key):
    """Auto-discover the Klaviyo 'Placed Order' metric ID.

    The metrics endpoint doesn't support filtering by name, so we fetch all
    metrics and search for 'Placed Order' by name.

    Returns metric ID string, or None if not found.
    """
    try:
        for m in _fetch_all_metrics(api_key):
            name = m.get("attributes", {}).get("name", "")
            if name == "Placed Order":
                metric_id = m["id"]
                logger.info(f"Found Placed Order metric: {metric_id}")
                return metric_id
        logger.warning("Placed Order metric not found in Klaviyo")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch Placed Order metric: {e}")
        return None


def fetch_any_metric_id(api_key):
    """Fetch a metric ID suitable for the Reporting API.

    The Reporting API requires a conversion_metric_id that supports values
    queries. Searches for well-known order metrics, then falls back to any
    Shopify-integrated metric.

    Returns metric ID string, or None if no suitable metrics exist.
    """
    try:
        all_metrics = _fetch_all_metrics(api_key)
    except Exception as e:
        logger.error(f"Failed to fetch metrics: {e}")
        return None

    # Priority 1: well-known order/revenue metrics
    priority_names = {"Placed Order", "Ordered Product", "Fulfilled Order"}
    for m in all_metrics:
        name = m.get("attributes", {}).get("name", "")
        if name in priority_names:
            metric_id = m["id"]
            logger.info(f"Found conversion metric: {name} ({metric_id})")
            return metric_id

    # Priority 2: any Shopify-integrated metric (these support values queries)
    for m in all_metrics:
        integration = m.get("attributes", {}).get("integration", {})
        integ_name = integration.get("name", "") if integration else ""
        if integ_name == "Shopify":
            metric_id = m["id"]
            m_name = m.get("attributes", {}).get("name", "unknown")
            logger.info(f"Fallback Shopify metric: {m_name} ({metric_id})")
            return metric_id

    logger.warning("No suitable conversion metric found in Klaviyo")
    return None


def _build_report_body(report_type, stats, conversion_metric_id):
    """Build the JSON:API body for a Klaviyo reporting endpoint."""
    attrs = {
        "statistics": stats,
        "timeframe": {"key": "last_365_days"},
        "conversion_metric_id": conversion_metric_id,
    }
    return {"data": {"type": report_type, "attributes": attrs}}


def fetch_campaign_metrics(api_key, conversion_metric_id, include_revenue=True):
    """Fetch performance metrics for all email campaigns.

    Uses POST /api/campaign-values-reports (1 API call for all campaigns).
    Requires a valid conversion_metric_id (any metric works for engagement stats).

    Returns dict mapping campaign_id -> {metric_name: value, ...}
    """
    if not conversion_metric_id:
        logger.warning("No conversion_metric_id — skipping campaign metrics")
        return {}

    stats = list(_ENGAGEMENT_STATS)
    if include_revenue:
        stats += _REVENUE_STATS

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
            results[campaign_id] = _remap_conversion_value(item.get("statistics", {}))

    logger.info(f"Fetched metrics for {len(results)} campaigns")
    return results


def _query_metric_by_flow(api_key, metric_id, measurement="count"):
    """Query a single Klaviyo metric aggregated by $flow over the last year.

    Returns dict mapping flow_id -> total count/unique for that metric.
    """
    from datetime import datetime, timedelta
    end = datetime.utcnow()
    start = end - timedelta(days=364)
    body = {
        "data": {
            "type": "metric-aggregate",
            "attributes": {
                "metric_id": metric_id,
                "measurements": [measurement],
                "interval": "month",
                "page_size": 500,
                "by": ["$flow"],
                "filter": [
                    f"greater-or-equal(datetime,{start.strftime('%Y-%m-%d')})",
                    f"less-than(datetime,{end.strftime('%Y-%m-%d')})",
                ],
            },
        }
    }
    resp = _requests.post(
        f"{_KLAVIYO_API}/metric-aggregates",
        headers=_klaviyo_headers(api_key),
        json=body,
        timeout=30,
    )
    if not resp.ok:
        logger.error(f"Metric aggregates API {resp.status_code}: {resp.text[:500]}")
        return {}

    totals = {}
    for row in resp.json().get("data", {}).get("attributes", {}).get("data", []):
        flow_id = (row.get("dimensions") or [""])[0]
        if not flow_id:
            continue
        values = row.get("measurements", {}).get(measurement, [])
        totals[flow_id] = sum(v for v in values if v)
    return totals


# Metric names -> their Klaviyo metric IDs (discovered at sync time)
_FLOW_METRIC_NAMES = {
    "Received Email": "recipients",
    "Opened Email": "opens_unique",
    "Clicked Email": "clicks_unique",
    "Bounced Email": "bounced",
    "Unsubscribed": "unsubscribes",
}


def _fetch_flow_revenue(api_key, conversion_metric_id):
    """Fetch per-flow revenue via flow-values-reports.

    The metric-aggregates API doesn't attribute Placed Order revenue to
    individual flows, so we use the flow-values-reports endpoint which
    does handle attribution (though it covers fewer flows).

    Returns dict mapping flow_id -> total revenue.
    """
    body = _build_report_body(
        "flow-values-report",
        ["conversion_value", "revenue_per_recipient"],
        conversion_metric_id,
    )
    resp = _requests.post(
        f"{_KLAVIYO_API}/flow-values-reports",
        headers=_klaviyo_headers(api_key),
        json=body,
        timeout=30,
    )
    if not resp.ok:
        logger.error(f"Flow revenue API {resp.status_code}: {resp.text[:500]}")
        return {}

    # Aggregate per-message results into per-flow totals
    flow_revenue = {}
    for item in resp.json().get("data", {}).get("attributes", {}).get("results", []):
        flow_id = item.get("groupings", {}).get("flow_id")
        if not flow_id:
            continue
        rev = item.get("statistics", {}).get("conversion_value", 0) or 0
        flow_revenue[flow_id] = flow_revenue.get(flow_id, 0) + rev

    logger.info(f"Fetched revenue for {len(flow_revenue)} flows")
    return flow_revenue


def fetch_flow_metrics(api_key, conversion_metric_id, include_revenue=True):
    """Fetch performance metrics for all flows via metric-aggregates API.

    The flow-values-reports endpoint only returns a subset of flows.
    Instead, we query each engagement metric (Received Email, Opened Email,
    etc.) aggregated by $flow, which returns data for ALL flows.

    Returns dict mapping flow_id -> {metric_name: value, ...}
    """
    import time

    # Discover metric IDs
    try:
        all_metrics = _fetch_all_metrics(api_key)
    except Exception as e:
        logger.error(f"Failed to fetch metrics list: {e}")
        return {}

    metric_ids = {}
    for m in all_metrics:
        name = m.get("attributes", {}).get("name", "")
        if name in _FLOW_METRIC_NAMES:
            metric_ids[name] = m["id"]
        if name == "Placed Order":
            metric_ids["Placed Order"] = m["id"]

    # Query each metric aggregated by flow
    results = {}
    for metric_name, db_field in _FLOW_METRIC_NAMES.items():
        mid = metric_ids.get(metric_name)
        if not mid:
            continue
        totals = _query_metric_by_flow(api_key, mid, measurement="unique")
        for flow_id, val in totals.items():
            results.setdefault(flow_id, {})[db_field] = val
        time.sleep(0.5)  # respect rate limits

    # Delivered ≈ recipients - bounced
    for flow_id, stats in results.items():
        recip = stats.get("recipients", 0)
        bounced = stats.get("bounced", 0)
        stats["delivered"] = max(0, recip - bounced)
        stats.pop("bounced", None)

    # Compute rates
    for flow_id, stats in results.items():
        recip = stats.get("recipients", 0)
        delivered = stats.get("delivered", 0)
        if recip > 0:
            stats["open_rate"] = stats.get("opens_unique", 0) / recip
            stats["click_rate"] = stats.get("clicks_unique", 0) / recip
            stats["unsubscribe_rate"] = stats.get("unsubscribes", 0) / recip
            stats["bounce_rate"] = (recip - delivered) / recip
        opens = stats.get("opens_unique", 0)
        if opens > 0:
            stats["click_to_open_rate"] = stats.get("clicks_unique", 0) / opens

    # Revenue via flow-values-reports (metric-aggregates doesn't attribute
    # Placed Order revenue to individual flows)
    if include_revenue and conversion_metric_id:
        time.sleep(0.5)
        try:
            rev_by_flow = _fetch_flow_revenue(api_key, conversion_metric_id)
            for flow_id, rev in rev_by_flow.items():
                results.setdefault(flow_id, {})["revenue"] = rev
            # Revenue per recipient
            for flow_id, stats in results.items():
                recip = stats.get("recipients", 0)
                rev = stats.get("revenue", 0)
                if recip > 0 and rev > 0:
                    stats["revenue_per_recipient"] = rev / recip
        except Exception as e:
            logger.warning(f"Failed to fetch flow revenue: {e}")

    logger.info(f"Fetched metrics for {len(results)} flows")
    return results
