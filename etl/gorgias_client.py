"""
Gorgias API integration — fetches support tickets (conversations) and messages.

In Gorgias, a "conversation" == a ticket; message bodies live on a per-ticket
sub-resource. Pagination is cursor-based (NOT offset).

Docs: https://developers.gorgias.com/reference/introduction
Auth: HTTP Basic — username = login email, password = REST API key.
Base: https://{domain}.gorgias.com/api
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

# Gorgias default REST limit is ~2 req/sec. Sleep keeps us comfortably under.
_THROTTLE_SECONDS = 0.55
_MAX_RETRIES = 5


def _base_url(domain):
    return f"https://{domain}.gorgias.com/api"


def _session(email, api_key):
    s = requests.Session()
    s.auth = (email, api_key)
    s.headers.update({"Accept": "application/json"})
    return s


def test_connection(domain, email, api_key):
    """Test the Gorgias API connection. Returns (ok, message)."""
    try:
        s = _session(email, api_key)
        r = s.get(f"{_base_url(domain)}/tickets", params={"limit": 1}, timeout=30)
        if r.status_code == 401:
            return False, "Auth failed — check email + API key"
        r.raise_for_status()
        return True, f"Connected to {domain}.gorgias.com"
    except Exception as e:
        return False, f"Connection failed: {e}"


def _get(session, url, params):
    """GET with retry on 429 / 5xx, honoring Retry-After."""
    for attempt in range(_MAX_RETRIES):
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("Retry-After", 2 ** attempt))
            logger.warning("Gorgias %s on %s — backing off %.1fs", r.status_code, url, wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def _paginate(session, url, params=None):
    """Yield every row across cursor-paginated pages."""
    params = dict(params or {})
    params.setdefault("limit", 100)
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        body = _get(session, url, params)
        for row in body.get("data", []):
            yield row
        cursor = (body.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(_THROTTLE_SECONDS)


def fetch_tickets(domain, email, api_key, since=None, until=None):
    """
    Fetch the ticket (conversation) list within a created-date window.

    The list payload already carries subject, excerpt, tags, status, channel,
    and timestamps — enough for population-level analysis without per-ticket
    message calls. Use fetch_messages_for() to enrich a subset with full bodies.

    Args:
        domain: subdomain before .gorgias.com (e.g. "drinkhydrant")
        email:  login email used for basic auth
        api_key: REST API key
        since:  datetime — only keep tickets created on/after this
        until:  datetime — only keep tickets created on/before this

    Returns:
        list[dict] tickets, newest first.
    """
    s = _session(email, api_key)
    base = _base_url(domain)

    tickets = []
    # Order newest-first so we can stop early once we pass the `since` boundary.
    params = {"order_by": "created_datetime:desc", "limit": 100}
    for t in _paginate(s, f"{base}/tickets", params):
        created = _parse_dt(t.get("created_datetime"))
        if since and created and created < since:
            break  # newest-first => everything after this is older too
        if until and created and created > until:
            continue
        tickets.append(t)

    logger.info("Fetched %d tickets in window", len(tickets))
    return tickets


def fetch_messages_for(domain, email, api_key, tickets):
    """Enrich the given tickets in-place with a "messages" list. Mutates & returns."""
    s = _session(email, api_key)
    base = _base_url(domain)
    n = len(tickets)
    for i, t in enumerate(tickets, 1):
        t["messages"] = list(_paginate(s, f"{base}/tickets/{t['id']}/messages"))
        if i % 25 == 0 or i == n:
            logger.info("  messages: %d/%d tickets", i, n)
        time.sleep(_THROTTLE_SECONDS)
    return tickets


def _parse_dt(value):
    """Parse a Gorgias ISO8601 timestamp into a naive UTC datetime."""
    if not value:
        return None
    from datetime import datetime

    v = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        # Trim fractional seconds beyond microseconds if present
        v = v[:26] + v[v.find("+"):] if "+" in v else v[:26]
        dt = datetime.fromisoformat(v)
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz=None).replace(tzinfo=None)
    return dt
