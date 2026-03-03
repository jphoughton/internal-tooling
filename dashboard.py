"""
Streamlit Dashboard for Inventory Demand Forecasting.
Thin router: dispatches to page modules in pages/.
"""
import logging
import os
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from db import (
    get_db, read_sql,
    get_media_spend, get_amazon_revenue_forecast,
    get_seasonal_indices, get_sku_seasonal_indices,
    get_setting,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
    get_inventory_snapshot, save_inventory_snapshot,
)
from analytics.waterfall import (
    get_active_sources,
    get_configured_sources,
    get_average_retention_curve,
    get_aov_and_units,
    build_waterfall,
    build_sku_forecast_table,
    clear_waterfall_cache,
)
from analytics.sku_flavors import get_flavor
from ui.styles import inject_global_styles, get_nav_section_css
from ui.components import check_password
from ui.business_vars import get_business_vars


# --- Cached wrappers for expensive computations ---
# Data updates once daily (6AM sync). All caches use 24h TTL and are
# explicitly cleared after sync / refresh / settings edits.
_CACHE_TTL = 86400  # 24 hours — data only changes once per day
_CACHE_VERSION = 'v2'  # bump to force cache invalidation on deploy

@st.cache_data(ttl=_CACHE_TTL)
def _cached_retention_curve(source_filter):
    return get_average_retention_curve(source_filter)

@st.cache_data(ttl=_CACHE_TTL)
def _cached_aov_and_units(source_filter):
    return get_aov_and_units(source_filter)

@st.cache_data(ttl=_CACHE_TTL)
def _cached_waterfall(media_plan_json, source_filter, horizon_months, seasonal_json=None):
    """Waterfall depends on media plan + seasonality, keyed on both JSON strings."""
    import json
    media_plan = json.loads(media_plan_json)
    seasonal = json.loads(seasonal_json) if seasonal_json else None
    # seasonal comes back as {"1": 0.95, ...} — convert keys to int
    if seasonal:
        seasonal = {int(k): v for k, v in seasonal.items()}
    return build_waterfall(media_plan, source_filter=source_filter,
                           horizon_months=horizon_months,
                           seasonal_indices=seasonal)

@st.cache_data(ttl=_CACHE_TTL)
def _cached_sku_forecast(waterfall_json, source_filter, sku_seasonal_json=None, global_seasonal_json=None):
    """SKU forecast depends on waterfall output + per-SKU seasonal indices."""
    import json
    df = pd.read_json(waterfall_json) if waterfall_json else pd.DataFrame()
    sku_seasonal = None
    global_seasonal = None
    if sku_seasonal_json:
        raw = json.loads(sku_seasonal_json)
        sku_seasonal = {sku: {int(k): v for k, v in months.items()} for sku, months in raw.items()}
    if global_seasonal_json:
        raw = json.loads(global_seasonal_json)
        global_seasonal = {int(k): v for k, v in raw.items()}
    return build_sku_forecast_table(df, source_filter=source_filter,
                                    sku_seasonal_indices=sku_seasonal,
                                    global_seasonal_indices=global_seasonal)

from utils.constants import FORECAST_SKUS


# --- Cached wrappers for queries that previously ran every rerun ---
@st.cache_data(ttl=_CACHE_TTL)
def _cached_active_sources():
    return get_active_sources()

@st.cache_data(ttl=_CACHE_TTL)
def _cached_configured_sources():
    return get_configured_sources()

@st.cache_data(ttl=_CACHE_TTL)
def _cached_business_vars():
    return get_business_vars()

@st.cache_data(ttl=_CACHE_TTL)
def _cached_media_spend():
    with get_db() as conn:
        return get_media_spend(conn, source='All Sources')

@st.cache_data(ttl=_CACHE_TTL)
def _cached_amz_rev_forecast():
    with get_db() as conn:
        return get_amazon_revenue_forecast(conn)

@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _cached_last_sync_ts():
    """Last successful sync timestamp — used by auto-sync check."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) as last_ts FROM sync_log WHERE status = 'success'"
            ).fetchone()
            return row['last_ts'] if row else None
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Startup validation — runs once per process on first Streamlit boot
# ---------------------------------------------------------------------------
_STARTUP_DONE = False

# Env vars that MUST be present for production operation.
# If running on Railway (RAILWAY_ENVIRONMENT is set), DATABASE_URL is required —
# falling back to SQLite is not appropriate in a deployed environment.
_REQUIRED_ENV_VARS: list[str] = (
    ["DATABASE_URL"] if os.environ.get("RAILWAY_ENVIRONMENT") else []
)

# Env vars that are notable but not fatal if missing — they can all be
# configured post-boot via the Settings page and are stored in the DB.
_NOTABLE_ENV_VARS = [
    "SHOPIFY_ACCESS_TOKEN",
    "SHOPIFY_STORE_URL",
    "AMAZON_REFRESH_TOKEN",
    "AMAZON_LWA_CLIENT_ID",
    "AMAZON_LWA_CLIENT_SECRET",
]

_log = logging.getLogger(__name__)


def _run_startup_validation() -> dict:
    """Validate DB connection, env vars, and log a startup banner.

    Returns a dict with keys:
        db_ok (bool), missing_required (list[str]), missing_notable (list[str]),
        warnings (list[str])
    """
    import config as _cfg

    # --- Startup banner ---
    _log.info("=" * 60)
    _log.info("  Hydrant Command Center  v%s", _cfg.BUILD_VERSION)
    _log.info("  %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
    _log.info("=" * 60)

    # --- Config summary ---
    db_url = os.environ.get("DATABASE_URL", "")
    _log.info("[config] DATABASE_URL   : %s", "set" if db_url else "NOT SET (SQLite fallback)")
    _log.info("[config] DEBUG          : %s", _cfg.DEBUG)
    _log.info("[config] SYNC_HOUR      : %s:00 %s", _cfg.SYNC_HOUR, _cfg.SYNC_TIMEZONE)
    _log.info("[config] FORECAST_HORIZON: %d days", _cfg.FORECAST_HORIZON_DAYS)
    shopify_set = bool(_cfg.SHOPIFY_STORE_URL and _cfg.SHOPIFY_ACCESS_TOKEN)
    amazon_set = bool(_cfg.AMAZON_REFRESH_TOKEN and _cfg.AMAZON_LWA_CLIENT_ID)
    _log.info("[config] Shopify creds  : %s", "configured" if shopify_set else "missing")
    _log.info("[config] Amazon creds   : %s", "configured" if amazon_set else "missing")

    # --- Required env var check (fatal) ---
    missing_required = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing_required:
        _log.error("[startup] MISSING required env vars: %s", ", ".join(missing_required))
    else:
        if _REQUIRED_ENV_VARS:
            _log.info("[startup] All required env vars are set")

    # --- Notable env var check (warnings only — configurable via Settings UI) ---
    missing_notable = [v for v in _NOTABLE_ENV_VARS if not os.environ.get(v)]
    if missing_notable:
        _log.warning("[startup] Missing API credential vars (configure via Settings): %s", ", ".join(missing_notable))
    else:
        _log.info("[startup] All notable env vars are set")

    # --- DB connection check (fatal) ---
    db_ok = False
    warnings = []
    try:
        from db import get_db
        with get_db() as conn:
            conn.execute("SELECT 1")
        db_ok = True
        _log.info("[startup] Database connection: OK")
    except Exception as exc:
        msg = f"Database connection failed: {exc}"
        _log.error("[startup] %s", msg)
        warnings.append(msg)

    _log.info("=" * 60)
    return {
        "db_ok": db_ok,
        "missing_required": missing_required,
        "missing_notable": missing_notable,
        "warnings": warnings,
    }


def _maybe_run_startup() -> None:
    """Run startup validation exactly once per process; surface critical errors in UI."""
    global _STARTUP_DONE
    if _STARTUP_DONE:
        return
    _STARTUP_DONE = True

    # Ensure logging is configured (Streamlit may not set up a handler)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    result = _run_startup_validation()

    # --- Fail fast: missing required env vars ---
    if result["missing_required"]:
        st.error(
            "**Missing required environment variables:** "
            + ", ".join(f"`{v}`" for v in result["missing_required"])
            + ". Set these in your Railway environment and redeploy.",
            icon="🚨",
        )
        st.stop()

    # --- Fail fast: DB unreachable ---
    if not result["db_ok"]:
        st.error(
            "**Database connection failed at startup.** "
            "Check `DATABASE_URL` in your environment variables. "
            f"Detail: {result['warnings'][0] if result['warnings'] else 'unknown error'}",
            icon="🚨",
        )
        st.stop()


@st.cache_data(ttl=_CACHE_TTL)
def _load_seasonal_json():
    """Load seasonal indices from DB and return as JSON string for cache key, or None if disabled."""
    import json as _json_s
    with get_db() as conn:
        enabled = get_setting(conn, "seasonality_enabled", "true")
        if enabled != "true":
            return None
        indices = get_seasonal_indices(conn)
    if not indices:
        return None
    return _json_s.dumps(indices, sort_keys=True)


@st.cache_data(ttl=_CACHE_TTL)
def _load_sku_seasonal_json():
    """Load per-SKU seasonal indices from DB as JSON string for cache key, or None if empty."""
    import json as _json_s
    with get_db() as conn:
        enabled = get_setting(conn, "seasonality_enabled", "true")
        if enabled != "true":
            return None
        indices = get_sku_seasonal_indices(conn)
    if not indices:
        return None
    return _json_s.dumps(indices, sort_keys=True)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _cached_3pl_inventory():
    """Cached wrapper for Packiyo 3PL inventory — reads DB snapshot first, falls back to live API."""
    import config as _cfg
    if not _cfg.PACKIYO_API_TOKEN:
        return None
    # Try DB snapshot first (written by 6AM scheduler)
    try:
        with get_db() as conn:
            snapshot = get_inventory_snapshot(conn, 'packiyo', max_age_hours=24)
        if snapshot is not None:
            return snapshot
    except Exception:
        pass
    # Fall back to live API
    from etl.packiyo_client import get_inventory
    result = get_inventory()
    # Write back to snapshot for next caller
    if result is not None:
        try:
            with get_db() as conn:
                save_inventory_snapshot(conn, 'packiyo', result)
        except Exception:
            pass
    return result


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _cached_amazon_inventory():
    """Cached wrapper for Amazon FBA inventory — reads DB snapshot first, falls back to live API."""
    import config as _cfg
    if not all(getattr(_cfg, k, '') for k in [
        'AMAZON_REFRESH_TOKEN', 'AMAZON_LWA_CLIENT_ID', 'AMAZON_LWA_CLIENT_SECRET',
    ]):
        return None
    # Try DB snapshot first (written by 6AM scheduler)
    try:
        with get_db() as conn:
            snapshot = get_inventory_snapshot(conn, 'amazon', max_age_hours=24)
        if snapshot is not None:
            return snapshot
    except Exception:
        pass
    # Fall back to live API
    from etl.amazon_inventory import get_inventory
    result = get_inventory()
    # Write back to snapshot for next caller
    if result is not None:
        try:
            with get_db() as conn:
                save_inventory_snapshot(conn, 'amazon', result)
        except Exception:
            pass
    return result


st.set_page_config(
    page_title="Hydrant — Command Center",
    page_icon="\U0001F4A7",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Run once per process after page config is set (st.error is now safe to call)
_maybe_run_startup()

# --- Password protection (Railway / production) ---
if not check_password():
    st.stop()

inject_global_styles()


# --- Sidebar ---
import os as _os
_logo_path = _os.path.join(_os.path.dirname(__file__), 'assets', 'hydrant-logo.svg')
with open(_logo_path) as _f:
    _logo_svg = _f.read()
st.sidebar.markdown(
    f'<div style="padding:8px 0 0;">{_logo_svg}</div>',
    unsafe_allow_html=True,
)
st.sidebar.caption("Command Center")

# ── Navigation with section grouping (channel-first) ──
_NAV_ICONS = {
    'DTC Overview': '\U0001F4CA', 'DTC Marketing': '\U0001F4C8', 'DTC Retention': '\U0001F504', 'DTC Ops': '\U0001F4E6',
    'Amazon Overview': '\U0001F4CA', 'Amazon Marketing': '\U0001F4C8', 'Amazon Retention': '\U0001F504', 'Amazon Ops': '\U0001F4E6',
    'Rollup Overview': '\U0001F4CA', 'Rollup Marketing': '\U0001F4C8', 'Rollup Retention': '\U0001F504', 'Rollup Ops': '\U0001F4E6',
    'Financials': '\U0001F4B0', 'Settings': '\u2699\uFE0F', 'Variables': '\U0001F4DD',
}

_NAV_GROUPS = [
    ('DTC', ['DTC Overview', 'DTC Marketing', 'DTC Retention', 'DTC Ops']),
    ('Amazon', ['Amazon Overview', 'Amazon Marketing', 'Amazon Retention', 'Amazon Ops']),
    ('Rollup', ['Rollup Overview', 'Rollup Marketing', 'Rollup Retention', 'Rollup Ops']),
    ('Finance & Config', ['Financials', 'Settings', 'Variables']),
]

_STANDALONE_PAGES = {'Financials', 'Settings', 'Variables'}

# Legacy URL redirect map (old page names → new)
_LEGACY_PAGE_MAP = {
    'Overview': 'Rollup Overview', 'Marketing': 'Rollup Marketing',
    'Retention': 'Rollup Retention', 'Demand Forecast': 'Rollup Ops',
    'Projected Inventory': 'Rollup Ops', 'Reorder Alerts': 'Rollup Ops',
    'FBA Transfers': 'Amazon Ops', '3PL Inventory': 'DTC Ops',
    'Amazon Inventory': 'Amazon Ops',
}

def _nav_format(x):
    """Strip channel prefix for display: 'DTC Overview' → '📊  Overview'."""
    icon = _NAV_ICONS.get(x, '')
    if x in _STANDALONE_PAGES:
        return f'{icon}  {x}'
    parts = x.split(' ', 1)
    return f'{icon}  {parts[1]}' if len(parts) == 2 else f'{icon}  {x}'

# Flatten all pages for the single radio
_ALL_NAV_PAGES = []
for _grp_name, _grp_pages in _NAV_GROUPS:
    _ALL_NAV_PAGES.extend(_grp_pages)

# Restore page from URL query params on first load only.
# We do NOT sync back to query_params on navigation because setting
# st.query_params triggers a rerun that can lose session state,
# causing the radio to revert to the URL's page.
if '_nav_radio' not in st.session_state:
    _default_page = st.query_params.get('page', 'DTC Overview')
    # Handle legacy bookmarks
    if _default_page in _LEGACY_PAGE_MAP:
        _default_page = _LEGACY_PAGE_MAP[_default_page]
    if _default_page not in _ALL_NAV_PAGES:
        _default_page = 'DTC Overview'
    st.session_state['_nav_radio'] = _default_page

# Single radio drives actual selection (hidden, styled via CSS)
page = st.sidebar.radio(
    'Navigate',
    _ALL_NAV_PAGES,
    format_func=_nav_format,
    label_visibility='collapsed',
    key='_nav_radio',
)

# Extract channel and page type from the selected page
if page in _STANDALONE_PAGES:
    _channel = None
    _source_filter = None
    _page_type = page
else:
    _channel, _page_type = page.split(' ', 1)
    _source_filter = {'DTC': 'shopify', 'Amazon': 'amazon', 'Rollup': None}[_channel]

# Inject section headers via JS/CSS after the radio renders
_section_js_map = {}
_idx = 0
for _grp_name, _grp_pages in _NAV_GROUPS:
    _section_js_map[_idx] = _grp_name
    _idx += len(_grp_pages)

st.sidebar.markdown(get_nav_section_css(_NAV_GROUPS), unsafe_allow_html=True)

try:
    active_sources = _cached_active_sources()
except Exception:
    active_sources = []

try:
    configured_sources = _cached_configured_sources()
except Exception:
    configured_sources = []

st.sidebar.markdown("---")
if configured_sources:
    _src_icons = {"shopify": "\U0001F7E2", "amazon": "\U0001F7E0"}
    _default_icon = "\u25CF"
    _src_labels = " ".join(f"{_src_icons.get(s, _default_icon)} {s.title()}" for s in configured_sources)
    st.sidebar.caption(f"Connected: {_src_labels}")
else:
    st.sidebar.caption("\u2699\uFE0F No sources \u2014 go to Settings")

# --- Auto-sync: trigger if no successful sync since 5 AM PST today ---
if configured_sources:
    _last_ts = _cached_last_sync_ts()
    _needs_auto_sync = False
    if _last_ts:
        try:
            from datetime import datetime as _dt_auto
            from zoneinfo import ZoneInfo as _ZI
            from config import SYNC_HOUR as _SH, SYNC_TIMEZONE as _STZ
            _pst = _ZI(_STZ)
            _now_pst = _dt_auto.now(_pst)
            _today_sync_cutoff = _now_pst.replace(hour=_SH, minute=0, second=0, microsecond=0)
            _last_dt = _dt_auto.strptime(_last_ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ZI('UTC'))
            _needs_auto_sync = _now_pst >= _today_sync_cutoff and _last_dt < _today_sync_cutoff
        except (ValueError, TypeError):
            _needs_auto_sync = True
    else:
        _needs_auto_sync = True  # Never synced before

    if _needs_auto_sync and "auto_sync_done" not in st.session_state:
        st.session_state["auto_sync_done"] = True
        st.sidebar.info("\u23F3 Auto-syncing (no sync since 5 AM today)...")
        try:
            from etl.sync import run_daily_sync as _auto_sync
            _auto_results = _auto_sync(full_refresh=False)
            clear_waterfall_cache()
            st.cache_data.clear()
            if _auto_results:
                _auto_ok = [k for k, v in _auto_results.items() if not str(v).startswith("ERROR")]
                if _auto_ok:
                    st.sidebar.success(f"Auto-sync complete: {', '.join(_auto_ok)}")
        except Exception as _auto_err:
            st.sidebar.warning(f"Auto-sync failed: {_auto_err}")

# Sync button
if st.sidebar.button("Refresh Data"):
    if not configured_sources:
        st.sidebar.error("No API sources configured. Go to **Settings** to connect Shopify or Amazon.")
    else:
        from etl.sync import run_daily_sync

        needs_full_refresh = False
        try:
            with get_db() as conn:
                has_data = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            if has_data == 0:
                needs_full_refresh = True
        except Exception:
            needs_full_refresh = True

        sync_container = st.sidebar.container()
        sync_container.info("Syncing data..." if not needs_full_refresh else "Running full data sync (this may take a few minutes)...")
        sync_progress = st.sidebar.progress(0, text="Starting sync...")

        def _on_sync_status(step, total, message):
            pct = min(step / max(total, 1), 1.0)
            sync_progress.progress(pct, text=message)

        results = run_daily_sync(full_refresh=needs_full_refresh, on_status=_on_sync_status)
        sync_progress.empty()
        sync_container.empty()

        if results:
            successes = [k for k, v in results.items() if not str(v).startswith("ERROR")]
            errors = [k for k, v in results.items() if str(v).startswith("ERROR")]
            if successes:
                order_counts = [str(results[k]) for k in successes if isinstance(results[k], int)]
                detail = f" ({', '.join(f'{c} orders' for c in order_counts)})" if order_counts else ""
                st.sidebar.success(f"Sync complete!{detail}")
            if errors:
                for k in errors:
                    err_msg = str(results[k]).replace("ERROR: ", "")
                    st.sidebar.error(f"**{k.title()} sync failed:** {err_msg}")
            # Only rerun if we got some successes (otherwise keep errors visible)
            if successes:
                clear_waterfall_cache()
                st.cache_data.clear()
                st.rerun()
        else:
            st.sidebar.warning("No sources configured to sync.")


# --- Helper: load SKU list ---
@st.cache_data(ttl=_CACHE_TTL)
def load_sku_list():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT sku, product_name, category, sources FROM sku_master WHERE is_active = 1 ORDER BY category, sku"
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=_CACHE_TTL)
def load_overview_stats(date_start=None, date_end=None):
    date_clause = ""
    date_clause_orders = ""
    params_d = []
    if date_start and date_end:
        date_clause = "AND sale_date BETWEEN ? AND ?"
        date_clause_orders = "AND order_date BETWEEN ? AND ?"
        params_d = [str(date_start), str(date_end)]

    with get_db() as conn:
        total_orders = conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE 1=1 {date_clause_orders}", params_d
        ).fetchone()[0]
        total_customers = conn.execute(
            f"SELECT COUNT(DISTINCT customer_id) FROM orders WHERE 1=1 {date_clause_orders}", params_d
        ).fetchone()[0]

        # Revenue by source — use daily_sku_sales (has both Shopify & Amazon)
        # The orders table only has Shopify; Amazon sales go directly to daily_sku_sales
        rev_by_source = {}
        for r in conn.execute(
            f"SELECT source, SUM(revenue) as rev FROM daily_sku_sales WHERE 1=1 {date_clause} GROUP BY source",
            params_d,
        ).fetchall():
            rev_by_source[r["source"]] = r["rev"] or 0
        total_revenue = sum(rev_by_source.values())

        total_skus = conn.execute("SELECT COUNT(*) FROM sku_master WHERE is_active = 1").fetchone()[0]

        # Source split — use daily_sku_sales for consistent revenue across channels
        source_split = read_sql(
            f"SELECT source, SUM(order_count) as orders, SUM(revenue) as revenue FROM daily_sku_sales WHERE 1=1 {date_clause} GROUP BY source",
            conn, params=params_d,
        )

        # Top SKUs
        top_skus = read_sql(f"""
            SELECT oi.sku, sm.product_name, sm.category,
                   SUM(oi.quantity) as total_units,
                   SUM(oi.total_price) as total_revenue
            FROM order_items oi
            JOIN sku_master sm ON oi.sku = sm.sku
            JOIN orders o ON oi.order_id = o.order_id
            WHERE 1=1 {date_clause_orders.replace('order_date', 'o.order_date')}
            GROUP BY oi.sku, sm.product_name, sm.category
            ORDER BY total_units DESC
            LIMIT 10
        """, conn, params=params_d)

        # Daily trend by source (for stacked revenue chart)
        daily_trend = read_sql(f"""
            SELECT sale_date, source,
                   SUM(units_sold) as units, SUM(revenue) as revenue
            FROM daily_sku_sales
            WHERE 1=1 {date_clause}
            GROUP BY sale_date, source
            ORDER BY sale_date
        """, conn, params=params_d)

    return {
        "total_orders": total_orders,
        "total_customers": total_customers,
        "total_revenue": total_revenue,
        "total_skus": total_skus,
        "shopify_revenue": rev_by_source.get("shopify", 0),
        "amazon_revenue": rev_by_source.get("amazon", 0),
        "source_split": source_split,
        "top_skus": top_skus,
        "daily_trend": daily_trend,
    }


# ================================================================
# GLOBAL NOTIFICATION BAR — Reorder & FBA Transfer Alerts
# ================================================================
@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _compute_global_alerts():
    """Compute reorder & FBA transfer urgency alerts for the notification bar."""
    alerts = {"reorder": [], "transfer": []}
    try:
        from analytics.reorder import build_reorder_plan
        import json as _json_alert

        # Load forecast
        _seasonal_alert = _load_seasonal_json()
        with get_db() as conn:
            _media_alert = get_media_spend(conn, source="All Sources")
        if not _media_alert:
            return alerts

        _wf_alert = _cached_waterfall(
            _json_alert.dumps(_media_alert, sort_keys=True), None, 12, _seasonal_alert
        )
        if _wf_alert is None or _wf_alert.empty:
            return alerts

        _sku_alert = _cached_sku_forecast(_wf_alert.to_json(), None, _load_sku_seasonal_json(), _seasonal_alert)
        if _sku_alert.empty:
            return alerts
        _sku_alert = _sku_alert[_sku_alert["SKU"].isin(FORECAST_SKUS)].copy()
        if "Variant" in _sku_alert.columns:
            _sku_alert = _sku_alert.drop(columns=["Variant"])
        if "Flavor" not in _sku_alert.columns:
            _sku_alert.insert(1, "Flavor", _sku_alert["SKU"].map(lambda s: get_flavor(s)))

        # Load inventory (use cached API wrappers)
        combined_inv = {}
        try:
            _3pl_items = _cached_3pl_inventory()
            if not _3pl_items:
                _3pl_items = []
            for item in _3pl_items:
                sku = item["sku"]
                if sku in FORECAST_SKUS:
                    combined_inv.setdefault(sku, {"sku": sku, "name": item.get("name", ""),
                                                   "quantity_available": 0, "quantity_on_hand": 0,
                                                   "quantity_inbound": 0, "3pl_available": 0,
                                                   "fba_fulfillable": 0})
                    combined_inv[sku]["3pl_available"] = int(float(item.get("quantity_available", 0) or 0))
                    combined_inv[sku]["quantity_available"] += combined_inv[sku]["3pl_available"]
                    combined_inv[sku]["quantity_inbound"] += int(float(item.get("quantity_inbound", 0) or 0))
        except Exception:
            pass

        _amz_inv_items = []
        try:
            _amz_inv_items = _cached_amazon_inventory() or []
            for item in _amz_inv_items:
                sku = item["sku"]
                if sku in FORECAST_SKUS:
                    combined_inv.setdefault(sku, {"sku": sku, "name": item.get("name", ""),
                                                   "quantity_available": 0, "quantity_on_hand": 0,
                                                   "quantity_inbound": 0, "3pl_available": 0,
                                                   "fba_fulfillable": 0})
                    fba_qty = int(float(item.get("total_quantity", 0) or 0))
                    combined_inv[sku]["fba_fulfillable"] = fba_qty
                    combined_inv[sku]["quantity_available"] += fba_qty
        except Exception:
            pass

        if not combined_inv:
            return alerts

        inv_data = list(combined_inv.values())

        # Build reorder plan
        with get_db() as conn:
            _planned = {}
            try:
                _pi_rows = conn.execute("SELECT sku, month, units FROM planned_inbound").fetchall()
                for r in _pi_rows:
                    _planned.setdefault(r[0], {})[r[1]] = int(r[2])
            except Exception:
                pass

        _bv_alert = get_business_vars()
        reorder_df, _ = build_reorder_plan(
            sku_forecast_table=_sku_alert,
            inventory_data=inv_data,
            lead_time_weeks=_bv_alert['lead_time_weeks'],
            moq=_bv_alert['moq_units'],
            safety_weeks=_bv_alert['safety_buffer_weeks'],
            forecast_skus=FORECAST_SKUS,
            planned_inbound=_planned,
        )

        if not reorder_df.empty:
            for _, row in reorder_df.iterrows():
                urgency = row.get("Urgency", "OK")
                if urgency in ("OVERDUE", "ORDER NOW", "ORDER SOON", "EN ROUTE \u26A0\uFE0F"):
                    days = row.get("Days Until Reorder")
                    alerts["reorder"].append({
                        "sku": row.get("SKU", ""),
                        "flavor": row.get("Flavor", ""),
                        "days": int(days) if pd.notna(days) else None,
                        "urgency": urgency,
                        "qty": int(row.get("Order Qty", 0)),
                    })

        # FBA transfer alerts
        if _amz_inv_items:
            with get_db() as conn:
                _amz_dem = read_sql("""
                    SELECT sku, SUM(units_sold) / 3.0 as monthly_demand
                    FROM daily_sku_sales
                    WHERE source = 'amazon' AND sale_date >= date('now', '-90 days')
                    GROUP BY sku
                """, conn)
            _amz_dem_map = dict(zip(_amz_dem["sku"], _amz_dem["monthly_demand"])) if not _amz_dem.empty else {}
            _today = datetime.utcnow().date()
            _xfer_lt = _bv_alert['fba_transfer_lt_weeks'] * 7

            for item in _amz_inv_items:
                sku = item["sku"]
                if sku not in FORECAST_SKUS:
                    continue
                fba_stock = int(float(item.get("total_quantity", 0) or 0))
                monthly = _amz_dem_map.get(sku, 0)
                daily_rate = monthly / 30.44 if monthly > 0 else 0
                if daily_rate <= 0:
                    continue
                days_of_stock = fba_stock / daily_rate
                if days_of_stock >= 365:
                    continue
                fba_stockout = _today + timedelta(days=int(days_of_stock))
                transfer_by = fba_stockout - timedelta(days=_xfer_lt)
                days_until = (transfer_by - _today).days
                if days_until <= 21:
                    t_urg = "OVERDUE" if days_until < 0 else ("TRANSFER NOW" if days_until <= 7 else "TRANSFER SOON")
                    alerts["transfer"].append({
                        "sku": sku,
                        "flavor": get_flavor(sku),
                        "days": days_until,
                        "urgency": t_urg,
                        "fba_stock": fba_stock,
                        "days_of_stock": round(days_of_stock),
                    })

    except Exception:
        pass
    return alerts

_global_alerts = _compute_global_alerts()
_reorder_alerts = _global_alerts.get("reorder", [])
_transfer_alerts = _global_alerts.get("transfer", [])

# Inject a sticky top banner via CSS + HTML (zero-height Streamlit element)
# This floats above all content without taking layout space
_all_urgent = []
for a in _reorder_alerts:
    _all_urgent.append({**a, "_type": "reorder"})
for a in _transfer_alerts:
    _all_urgent.append({**a, "_type": "transfer"})
_all_urgent.sort(key=lambda x: x.get("days") or -999)

if _all_urgent:
    # Determine bar color from most urgent item
    _most_urgent = _all_urgent[0]
    _mu_urg = _most_urgent.get("urgency", "")
    if _mu_urg in ("OVERDUE", "EN ROUTE \u26A0\uFE0F"):
        _bar_bg = "#dc2626"
        _bar_text_color = "#ffffff"
    elif _mu_urg in ("ORDER NOW", "TRANSFER NOW"):
        _bar_bg = "#ea580c"
        _bar_text_color = "#ffffff"
    else:
        _bar_bg = "#d97706"
        _bar_text_color = "#ffffff"

    # Build the single-line message
    _n_total = len(_all_urgent)
    _mu_flavor = _most_urgent.get("flavor") or _most_urgent.get("sku", "")
    _mu_days = _most_urgent.get("days")
    _mu_type = _most_urgent.get("_type")

    if _mu_urg == "OVERDUE":
        _lead = f"\u26A0\uFE0F  {_mu_flavor} reorder is {abs(_mu_days)}d overdue"
    elif _mu_type == "transfer":
        _lead = f"\U0001F4E6  {_mu_flavor} \u2014 {_mu_days}d until FBA transfer needed"
    else:
        _lead = f"\U0001F514  {_mu_flavor} \u2014 {_mu_days}d until reorder"

    _extra = ""
    if _n_total > 1:
        _extra = f'<span style="opacity:0.85;margin-left:8px;">+{_n_total - 1} more \u2192 Rollup Ops</span>'

    st.markdown(
        f'<div style="'
        f'background:{_bar_bg};color:{_bar_text_color};'
        f'padding:8px 20px;font-size:0.8rem;font-weight:600;'
        f'display:flex;align-items:center;justify-content:center;gap:6px;'
        f'letter-spacing:0.01em;'
        f'margin:-1rem -2.5rem 1rem -2.5rem;'
        f'">'
        f'{_lead}{_extra}'
        f'</div>',
        unsafe_allow_html=True,
    )

# ================================================================
# PAGE DISPATCH
# ================================================================
# Build shared context for page modules (all cached — no DB queries on rerun)
_biz_vars = _cached_business_vars()
_ctx_media_spend = _cached_media_spend()
_ctx_amz_rev_forecast = _cached_amz_rev_forecast()
_ctx = {
    'forecast_skus': FORECAST_SKUS,
    'cached_waterfall': _cached_waterfall,
    'cached_sku_forecast': _cached_sku_forecast,
    'cached_retention_curve': _cached_retention_curve,
    'cached_aov_and_units': _cached_aov_and_units,
    'load_seasonal_json': _load_seasonal_json,
    'load_sku_seasonal_json': _load_sku_seasonal_json,
    'cached_3pl_inventory': _cached_3pl_inventory,
    'cached_amazon_inventory': _cached_amazon_inventory,
    'active_sources': active_sources,
    'configured_sources': configured_sources,
    'biz_vars': _biz_vars,
    'media_spend': _ctx_media_spend,
    'amazon_revenue_forecast': _ctx_amz_rev_forecast,
    'channel': _channel,
    'source_filter': _source_filter,
    'page': page,
}

# --- Pre-warm critical caches (first load only) ---
# Trigger cached functions so data is ready before the page renders.
# These are no-ops if the cache is already warm (within TTL).
if 'caches_warmed' not in st.session_state:
    st.session_state['caches_warmed'] = True
    try:
        _cached_3pl_inventory()
    except Exception:
        pass
    try:
        _cached_amazon_inventory()
    except Exception:
        pass
    try:
        if _ctx_media_spend:
            import json as _json_warm
            _warm_seasonal = _load_seasonal_json()
            _warm_media = _json_warm.dumps(_ctx_media_spend, sort_keys=True)
            _cached_waterfall(_warm_media, None, 12, _warm_seasonal)
    except Exception:
        pass

if _page_type == 'Overview':
    from views.overview import render
    render(_ctx)
elif _page_type == 'Marketing':
    from views.marketing import render
    render(_ctx)
elif _page_type == 'Retention':
    from views.retention import render
    render(_ctx)
elif _page_type == 'Ops':
    from views.ops import render
    render(_ctx)
elif _page_type == 'Financials':
    from views.financials import render
    render(_ctx)
elif _page_type == 'Settings':
    from views.settings import render
    render(_ctx)
elif _page_type == 'Variables':
    from views.variables import render
    render(_ctx)
