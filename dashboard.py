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
    get_inventory_snapshot,
    get_precomputed,
)
from analytics.waterfall import (
    get_active_sources,
    get_configured_sources,
    get_average_retention_curve,
    get_customer_retention_curve,
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
    """Load retention curve from precomputed table first, fall back to live query."""
    import json as _json
    key = f"retention_curve_{source_filter or 'shopify'}"
    try:
        with get_db() as conn:
            cached = get_precomputed(conn, key, max_age_hours=25)
        if cached:
            data = _json.loads(cached)
            # Keys come back as strings from JSON — convert to int
            return {int(k): v for k, v in data.items()}
    except Exception:
        pass
    return get_average_retention_curve(source_filter)

@st.cache_data(ttl=_CACHE_TTL)
def _cached_customer_retention_curve(source_filter):
    """Load customer retention curve from precomputed table first, fall back to live query."""
    import json as _json
    key = f"customer_retention_curve_{source_filter or 'shopify'}"
    try:
        with get_db() as conn:
            cached = get_precomputed(conn, key, max_age_hours=25)
        if cached:
            data = _json.loads(cached)
            return {int(k): v for k, v in data.items()}
    except Exception:
        pass
    return get_customer_retention_curve(source_filter)

@st.cache_data(ttl=_CACHE_TTL)
def _cached_aov_and_units(source_filter):
    """Load AOV/units from precomputed table first, fall back to live query."""
    import json as _json
    key = f"aov_and_units_{source_filter or 'shopify'}"
    try:
        with get_db() as conn:
            cached = get_precomputed(conn, key, max_age_hours=25)
        if cached:
            return _json.loads(cached)
    except Exception:
        pass
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


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _cached_model_runs():
    """Cache model run metadata for freshness badges."""
    from db import get_model_runs
    with get_db() as conn:
        return get_model_runs(conn)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _cached_freshness_badge(sources_key):
    """Cache freshness badge data (last sync ts, new rows, synced sources) for a set of sources."""
    sources = sources_key.split(',') if sources_key else []
    with get_db() as conn:
        ts = get_last_sync_timestamp(conn, sources)
        new = get_new_rows_since_yesterday(conn, sources)
        srcs = get_synced_sources(conn, sources)
    return {'ts': ts, 'new': new, 'srcs': srcs}

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
def _load_channel_seasonal_json(source):
    """Load channel-specific seasonal indices computed from that channel's own data."""
    import json as _json_s
    from analytics.seasonal import compute_channel_seasonal_indices
    with get_db() as conn:
        enabled = get_setting(conn, "seasonality_enabled", "true")
        if enabled != "true":
            return None
    indices = compute_channel_seasonal_indices(source=source)
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
    """Cached wrapper for Packiyo 3PL inventory — reads DB snapshot (written by scheduler)."""
    try:
        with get_db() as conn:
            return get_inventory_snapshot(conn, 'packiyo', max_age_hours=36)
    except Exception:
        return None


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _cached_amazon_inventory():
    """Cached wrapper for Amazon FBA inventory — reads DB snapshot (written by scheduler)."""
    try:
        with get_db() as conn:
            return get_inventory_snapshot(conn, 'amazon', max_age_hours=36)
    except Exception:
        return None


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
import functools as _functools

@_functools.lru_cache(maxsize=1)
def _read_logo():
    _logo_path = _os.path.join(_os.path.dirname(__file__), 'assets', 'hydrant-logo.svg')
    with open(_logo_path) as _f:
        return _f.read()

st.sidebar.markdown(
    f'<div style="padding:8px 0 0;">{_read_logo()}</div>',
    unsafe_allow_html=True,
)
st.sidebar.caption("Command Center")

# ── Navigation with section grouping (channel-first) ──
_NAV_ICONS = {
    'DTC Overview': '\U0001F4CA', 'DTC Marketing': '\U0001F4C8', 'DTC Retention': '\U0001F504', 'DTC Ops': '\U0001F4E6',
    'Amazon Overview': '\U0001F4CA', 'Amazon Marketing': '\U0001F4C8', 'Amazon Retention': '\U0001F504', 'Amazon Ops': '\U0001F4E6',
    'Rollup Overview': '\U0001F4CA', 'Rollup Marketing': '\U0001F4C8', 'Rollup Retention': '\U0001F504', 'Rollup Ops': '\U0001F4E6',
    'Cash Flow': '\U0001F4B0',
    'Financials': '\U0001F4B0', 'Settings': '\u2699\uFE0F', 'Variables': '\U0001F4DD',
}

_NAV_GROUPS = [
    ('Rollup', ['Rollup Overview', 'Rollup Marketing', 'Rollup Retention', 'Rollup Ops']),
    ('DTC', ['DTC Overview', 'DTC Marketing', 'DTC Retention', 'DTC Ops']),
    ('Amazon', ['Amazon Overview', 'Amazon Marketing', 'Amazon Retention', 'Amazon Ops']),
    ('Finance & Config', ['Cash Flow', 'Financials', 'Settings', 'Variables']),
]

_STANDALONE_PAGES = {'Cash Flow', 'Financials', 'Settings', 'Variables'}

# Legacy URL redirect map (old page names → new)
_LEGACY_PAGE_MAP = {
    'Overview': 'Rollup Overview', 'Marketing': 'Rollup Marketing',
    'Retention': 'Rollup Retention', 'Demand Forecast': 'Rollup Ops',
    'Projected Inventory': 'Rollup Ops', 'Reorder Alerts': 'Rollup Ops',
    'FBA Transfers': 'Amazon Ops', '3PL Inventory': 'DTC Ops',
    'Amazon Inventory': 'Amazon Ops',
}

def _nav_format(x):
    """Format nav label — keep full name for unique radio identity."""
    icon = _NAV_ICONS.get(x, '')
    return f'{icon}  {x}'

# Flatten all pages for the single radio
_ALL_NAV_PAGES = []
for _grp_name, _grp_pages in _NAV_GROUPS:
    _ALL_NAV_PAGES.extend(_grp_pages)

# Restore page from URL query params on first load only.
# We do NOT sync back to query_params on navigation because setting
# st.query_params triggers a rerun that can lose session state,
# causing the radio to revert to the URL's page.
if '_nav_radio' not in st.session_state:
    _default_page = st.query_params.get('page', 'Rollup Overview')
    # Handle legacy bookmarks
    if _default_page in _LEGACY_PAGE_MAP:
        _default_page = _LEGACY_PAGE_MAP[_default_page]
    if _default_page not in _ALL_NAV_PAGES:
        _default_page = 'Rollup Overview'
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

# --- Last synced indicator (replaces auto-sync — scheduler handles daily ETL) ---
if configured_sources:
    _last_ts = _cached_last_sync_ts()
    if _last_ts:
        try:
            from datetime import datetime as _dt_badge
            _last_dt = _dt_badge.strptime(_last_ts[:19], "%Y-%m-%d %H:%M:%S")
            _ago = datetime.utcnow() - _last_dt
            if _ago.days > 0:
                _ago_str = f"{_ago.days}d ago"
            elif _ago.seconds > 3600:
                _ago_str = f"{_ago.seconds // 3600}h ago"
            else:
                _ago_str = f"{_ago.seconds // 60}m ago"
            st.sidebar.caption(f"\U0001F504 Last synced {_ago_str}")
        except (ValueError, TypeError):
            st.sidebar.caption(f"\U0001F504 Last synced: {_last_ts[:16]}")
    else:
        st.sidebar.caption("\u26A0\uFE0F No sync data — click Refresh Data")

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
            # Run analytics models to populate pre-computed page data
            if successes:
                sync_container.info("Computing analytics models...")
                try:
                    from analytics.orchestrator import run_all_daily_models
                    run_all_daily_models(triggered_by='etl_sync')
                except Exception as e:
                    log.warning('Post-sync model run failed: %s', e)
                clear_waterfall_cache()
                st.cache_data.clear()
                st.rerun()
        else:
            st.sidebar.warning("No sources configured to sync.")


# ================================================================
# GLOBAL NOTIFICATION BAR — Reorder & FBA Transfer Alerts
# ================================================================
@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _compute_global_alerts():
    """Read pre-computed reorder & FBA transfer alerts from DB.

    Falls back to live computation if pre-computed results are not available.
    """
    # Try pre-computed first (populated by orchestrator after daily sync)
    try:
        with get_db() as conn:
            cached = get_precomputed(conn, 'global_alerts', max_age_hours=25)
        if cached:
            import json as _json_alerts
            return _json_alerts.loads(cached)
    except Exception:
        pass

    # Fallback: compute live (first deploy before scheduler runs)
    return _compute_global_alerts_live()


def _compute_global_alerts_live():
    """Live computation fallback for global alerts."""
    alerts = {"reorder": [], "transfer": [], "cashflow": []}

    try:
        with get_db() as conn:
            from analytics.cashflow import build_cashflow_forecast, get_cashflow_kpis
            cf_df = build_cashflow_forecast(conn, weeks=13, scenario='base')
            if not cf_df.empty:
                cf_kpis = get_cashflow_kpis(conn, cf_df)
                if cf_kpis.get('alert_week'):
                    alerts["cashflow"].append({
                        "week": cf_kpis['alert_week'],
                        "balance": cf_df[cf_df['week_num'] == cf_kpis['alert_week']].iloc[0]['closing_balance'],
                        "threshold": cf_kpis['min_cash_threshold'],
                    })
    except Exception:
        pass

    try:
        from analytics.reorder import build_reorder_plan
        import json as _json_alert

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

        if _amz_inv_items:
            from utils.date_helpers import business_today as _biz_today, business_yesterday as _biz_yesterday
            with get_db() as conn:
                _amz_dem = read_sql("""
                    SELECT sku, SUM(units_sold) / 3.0 as monthly_demand
                    FROM daily_sku_sales
                    WHERE source = 'amazon' AND sale_date >= date('now', '-90 days')
                      AND sale_date <= %s
                    GROUP BY sku
                """, conn, params=(str(_biz_yesterday()),))
            _amz_dem_map = dict(zip(_amz_dem["sku"], _amz_dem["monthly_demand"])) if not _amz_dem.empty else {}
            _today = _biz_today()
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
    'cached_customer_retention_curve': _cached_customer_retention_curve,
    'cached_aov_and_units': _cached_aov_and_units,
    'load_seasonal_json': _load_seasonal_json,
    'load_channel_seasonal_json': _load_channel_seasonal_json,
    'load_sku_seasonal_json': _load_sku_seasonal_json,
    'cached_3pl_inventory': _cached_3pl_inventory,
    'cached_amazon_inventory': _cached_amazon_inventory,
    'cached_freshness_badge': _cached_freshness_badge,
    'cached_model_runs': _cached_model_runs,
    'active_sources': active_sources,
    'configured_sources': configured_sources,
    'biz_vars': _biz_vars,
    'media_spend': _ctx_media_spend,
    'amazon_revenue_forecast': _ctx_amz_rev_forecast,
    'channel': _channel,
    'source_filter': _source_filter,
    'page': page,
    'global_alerts': _global_alerts,
}

_page_modules = {
    'Overview': 'views.overview',
    'Marketing': 'views.marketing',
    'Retention': 'views.retention',
    'Ops': 'views.ops',
    'Cash Flow': 'views.cashflow',
    'Financials': 'views.financials',
    'Settings': 'views.settings',
    'Variables': 'views.variables',
}

if _page_type in _page_modules:
    import importlib
    _mod = importlib.import_module(_page_modules[_page_type])
    try:
        _mod.render(_ctx)
    except Exception as _page_exc:
        import traceback as _tb
        _tb_str = _tb.format_exc()
        st.error(f'{type(_page_exc).__name__}: {_page_exc}')
        st.expander('Traceback:').code(_tb_str)
        # Log to DB for automated monitoring
        try:
            from db import get_db, log_page_error
            _full_page = f'{_channel} {_page_type}' if _channel else _page_type
            with get_db() as _err_conn:
                log_page_error(_err_conn, _full_page, type(_page_exc).__name__, str(_page_exc), _tb_str)
        except Exception:
            pass  # don't let error logging break the page
