"""
Streamlit Dashboard for Inventory Demand Forecasting.
Pages: Overview, Retention, Forecast, Reorder Alerts
"""
import os
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import numpy as np
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from db import (
    get_db, init_db, rebuild_daily_sales,
    get_media_spend, upsert_media_spend,
    get_amazon_revenue_forecast, upsert_amazon_revenue_forecast,
    get_planned_inbound, get_planned_inbound_dict, upsert_planned_inbound,
    get_seasonal_indices, upsert_seasonal_index,
    get_setting, set_setting,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
)
from analytics.retention import (
    get_customer_cohort_data,
    get_cohort_sizes,
)
from analytics.forecast import (
    forecast_sku,
    get_forecast_summary,
    get_sku_daily_sales,
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
from analytics.sku_flavors import get_flavor, get_flavor_map, sort_df_by_best_seller, get_sku_sales_rank
from analytics.dtc_demand import (
    build_master_dtc_forecast,
    get_amazon_sku_velocity,
    get_current_month_progress,
    compute_remaining_month_demand,
)
from config import USE_MOCK_DATA, ENV_KEYS, save_env, reload_config
import config


# --- Cached wrappers for expensive computations ---
# These survive media spend changes (only cleared on full data refresh).
@st.cache_data(ttl=600)
def _cached_retention_curve(source_filter):
    return get_average_retention_curve(source_filter)

@st.cache_data(ttl=600)
def _cached_aov_and_units(source_filter):
    return get_aov_and_units(source_filter)

@st.cache_data(ttl=600)
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

@st.cache_data(ttl=600)
def _cached_sku_forecast(waterfall_json, source_filter):
    """SKU forecast depends on waterfall output."""
    df = pd.read_json(waterfall_json) if waterfall_json else pd.DataFrame()
    return build_sku_forecast_table(df, source_filter=source_filter)

# Core SKUs to include in the variant forecast table
FORECAST_SKUS = {
    "ENBP-BP0030-LEB0",
    "HYBP-BP0030-CHLB0",
    "HYBP-BP0030-GFB0",
    "HYBP-BP0030-ICLEB0",
    "HYBP-BP0050-NAB0",
    "SLBP-BP0030-CHB0",
    "ENPO-ST0030-RSLEB0",
    "HYPO-ST0030-BOB0",
    "HYPO-ST0030-FRPUB0",
    "HYPO-ST0030-LELMB0",
    "HYPO-ST0030-VPBFLB0",
    "IMPO-ST0030-ELB0",
    "NSPO-ST0030-BEB0",
    "NSPO-ST0030-LEB0",
    "NSPO-ST0030-VPWLBB0",
    "NSPO-ST0030-WALEB0",
    "SLPO-ST0030-ELB0",
}

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


def _smart_date_filter(data_min, data_max, key_prefix, show_presets=True, default_preset='MTD'):
    """Reusable date filter with quick presets (MTD, YTD, Last 7d, etc.).

    Args:
        data_min: earliest date available (datetime.date)
        data_max: latest date available (datetime.date)
        key_prefix: unique key prefix for Streamlit widgets
        show_presets: whether to show preset buttons
        default_preset: preset name to use as default (defaults to "MTD")

    Returns:
        (start_date, end_date) as datetime.date objects
    """
    from datetime import date as _d, timedelta as _td
    today = datetime.utcnow().date()

    # Build preset options
    presets = {
        'MTD': (_d(today.year, today.month, 1), today),
        'Last 7 Days': (today - _td(days=6), today),
        'Last 30 Days': (today - _td(days=29), today),
        'Last 90 Days': (today - _td(days=89), today),
        'YTD': (_d(today.year, 1, 1), today),
        'All Time': (data_min, data_max),
    }

    # Determine defaults — use requested preset (MTD unless overridden)
    if default_preset and default_preset in presets:
        _def_start, _def_end = presets[default_preset]
        _def_start = max(_def_start, data_min)
        _def_end = min(_def_end, data_max)
    else:
        _def_start, _def_end = data_min, data_max

    # Seed session state on first render so date_input widgets pick up the preset
    if f'{key_prefix}_start' not in st.session_state:
        st.session_state[f'{key_prefix}_start'] = _def_start
    if f'{key_prefix}_end' not in st.session_state:
        st.session_state[f'{key_prefix}_end'] = _def_end

    default_start = st.session_state[f'{key_prefix}_start']
    default_end = st.session_state[f'{key_prefix}_end']

    # Clamp to valid range
    default_start = max(default_start, data_min)
    default_end = min(default_end, data_max)

    if show_presets:
        # Detect which preset matches current date range
        active_preset = None
        for label, (ps, pe) in presets.items():
            clamped_s = max(ps, data_min)
            clamped_e = min(pe, data_max)
            if default_start == clamped_s and default_end == clamped_e:
                active_preset = label
                break

        preset_cols = st.columns(len(presets))
        for i, (label, (ps, pe)) in enumerate(presets.items()):
            is_active = label == active_preset
            if is_active:
                btn_type = 'primary'
            else:
                btn_type = 'secondary'
            if preset_cols[i].button(
                label, key=f'{key_prefix}_preset_{label}',
                use_container_width=True, type=btn_type,
            ):
                st.session_state[f'{key_prefix}_start'] = max(ps, data_min)
                st.session_state[f'{key_prefix}_end'] = min(pe, data_max)
                st.rerun()

    dc1, dc2, dc_spacer = st.columns([1, 1, 4])
    with dc1:
        start = st.date_input('From', value=default_start, min_value=data_min,
                               max_value=data_max, key=f'{key_prefix}_start',
                               label_visibility='collapsed')
    with dc2:
        end = st.date_input('To', value=default_end, min_value=data_min,
                             max_value=data_max, key=f'{key_prefix}_end',
                             label_visibility='collapsed')
    return start, end


st.set_page_config(
    page_title="Hydrant — Command Center",
    page_icon="💧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Password protection (Railway / production) ---
def _check_password():
    """Returns True if the user has entered the correct password."""
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        return True  # No password set = no gate (local dev)
    if st.session_state.get("authenticated"):
        return True
    st.title("Hydrant Command Center")
    entered = st.text_input("Password", type="password", key="pw_input")
    if st.button("Login", key="pw_login"):
        if entered == password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

if not _check_password():
    st.stop()

# ── Global CSS — Hydrant Brand ────────────────────────────────
# Brand colors: Navy #0F3557, Sky Blue #7ECCE5, Orange #F58B3D,
#   Cream #F5F0E3, Soft Green #C5E0A5, Coral #F4A3A0, Light Yellow #FFF9C4
st.markdown("""
<style>
/* ── Typography — Visby CF (fallback to system) ── */
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap');
html, body, [class*="css"] {
    font-family: 'Visby CF', 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
}

/* ── App background — clean white ── */
.stApp {
    background: #f7f8fc !important;
}

/* ── Sidebar — Hydrant navy ── */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0B2A45 0%, #0F3557 40%, #143D61 100%) !important;
    border-right: none;
    box-shadow: 4px 0 20px rgba(15,53,87,0.15);
}
section[data-testid="stSidebar"] * {
    color: #ffffff !important;
}
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2 {
    font-size: 1.3rem !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em;
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}

/* ── Headers — Hydrant navy ── */
h1 {
    font-weight: 700 !important;
    letter-spacing: -0.03em !important;
    font-size: 2rem !important;
    margin-bottom: 0.1rem !important;
    color: #0F3557 !important;
}
h2 {
    font-weight: 600 !important;
    letter-spacing: -0.02em !important;
    font-size: 1.3rem !important;
    margin-top: 1.8rem !important;
    padding-bottom: 0.5rem;
    border-bottom: 2px solid #E8EDF3;
    color: #0F3557 !important;
}
h3 {
    font-weight: 600 !important;
    letter-spacing: -0.01em !important;
    font-size: 1.05rem !important;
    color: #1a3a5c !important;
}

/* ── Body text — scoped to avoid overriding component internals ── */
.main [data-testid="stMarkdownContainer"] p,
.main [data-testid="stMarkdownContainer"] span,
.main [data-testid="stMarkdownContainer"] li {
    color: #2c3e50;
}
.main label {
    color: #2c3e50;
}

/* ── Metric cards — white cards with sky-blue accent ── */
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #E8EDF3;
    border-radius: 14px;
    padding: 18px 22px !important;
    box-shadow: 0 2px 8px rgba(15,53,87,0.06);
    transition: all 0.2s ease;
    border-top: 3px solid #7ECCE5;
}
[data-testid="stMetric"]:hover {
    box-shadow: 0 4px 16px rgba(15,53,87,0.1);
    transform: translateY(-1px);
}
[data-testid="stMetricLabel"] {
    font-size: 0.72rem !important;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #6b7c93 !important;
    opacity: 1 !important;
    font-weight: 600 !important;
}
[data-testid="stMetricValue"] {
    font-size: 1.55rem !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em;
    color: #0F3557 !important;
}
[data-testid="stMetricDelta"] {
    font-size: 0.78rem !important;
    font-weight: 600 !important;
}
[data-testid="stMetricDelta"][data-testid] svg { color: inherit !important; }

/* ── DataFrames & tables — force light/white everywhere ── */
[data-testid="stDataFrame"] {
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #E8EDF3;
    box-shadow: 0 2px 12px rgba(15,53,87,0.06);
    background: #ffffff !important;
}
[data-testid="stDataFrame"] iframe {
    border-radius: 12px;
    background: #ffffff !important;
}
[data-testid="stDataFrame"] > div,
[data-testid="stDataFrame"] > div > div,
[data-testid="stDataFrame"] > div > div > div {
    background: #ffffff !important;
}
/* Glide Data Editor (canvas-based): force container backgrounds white */
[data-testid="stDataFrame"] [data-testid="glideDataEditor"],
[data-testid="stDataFrame"] canvas,
[data-testid="stDataFrame"] .dvn-scroller,
[data-testid="stDataFrame"] [class*="glide"],
[data-testid="stDataFrame"] [class*="data-grid"],
[data-testid="stDataFrame"] [role="grid"],
[data-testid="stDataFrame"] [role="gridcell"],
[data-testid="stDataFrame"] [role="columnheader"] {
    background: #ffffff !important;
    background-color: #ffffff !important;
    color: #1e2d3d !important;
}
/* Target the Glide wrapper divs exhaustively */
[data-testid="stDataFrame"] div[style] {
    background: #ffffff !important;
    background-color: #ffffff !important;
}
/* Static table rendering (st.table) */
.stDataFrame table,
[data-testid="stTable"] table {
    background: #ffffff !important;
    color: #1e2d3d !important;
    border-radius: 12px;
    overflow: hidden;
}
.stDataFrame table th,
[data-testid="stTable"] table th {
    background: #F0F4F8 !important;
    color: #0F3557 !important;
    font-weight: 600 !important;
    font-size: 0.76rem !important;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 2px solid #D6DEE8 !important;
    padding: 11px 14px !important;
}
.stDataFrame table td,
[data-testid="stTable"] table td {
    background: #ffffff !important;
    color: #1e2d3d !important;
    border-bottom: 1px solid #F0F4F8 !important;
    padding: 9px 14px !important;
    font-size: 0.84rem !important;
}
.stDataFrame table tr:hover td,
[data-testid="stTable"] table tr:hover td {
    background: #F7FAFC !important;
}

/* ── Tabs — rounded pill style with Hydrant blue ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background: #F0F4F8;
    border-radius: 12px;
    padding: 4px;
    border: 1px solid #E8EDF3;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 9px;
    font-weight: 600;
    font-size: 0.84rem;
    padding: 8px 22px;
    transition: all 0.15s ease;
    color: #5a6f83 !important;
    background: transparent !important;
}
.stTabs [data-baseweb="tab"] * {
    color: inherit !important;
}
.stTabs [data-baseweb="tab"]:hover {
    background: rgba(126,204,229,0.15) !important;
    color: #0F3557 !important;
}
.stTabs [data-baseweb="tab"]:hover * {
    color: #0F3557 !important;
}
.stTabs [aria-selected="true"] {
    background: #0F3557 !important;
    color: #ffffff !important;
    box-shadow: 0 2px 8px rgba(15,53,87,0.25);
}
.stTabs [aria-selected="true"] *,
.stTabs [aria-selected="true"] span,
.stTabs [aria-selected="true"] p,
.stTabs [aria-selected="true"] div {
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}
/* Tab highlight bar — remove default underline, we use pill bg instead */
.stTabs [data-baseweb="tab-highlight"] {
    display: none !important;
}
.stTabs [data-baseweb="tab-border"] {
    display: none !important;
}

/* ── Buttons — rounded, Hydrant style ── */
.stButton button {
    border-radius: 24px !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    letter-spacing: 0.01em;
    transition: all 0.15s ease !important;
    border: 1px solid #D6DEE8 !important;
    color: #0F3557 !important;
    background: #ffffff !important;
}
.stButton button:hover {
    border-color: #7ECCE5 !important;
    box-shadow: 0 2px 8px rgba(126,204,229,0.2);
    color: #0F3557 !important;
}
.stButton button[kind="primary"],
.stButton button[data-testid="stBaseButton-primary"] {
    background: linear-gradient(135deg, #7ECCE5, #5BB8D4) !important;
    border: none !important;
    color: #0F3557 !important;
    box-shadow: 0 2px 10px rgba(126,204,229,0.3) !important;
}
.stButton button[kind="primary"]:hover,
.stButton button[data-testid="stBaseButton-primary"]:hover {
    box-shadow: 0 4px 16px rgba(126,204,229,0.4) !important;
    transform: translateY(-1px);
}

/* ── Expanders — white card ── */
[data-testid="stExpander"] {
    background: #ffffff !important;
    border: 1px solid #E8EDF3 !important;
    border-radius: 12px !important;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(15,53,87,0.04);
}
[data-testid="stExpander"]:hover {
    border-color: #7ECCE5 !important;
}
[data-testid="stExpanderToggleIcon"] {
    opacity: 0.6;
}

/* ── Dividers ── */
hr {
    opacity: 0.3 !important;
    margin: 2.5rem 0 !important;
    border-color: #D6DEE8 !important;
}

/* ── Captions ── */
.stCaption, [data-testid="stCaptionContainer"] {
    font-size: 0.8rem !important;
    color: #7a8da0 !important;
    opacity: 1 !important;
}

/* ── Progress bars — sky blue ── */
[data-testid="stProgress"] > div > div {
    border-radius: 8px !important;
    background: #E8EDF3 !important;
}
[data-testid="stProgress"] > div > div > div {
    background: linear-gradient(90deg, #7ECCE5, #5BB8D4) !important;
    border-radius: 8px !important;
}

/* ── Alert boxes ── */
[data-testid="stAlert"] {
    border-radius: 12px !important;
    border: 1px solid #E8EDF3 !important;
}

/* ── Selectboxes & inputs ── */
[data-testid="stSelectbox"] > div > div,
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stDateInput > div > div > input {
    border-radius: 10px !important;
    border-color: #D6DEE8 !important;
    background: #ffffff !important;
    color: #0F3557 !important;
}
[data-testid="stSelectbox"] > div > div:focus-within,
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus {
    border-color: #7ECCE5 !important;
    box-shadow: 0 0 0 3px rgba(126,204,229,0.15) !important;
}

/* ── Data editor — force white background ── */
[data-testid="stDataEditor"] {
    border: 1px solid #E8EDF3;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 12px rgba(15,53,87,0.06);
    background: #ffffff !important;
}
[data-testid="stDataEditor"] > div,
[data-testid="stDataEditor"] > div > div,
[data-testid="stDataEditor"] > div > div > div,
[data-testid="stDataEditor"] div[style],
[data-testid="stDataEditor"] [data-testid="glideDataEditor"],
[data-testid="stDataEditor"] canvas,
[data-testid="stDataEditor"] [role="grid"],
[data-testid="stDataEditor"] [role="gridcell"],
[data-testid="stDataEditor"] [role="columnheader"] {
    background: #ffffff !important;
    background-color: #ffffff !important;
}

/* ── File uploader ── */
[data-testid="stFileUploader"] > div {
    border-radius: 12px !important;
    border-color: #D6DEE8 !important;
    background: #F7FAFC !important;
}

/* ── Toggle switch ── */
[data-testid="stToggle"] label span {
    font-weight: 500 !important;
    color: #2c3e50 !important;
}

/* ── Radio buttons (horizontal) ── */
.stRadio > div[role="radiogroup"] > label {
    border-radius: 8px;
    padding: 4px 12px;
    transition: background 0.15s ease;
    color: #2c3e50 !important;
}
.stRadio > div[role="radiogroup"] > label:hover {
    background: rgba(126,204,229,0.1);
}

/* ── Main container — full width ── */
.block-container {
    padding-top: 1.5rem !important;
    padding-left: 2.5rem !important;
    padding-right: 2.5rem !important;
    max-width: 100% !important;
}

/* ── Sidebar navigation — styled radio buttons ── */
section[data-testid="stSidebar"] .stRadio [data-testid="stWidgetLabel"] { display: none; }
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] {
    gap: 1px !important;
}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label {
    padding: 10px 16px !important;
    margin: 1px 4px !important;
    border-radius: 10px !important;
    font-size: 0.88rem !important;
    font-weight: 500 !important;
    color: rgba(255,255,255,0.7) !important;
    transition: all 0.18s ease !important;
    cursor: pointer !important;
    background: transparent !important;
    border-left: 3px solid transparent !important;
}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label:hover {
    background: rgba(126,204,229,0.12) !important;
    color: #ffffff !important;
}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label[data-checked="true"],
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label:has(input:checked) {
    background: rgba(126,204,229,0.18) !important;
    color: #ffffff !important;
    font-weight: 600 !important;
    border-left: 3px solid #7ECCE5 !important;
}
/* Hide the radio dot */
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label > div:first-child {
    display: none !important;
}
/* Section label pseudo-element styling */
section[data-testid="stSidebar"] .nav-section {
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: rgba(255,255,255,0.35);
    padding: 14px 20px 4px;
    font-weight: 600;
}

/* ── Styled dataframe (Pandas Styler) ── */
.stTable {
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #E8EDF3;
    box-shadow: 0 2px 12px rgba(15,53,87,0.06);
}

/* ── Number input styling ── */
.stNumberInput label, .stTextInput label, .stSelectbox label, .stDateInput label {
    color: #0F3557 !important;
    font-weight: 500 !important;
}

/* ── Multiselect ── */
[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
    background: #7ECCE5 !important;
    color: #0F3557 !important;
    border-radius: 6px !important;
}
</style>
""", unsafe_allow_html=True)

# ── Global Plotly theme — Hydrant brand ───────────────────────
import plotly.io as pio
_hydrant_template = go.layout.Template()
_hydrant_template.layout.plot_bgcolor = "#ffffff"
_hydrant_template.layout.paper_bgcolor = "rgba(0,0,0,0)"
_hydrant_template.layout.font = dict(
    family="Visby CF, DM Sans, -apple-system, sans-serif",
    size=12,
    color="#2c3e50",
)
_hydrant_template.layout.xaxis = dict(
    gridcolor="#E8EDF3", gridwidth=1,
    linecolor="#D6DEE8", linewidth=1,
    tickfont=dict(color="#6b7c93"),
)
_hydrant_template.layout.yaxis = dict(
    gridcolor="#E8EDF3", gridwidth=1,
    linecolor="#D6DEE8", linewidth=1,
    tickfont=dict(color="#6b7c93"),
)
_hydrant_template.layout.margin = dict(l=0, r=0, t=30, b=0)
_hydrant_template.layout.colorway = [
    "#0F3557",  # Hydrant navy
    "#7ECCE5",  # Sky blue
    "#F58B3D",  # Orange
    "#C5E0A5",  # Soft green
    "#F4A3A0",  # Coral
    "#5BB8D4",  # Medium blue
    "#FFC857",  # Warm yellow
    "#A8D5BA",  # Mint
    "#E87461",  # Warm red
    "#B5C7D3",  # Steel blue
]
pio.templates["hydrant"] = _hydrant_template
pio.templates.default = "hydrant"


# ── Reusable HTML table renderer — white bg, replaces st.dataframe() ──
def _render_df_as_html_global(df, max_height=None):
    """Render a plain DataFrame as a styled HTML table with white background.
    Replaces st.dataframe() to avoid Glide canvas dark-background issues."""
    styled = (
        df.style
        .set_properties(**{
            "background-color": "#ffffff",
            "color": "#1e2d3d",
            "font-size": "0.84rem",
            "font-family": "Visby CF, DM Sans, -apple-system, sans-serif",
            "text-align": "left",
        })
        .set_table_styles([
            {"selector": "th", "props": [
                ("background-color", "#F0F4F8"),
                ("color", "#0F3557"),
                ("font-weight", "600"),
                ("font-size", "0.74rem"),
                ("text-transform", "uppercase"),
                ("letter-spacing", "0.05em"),
                ("border-bottom", "2px solid #D6DEE8"),
                ("padding", "11px 14px"),
                ("position", "sticky"),
                ("top", "0"),
                ("z-index", "1"),
            ]},
            {"selector": "td", "props": [
                ("border-bottom", "1px solid #F0F4F8"),
                ("padding", "9px 14px"),
            ]},
        ])
        .hide(axis="index")
    )
    html = styled.to_html()
    height_style = f"max-height:{max_height}px;overflow-y:auto;" if max_height else ""
    st.markdown(
        f'<div style="background:#ffffff;border-radius:12px;overflow:hidden;'
        f'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
        f'width:100%;{height_style}">'
        '<style>'
        '.html-table table { width:100%; border-collapse:collapse; }'
        '.html-table th, .html-table td { white-space:nowrap; }'
        '.html-table tr:hover td { background:#F7FAFC !important; }'
        '</style>'
        f'<div class="html-table" style="overflow-x:auto;">{html}</div></div>',
        unsafe_allow_html=True,
    )


def _render_freshness_badge(last_refreshed_str=None, new_rows=None, is_live=False,
                            source=None, is_fallback=False):
    """Render a compact data-freshness indicator (right-aligned).

    Args:
        last_refreshed_str: UTC timestamp string from sync_log or datetime.utcnow().
        new_rows: Number of rows fetched / new today.
        is_live: True when data was just fetched from a live API.
        source: Data source label (e.g. 'Packiyo', 'Shopify + Amazon').
        is_fallback: True when showing cached/fallback data instead of live.
    """
    if is_fallback:
        dot_color = '#f59e0b'
        time_label = f'<span style="color:{dot_color};font-weight:600;">&#9679; Fallback</span>'
    elif is_live:
        dot_color = '#22c55e'
        time_label = f'<span style="color:{dot_color};font-weight:600;">&#9679; Live</span>'
    elif last_refreshed_str:
        try:
            last_dt = datetime.strptime(last_refreshed_str[:19], '%Y-%m-%d %H:%M:%S')
            delta = datetime.utcnow() - last_dt
            secs = delta.total_seconds()
            if secs < 3600:
                ago = f"{max(1, int(secs // 60))}m ago"
            elif secs < 86400:
                ago = f"{int(secs // 3600)}h ago"
            else:
                ago = f"{delta.days}d ago"
            time_label = f'Updated {ago}'
        except (ValueError, TypeError):
            time_label = f'Updated {last_refreshed_str}'
    else:
        time_label = '<span style="color:#f59e0b;">No sync data</span>'

    source_label = ''
    if source:
        source_label = (
            f'<div style="font-size:0.68rem;color:#94a3b8;margin-top:1px;">'
            f'Source: {source}</div>'
        )

    rows_label = ''
    if new_rows is not None and new_rows > 0:
        rows_text = f'{new_rows:,} rows' if is_live else f'+{new_rows:,} rows today'
        rows_label = (
            f'<div style="font-size:0.68rem;color:#22c55e;margin-top:1px;">'
            f'{rows_text}</div>'
        )
    elif new_rows is not None and new_rows == 0 and not is_live:
        rows_label = (
            '<div style="font-size:0.68rem;color:#94a3b8;margin-top:1px;">'
            'No new rows today</div>'
        )

    st.markdown(
        f'<div style="text-align:right;padding:28px 0 0;white-space:nowrap;">'
        f'<div style="font-size:0.74rem;color:#64748b;font-weight:500;">{time_label}</div>'
        f'{source_label}'
        f'{rows_label}'
        f'</div>',
        unsafe_allow_html=True,
    )


# --- Sidebar ---
st.sidebar.markdown(
    '<div style="display:flex;align-items:center;gap:10px;padding:4px 0 0;">'
    '<span style="font-size:1.5rem;">💧</span>'
    '<span style="font-size:1.3rem;font-weight:700;letter-spacing:-0.02em;">hydrant</span>'
    '</div>',
    unsafe_allow_html=True,
)
st.sidebar.caption("Command Center")

# ── Navigation with section grouping ──
_NAV_ICONS = {
    "Overview": "📊", "Marketing": "📈", "Retention": "🔄",
    "Demand Forecast": "🔮", "Projected Inventory": "📦",
    "Reorder Alerts": "🚨", "FBA Transfers": "🚚",
    "3PL Inventory": "🏭", "Amazon Inventory": "🛒",
    "Financials": "💰", "Settings": "⚙️",
}

_NAV_GROUPS = [
    ("Analytics", ["Overview", "Marketing", "Retention"]),
    ("Inventory", ["Demand Forecast", "Projected Inventory", "Reorder Alerts", "FBA Transfers", "3PL Inventory", "Amazon Inventory"]),
    ("Finance & Config", ["Financials", "Settings"]),
]

# Flatten all pages for the single radio
_ALL_NAV_PAGES = []
for _grp_name, _grp_pages in _NAV_GROUPS:
    _ALL_NAV_PAGES.extend(_grp_pages)

# Single radio drives actual selection (hidden, styled via CSS)
page = st.sidebar.radio(
    "Navigate",
    _ALL_NAV_PAGES,
    format_func=lambda x: f"{_NAV_ICONS.get(x, '')}  {x}",
    label_visibility="collapsed",
    key="_nav_radio",
)

# Inject section headers via JS/CSS after the radio renders
# Build a mapping: for each page index, which group it belongs to
_section_js_map = {}
_idx = 0
for _grp_name, _grp_pages in _NAV_GROUPS:
    _section_js_map[_idx] = _grp_name
    _idx += len(_grp_pages)

_section_style = """
<style>
/* Section dividers injected via nth-child */
"""
_idx = 0
for _grp_name, _grp_pages in _NAV_GROUPS:
    _child_num = _idx + 1  # CSS nth-child is 1-based
    # Force the label to be a block container so ::before stacks vertically
    _section_style += f"""
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] > label:nth-child({_child_num}) {{
    margin-top: {('20px' if _idx > 0 else '4px')} !important;
    flex-wrap: wrap !important;
    padding-top: 28px !important;
    position: relative !important;
}}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] > label:nth-child({_child_num})::before {{
    content: "{_grp_name.upper()}";
    position: absolute;
    top: 2px;
    left: 16px;
    font-size: 0.58rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: rgba(255,255,255,0.35) !important;
    font-weight: 600;
    line-height: 1;
    -webkit-text-fill-color: rgba(255,255,255,0.35) !important;
}}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] > label:nth-child({_child_num}):has(input:checked),
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] > label:nth-child({_child_num})[data-checked="true"] {{
    background: linear-gradient(to bottom, transparent 22px, rgba(126,204,229,0.18) 22px) !important;
}}
"""
    _idx += len(_grp_pages)

_section_style += "</style>"
st.sidebar.markdown(_section_style, unsafe_allow_html=True)

try:
    active_sources = get_active_sources()
except Exception:
    active_sources = []

try:
    configured_sources = get_configured_sources()
except Exception:
    configured_sources = []

st.sidebar.markdown("---")
if USE_MOCK_DATA:
    st.sidebar.caption("⚠️ Mock data mode")
elif configured_sources:
    _src_icons = {"shopify": "🟢", "amazon": "🟠"}
    _src_labels = " ".join(f"{_src_icons.get(s, '●')} {s.title()}" for s in configured_sources)
    st.sidebar.caption(f"Connected: {_src_labels}")
else:
    st.sidebar.caption("⚙️ No sources — go to Settings")

# --- Auto-sync: run daily sync if last sync was > 24 hours ago ---
if not USE_MOCK_DATA and configured_sources:
    with get_db() as conn:
        _last_any_sync = conn.execute(
            "SELECT MAX(created_at) as last_ts FROM sync_log WHERE status = 'success'"
        ).fetchone()
        _last_ts = _last_any_sync["last_ts"] if _last_any_sync else None
    _needs_auto_sync = False
    if _last_ts:
        try:
            from datetime import datetime as _dt_auto
            _last_dt = _dt_auto.strptime(_last_ts[:19], "%Y-%m-%d %H:%M:%S")
            _needs_auto_sync = (_dt_auto.utcnow() - _last_dt).total_seconds() > 86400  # 24h
        except (ValueError, TypeError):
            _needs_auto_sync = True
    else:
        _needs_auto_sync = True  # Never synced before

    if _needs_auto_sync and "auto_sync_done" not in st.session_state:
        st.session_state["auto_sync_done"] = True
        st.sidebar.info("⏳ Auto-syncing (last sync > 24h ago)...")
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
    if USE_MOCK_DATA:
        from mock_data import generate_mock_data
        with st.spinner("Regenerating mock data..."):
            generate_mock_data()
        st.sidebar.success("Mock data refreshed!")
        clear_waterfall_cache()
        st.cache_data.clear()
        st.rerun()
    elif not configured_sources:
        st.sidebar.error("No API sources configured. Go to **Settings** to connect Shopify or Amazon.")
    else:
        from etl.sync import run_daily_sync

        # Detect stale/mock data: check for mock-style order IDs or mismatched sources
        needs_full_refresh = False
        has_mock_data = False
        with get_db() as conn:
            mock_check = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE order_id LIKE 'ama-ord-%' OR order_id LIKE 'sho-ord-%'"
            ).fetchone()[0]
            has_mock_data = mock_check > 0
            has_data = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

        if has_mock_data:
            with get_db() as conn:
                for table in ["daily_sku_sales", "order_items", "orders", "customers", "sku_master", "sync_log"]:
                    conn.execute(f"DELETE FROM {table}")
            needs_full_refresh = True
            clear_waterfall_cache()
            st.cache_data.clear()
        elif has_data == 0:
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
@st.cache_data(ttl=300)
def load_sku_list():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT sku, product_name, category, sources FROM sku_master WHERE is_active = 1 ORDER BY category, sku"
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=300)
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
        source_split = pd.read_sql_query(
            f"SELECT source, SUM(order_count) as orders, SUM(revenue) as revenue FROM daily_sku_sales WHERE 1=1 {date_clause} GROUP BY source",
            conn, params=params_d,
        )

        # Top SKUs
        top_skus = pd.read_sql_query(f"""
            SELECT oi.sku, sm.product_name, sm.category,
                   SUM(oi.quantity) as total_units,
                   SUM(oi.total_price) as total_revenue
            FROM order_items oi
            JOIN sku_master sm ON oi.sku = sm.sku
            JOIN orders o ON oi.order_id = o.order_id
            WHERE 1=1 {date_clause_orders.replace('order_date', 'o.order_date')}
            GROUP BY oi.sku
            ORDER BY total_units DESC
            LIMIT 10
        """, conn, params=params_d)

        # Daily trend by source (for stacked revenue chart)
        daily_trend = pd.read_sql_query(f"""
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
# Lightweight check: only compute alerts if data exists, cache for 30 min
@st.cache_data(ttl=1800, show_spinner=False)
def _compute_global_alerts():
    """Compute reorder & FBA transfer urgency alerts for the notification bar."""
    alerts = {"reorder": [], "transfer": []}
    try:
        from analytics.reorder import build_reorder_plan, LEAD_TIME_WEEKS, MOQ_UNITS, SAFETY_STOCK_WEEKS
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

        _sku_alert = _cached_sku_forecast(_wf_alert.to_json(), None)
        if _sku_alert.empty:
            return alerts
        _sku_alert = _sku_alert[_sku_alert["SKU"].isin(FORECAST_SKUS)].copy()
        if "Variant" in _sku_alert.columns:
            _sku_alert = _sku_alert.drop(columns=["Variant"])
        if "Flavor" not in _sku_alert.columns:
            _sku_alert.insert(1, "Flavor", _sku_alert["SKU"].map(lambda s: get_flavor(s)))

        # Load inventory
        combined_inv = {}
        try:
            from etl.packiyo_client import get_inventory as _alert_3pl
            for item in _alert_3pl():
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
            from etl.amazon_inventory import get_inventory as _alert_fba
            _amz_inv_items = _alert_fba()
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

        reorder_df, _ = build_reorder_plan(
            sku_forecast_table=_sku_alert,
            inventory_data=inv_data,
            lead_time_weeks=LEAD_TIME_WEEKS,
            moq=MOQ_UNITS,
            safety_weeks=SAFETY_STOCK_WEEKS,
            forecast_skus=FORECAST_SKUS,
            planned_inbound=_planned,
        )

        if not reorder_df.empty:
            for _, row in reorder_df.iterrows():
                urgency = row.get("Urgency", "OK")
                if urgency in ("OVERDUE", "ORDER NOW", "ORDER SOON", "EN ROUTE ⚠️"):
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
                _amz_dem = pd.read_sql_query("""
                    SELECT sku, SUM(units_sold) / 3.0 as monthly_demand
                    FROM daily_sku_sales
                    WHERE source = 'amazon' AND sale_date >= date('now', '-90 days')
                    GROUP BY sku
                """, conn)
            _amz_dem_map = dict(zip(_amz_dem["sku"], _amz_dem["monthly_demand"])) if not _amz_dem.empty else {}
            _today = datetime.utcnow().date()
            _xfer_lt = 4 * 7  # 4 weeks default

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
    if _mu_urg in ("OVERDUE", "EN ROUTE ⚠️"):
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
        _lead = f"⚠️  {_mu_flavor} reorder is {abs(_mu_days)}d overdue"
    elif _mu_type == "transfer":
        _lead = f"📦  {_mu_flavor} — {_mu_days}d until FBA transfer needed"
    else:
        _lead = f"🔔  {_mu_flavor} — {_mu_days}d until reorder"

    _extra = ""
    if _n_total > 1:
        _extra = f'<span style="opacity:0.85;margin-left:8px;">+{_n_total - 1} more → Reorder Alerts</span>'

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
# PAGE: OVERVIEW
# ================================================================
if page == "Overview":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Overview")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        _render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)

    # Show last sync status (compact)
    with get_db() as conn:
        last_sync = conn.execute(
            "SELECT source, sync_date, records_fetched, status, error_message, created_at "
            "FROM sync_log ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        total_orders_check = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    if total_orders_check == 0:
        st.warning("No sales data yet. Click **Refresh Data** in the sidebar, or connect a store in **Settings**.")
        if last_sync and last_sync["status"] != "success":
            st.error(f"Last sync failed ({last_sync['source'].title()}): {last_sync['error_message'] or 'Unknown error'}")
    else:
        with get_db() as conn:
            date_range = conn.execute("SELECT MIN(order_date), MAX(order_date) FROM orders").fetchone()
        if date_range and date_range[0] and date_range[1]:
            earliest = datetime.strptime(date_range[0], "%Y-%m-%d")
            latest = datetime.strptime(date_range[1], "%Y-%m-%d")
            data_span_days = (latest - earliest).days
            if data_span_days < 75:
                st.warning(
                    f"Limited history ({data_span_days} days). Request `read_all_orders` scope "
                    f"in your [Shopify Partner Dashboard](https://partners.shopify.com) for full access."
                )

    # --- Date Filter ---
    with get_db() as conn:
        _ov_dr = conn.execute("SELECT MIN(order_date), MAX(order_date) FROM orders").fetchone()
        _ov_dr2 = conn.execute("SELECT MIN(sale_date), MAX(sale_date) FROM daily_sku_sales").fetchone()
    # Use whichever source has the widest date range
    _ov_dates = [d for d in [_ov_dr[0] if _ov_dr else None, _ov_dr2[0] if _ov_dr2 else None] if d]
    _ov_dates_max = [d for d in [_ov_dr[1] if _ov_dr else None, _ov_dr2[1] if _ov_dr2 else None] if d]
    _ov_earliest = datetime.strptime(min(_ov_dates), "%Y-%m-%d").date() if _ov_dates else datetime(2020, 1, 1).date()
    _ov_latest = datetime.strptime(max(_ov_dates_max), "%Y-%m-%d").date() if _ov_dates_max else datetime.utcnow().date()

    ov_start, ov_end = _smart_date_filter(_ov_earliest, _ov_latest, "ov")

    stats = load_overview_stats(date_start=str(ov_start), date_end=str(ov_end))

    # ── KPI row ──
    _k1, _k2, _k3, _k4, _k5 = st.columns(5)
    _k5.metric("Total Revenue", f"${stats['total_revenue']:,.0f}")
    _k3.metric("Shopify", f"${stats['shopify_revenue']:,.0f}")
    _k4.metric("Amazon", f"${stats['amazon_revenue']:,.0f}")
    _k1.metric("Orders", f"{stats['total_orders']:,}")
    _k2.metric("Customers", f"{stats['total_customers']:,}")

    st.markdown("")  # spacing

    # ── Revenue chart + source split ──
    col_left, col_right = st.columns([3, 1])

    with col_left:
        st.subheader("Revenue Trend")
        daily = stats["daily_trend"]
        if not daily.empty:
            daily["sale_date"] = pd.to_datetime(daily["sale_date"])

            # Pivot to get Shopify and Amazon as separate columns
            daily_pivot = daily.pivot_table(
                index="sale_date", columns="source", values="revenue", aggfunc="sum"
            ).fillna(0).reset_index()

            fig_rev = go.Figure()
            _source_colors = {"shopify": "#0F3557", "amazon": "#F58B3D"}
            for src in ["shopify", "amazon"]:
                if src in daily_pivot.columns:
                    # Add 7-day moving average for smoother visualization
                    ma_col = daily_pivot[src].rolling(7).mean()
                    fig_rev.add_trace(go.Scatter(
                        x=daily_pivot["sale_date"], y=ma_col,
                        mode="lines", name=f"{src.title()} (7d avg)",
                        line=dict(color=_source_colors.get(src, "#888"), width=2),
                        stackgroup="one",
                    ))

            # Total combined line
            daily_total = daily.groupby("sale_date")["revenue"].sum().reset_index()
            daily_total["revenue_7d"] = daily_total["revenue"].rolling(7).mean()
            fig_rev.add_trace(go.Scatter(
                x=daily_total["sale_date"], y=daily_total["revenue_7d"],
                mode="lines", name="Total (7d avg)",
                line=dict(color="#111827", width=2, dash="dot"),
            ))

            fig_rev.update_layout(
                yaxis_title="Revenue",
                height=380,
                margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(gridcolor="#E8EDF3"),
                xaxis=dict(gridcolor="#E8EDF3"),
            )
            st.plotly_chart(fig_rev, use_container_width=True)

    with col_right:
        st.subheader("Channel Mix")
        source = stats["source_split"]
        if not source.empty:
            fig = px.pie(source, values="revenue", names="source",
                         color_discrete_sequence=["#0F3557", "#F58B3D"],
                         hole=0.55)
            fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10),
                              plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                              showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.05))
            fig.update_traces(textposition="inside", textinfo="percent+label",
                              textfont_size=11)
            st.plotly_chart(fig, use_container_width=True)

    # ── Top SKUs ──
    st.subheader("Top SKUs")
    top = stats["top_skus"]
    if not top.empty:
        top = top.copy()
        top["label"] = top.apply(
            lambda r: get_flavor(r['sku'], r.get('product_name', '')) or r['sku'],
            axis=1,
        )
        fig = px.bar(top, x="total_units", y="label", orientation="h",
                     text="total_units",
                     color_discrete_sequence=["#0F3557"])
        fig.update_layout(
            height=min(360, len(top) * 36 + 40),
            margin=dict(l=0, r=0, t=10, b=0),
            yaxis=dict(categoryorder="total ascending", title=""),
            xaxis=dict(title="Units Sold", gridcolor="#E8EDF3"),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig.update_traces(textposition="outside", textfont_size=11)
        st.plotly_chart(fig, use_container_width=True)

    # ── Inventory Snapshot ──
    st.markdown("")
    st.subheader("Inventory & Demand")

    # Gather inventory from all sources
    _ov_3pl_total = 0
    _ov_fba_total = 0
    _ov_3pl_ok = False
    _ov_fba_ok = False

    if config.PACKIYO_API_TOKEN:
        try:
            from etl.packiyo_client import get_inventory as _ov_get_3pl
            _ov_3pl = _ov_get_3pl()
            _ov_3pl_total = sum(i.get("quantity_available", 0) or 0 for i in _ov_3pl)
            _ov_3pl_ok = True
        except Exception:
            pass

    _ov_has_amz = all(getattr(config, k, "") for k in [
        "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
    ])
    if _ov_has_amz:
        try:
            from etl.amazon_inventory import get_inventory as _ov_get_fba
            _ov_fba = _ov_get_fba()
            _ov_fba_total = sum(i.get("total_quantity", 0) or 0 for i in _ov_fba)
            _ov_fba_ok = True
        except Exception:
            pass

    # Demand forecast rollup
    _ov_demand_3mo = 0
    _ov_demand_12mo = 0
    try:
        import json as _json_ov
        with get_db() as conn:
            _ov_spend = get_media_spend(conn, source="All Sources")
        if not _ov_spend:
            _ov_spend = [{"month": (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m"),
                          "spend": 0, "new_customer_roas": 0.7} for i in range(12)]
        _ov_wf = _cached_waterfall(_json_ov.dumps(_ov_spend, sort_keys=True), None, 12, _load_seasonal_json())
        if not _ov_wf.empty:
            _ov_demand_3mo = _ov_wf.head(3)["total_units"].sum()
            _ov_demand_12mo = _ov_wf["total_units"].sum()
    except Exception:
        pass

    _iv1, _iv2, _iv3, _iv4, _iv5 = st.columns(5)
    _iv1.metric("3PL Stock", f"{_ov_3pl_total:,.0f}" if _ov_3pl_ok else "—")
    _iv2.metric("FBA Stock", f"{_ov_fba_total:,.0f}" if _ov_fba_ok else "—")
    _total_inv = _ov_3pl_total + _ov_fba_total
    _iv3.metric("Total Inventory", f"{_total_inv:,.0f}" if (_ov_3pl_ok or _ov_fba_ok) else "—")
    _iv4.metric("Demand (3mo)", f"{_ov_demand_3mo:,.0f}")
    _iv5.metric("Demand (12mo)", f"{_ov_demand_12mo:,.0f}")

    if (_ov_3pl_ok or _ov_fba_ok) and _ov_demand_3mo > 0:
        _monthly_demand = _ov_demand_3mo / 3
        _months_supply = _total_inv / _monthly_demand if _monthly_demand > 0 else 0
        _supply_color = "🟢" if _months_supply >= 3 else ("🟡" if _months_supply >= 1.5 else "🔴")
        st.caption(f"{_supply_color} {_months_supply:.1f} months of supply across all channels")


# ================================================================
# PAGE: RETENTION
# ================================================================
elif page == "Retention":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Retention")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify'])
            _new = get_new_rows_since_yesterday(conn, ['shopify'])
            _srcs = get_synced_sources(conn, ['shopify'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        _render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)

    col1, col2 = st.columns(2)
    skus = load_sku_list()
    sku_options = skus["sku"].tolist() if not skus.empty else []
    with col1:
        sku_filter = st.selectbox("Filter by SKU", ["All SKUs"] + sku_options)
    with col2:
        source_options = ["All Sources"] + active_sources
        source_filter = st.selectbox("Filter by Source", source_options)

    sku_val = None if sku_filter == "All SKUs" else sku_filter
    source_val = None if source_filter == "All Sources" else source_filter

    matrix = get_customer_cohort_data(sku_filter=sku_val, source_filter=source_val)

    if matrix.empty:
        st.warning("No retention data available with current filters.")
    else:
        # Date range filter — scoped to this page only
        all_cohorts = sorted(matrix.index.tolist())
        earliest = datetime.strptime(all_cohorts[0], "%Y-%m").date()
        latest = datetime.strptime(all_cohorts[-1], "%Y-%m").date()

        start_date, end_date = _smart_date_filter(earliest, latest, "ret")

        # Filter matrix to selected date range
        start_str = start_date.strftime("%Y-%m")
        end_str = end_date.strftime("%Y-%m")
        filtered_cohorts = [c for c in all_cohorts if start_str <= c <= end_str]
        matrix = matrix.loc[filtered_cohorts]

        if matrix.empty:
            st.warning("No cohorts in the selected date range.")
        else:
            st.subheader("Cohort Heatmap")
            st.caption("Each row is a monthly cohort. Values = % of customers returning at each month offset.")

            # Create heatmap
            fig = px.imshow(
                matrix.values,
                labels=dict(x="Months Since First Purchase", y="Cohort", color="Retention %"),
                x=[str(c) for c in matrix.columns],
                y=matrix.index.tolist(),
                color_continuous_scale="Blues",
                aspect="auto",
                zmin=0, zmax=1,
            )
            fig.update_layout(height=max(300, len(matrix) * 40))
            st.plotly_chart(fig, use_container_width=True)

            # Average retention curve — computed from the date-filtered matrix
            st.subheader("Retention Curve")
            st.caption("Average across selected cohorts.")

            # Build curve from the filtered matrix directly
            _curve = {}
            for col in matrix.columns:
                vals = matrix[col].dropna()
                if len(vals) >= 2:
                    _curve[int(col)] = float(vals.mean())
                elif len(vals) > 0:
                    _curve[int(col)] = float(vals.mean())

            if _curve:
                max_display = min(max(_curve.keys()), 36)
                curve_months = list(range(1, max_display + 1))
                curve_rates = [_curve.get(m, 0) for m in curve_months]

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=curve_months,
                    y=curve_rates,
                    mode="lines+markers",
                    name="Retention Rate",
                    line=dict(color="#0F3557", width=2),
                    marker=dict(size=4),
                ))
                fig.update_layout(
                    xaxis_title="Month Offset", yaxis_title="Retention %",
                    yaxis=dict(tickformat=".0%", gridcolor="#E8EDF3"),
                    xaxis=dict(gridcolor="#E8EDF3"),
                    height=320, margin=dict(l=0, r=0, t=10, b=0),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig, use_container_width=True)

            # Cohort sizes
            st.subheader("Cohort Sizes")
            sizes = get_cohort_sizes()
            if not sizes.empty:
                sizes = sizes[sizes["cohort"].between(start_str, end_str)]
            if not sizes.empty:
                fig = px.bar(sizes, x="cohort", y="cohort_size",
                             color_discrete_sequence=["#0F3557"])
                fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0),
                                  plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                                  xaxis=dict(gridcolor="#E8EDF3"),
                                  yaxis=dict(gridcolor="#E8EDF3"))
                st.plotly_chart(fig, use_container_width=True)

    # --- Seasonality Factors ---
    st.divider()
    st.subheader("Seasonality")
    st.caption("Monthly multipliers for demand forecasts. >1.0 = increased demand, <1.0 = decreased.")

    with get_db() as conn:
        _seas_enabled_val = get_setting(conn, "seasonality_enabled", "true")
        _seas_indices = get_seasonal_indices(conn)

    _seas_enabled = st.toggle("Enable Seasonality", value=(_seas_enabled_val == "true"), key="seas_toggle")

    # Build editable table
    _month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    _seas_df = pd.DataFrame({
        "Month": _month_names,
        "Index": [_seas_indices.get(m, 1.0) for m in range(1, 13)],
    })

    col_seas_edit, col_seas_chart = st.columns([1, 2])

    with col_seas_edit:
        edited_seas = st.data_editor(
            _seas_df,
            column_config={
                "Month": st.column_config.TextColumn("Month", disabled=True),
                "Index": st.column_config.NumberColumn("Seasonal Index", min_value=0.5, max_value=2.0, step=0.01, format="%.2f"),
            },
            use_container_width=True,
            hide_index=True,
            key="seas_editor",
        )

    with col_seas_chart:
        fig_seas = go.Figure()
        fig_seas.add_trace(go.Bar(
            x=_month_names,
            y=edited_seas["Index"].tolist(),
            marker_color=["#E05252" if v < 1.0 else "#2DA87E" for v in edited_seas["Index"]],
            text=[f"{v:.2f}" for v in edited_seas["Index"]],
            textposition="outside",
        ))
        fig_seas.add_shape(type="line", x0=-0.5, x1=11.5, y0=1.0, y1=1.0,
                           line=dict(dash="dash", color="gray", width=1))
        fig_seas.update_layout(
            yaxis_title="Seasonal Index",
            yaxis=dict(range=[0.5, 1.5]),
            height=350,
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig_seas, use_container_width=True)

    if st.button("Save Settings", type="primary", key="save_seas"):
        with get_db() as conn:
            set_setting(conn, "seasonality_enabled", "true" if _seas_enabled else "false")
            for i, row in edited_seas.iterrows():
                upsert_seasonal_index(conn, i + 1, float(row["Index"]))
        clear_waterfall_cache()
        st.success("Seasonality settings saved! Forecasts will update on next load.")
        st.rerun()


# ================================================================
# PAGE: DEMAND FORECAST
# ================================================================
elif page == "Demand Forecast":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Demand Forecast")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        _render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
    st.caption("Shopify retention-based + Amazon velocity-based demand with new/repeat breakdown.")

    # Seasonality status
    _seas_status_json = _load_seasonal_json()
    if _seas_status_json:
        st.caption("Seasonality enabled — indices from the Retention page.")
    else:
        st.caption("Seasonality disabled — enable on the Retention page to apply seasonal adjustments.")

    # --- Filters ---
    col_hz, col_amz_growth = st.columns(2)
    with col_hz:
        horizon = st.selectbox("Forecast Horizon", [6, 12, 18], index=1, key="wf_horizon")
    with col_amz_growth:
        amz_growth = st.number_input(
            "Amazon Monthly Growth %", min_value=-20.0, max_value=50.0, value=0.0, step=1.0,
            key="amz_growth",
            help="Expected monthly growth rate for Amazon sales (e.g., 5 = 5% monthly growth)."
        )

    # --- Media Spend Input (Shopify) ---
    with st.expander("Media Spend Plan", expanded=False):
        st.caption("Enter your monthly DTC ad budget and expected new customer ROAS. This drives the Shopify new customer forecast.")

        with get_db() as conn:
            existing_spend = get_media_spend(conn, source="All Sources")

        if existing_spend:
            spend_df = pd.DataFrame(existing_spend)
        else:
            now = datetime.utcnow()
            rows = []
            for i in range(horizon):
                m = now + relativedelta(months=i)
                rows.append({"month": m.strftime("%Y-%m"), "spend": 0.0, "new_customer_roas": 0.7})
            spend_df = pd.DataFrame(rows)

        now = datetime.utcnow()
        horizon_months_list = [(now + relativedelta(months=i)).strftime("%Y-%m") for i in range(horizon)]
        existing_months = set(spend_df["month"].tolist())
        for m in horizon_months_list:
            if m not in existing_months:
                spend_df = pd.concat([spend_df, pd.DataFrame([{"month": m, "spend": 5000.0, "new_customer_roas": 2.0}])], ignore_index=True)
        spend_df = spend_df[spend_df["month"].isin(horizon_months_list)].sort_values("month").reset_index(drop=True)

        edited_spend = st.data_editor(
            spend_df,
            column_config={
                "month": st.column_config.TextColumn("Month", disabled=True),
                "spend": st.column_config.NumberColumn("Monthly Ad Spend ($)", min_value=0, step=500, format="$%.0f"),
                "new_customer_roas": st.column_config.NumberColumn("New Customer ROAS", min_value=0.1, step=0.1, format="%.1fx"),
            },
            use_container_width=True,
            hide_index=True,
            key="spend_editor",
        )

        if st.button("Save & Recalculate", type="primary"):
            with get_db() as conn:
                for _, row in edited_spend.iterrows():
                    upsert_media_spend(conn, row["month"], row["spend"], row["new_customer_roas"], source="All Sources")
            _cached_waterfall.clear()
            _cached_sku_forecast.clear()
            st.rerun()

    # --- Amazon Revenue Forecast Input ---
    with st.expander("Amazon Revenue Forecast", expanded=False):
        st.caption("Enter your projected Amazon revenue per month. Units will be derived automatically using historical rev/unit. Leave at $0 to use velocity-based projections instead.")

        with get_db() as conn:
            existing_amz_rev = get_amazon_revenue_forecast(conn)

        now_amz = datetime.utcnow()
        amz_horizon_months = [(now_amz + relativedelta(months=i)).strftime("%Y-%m") for i in range(horizon)]

        if existing_amz_rev:
            amz_rev_df = pd.DataFrame(existing_amz_rev)
        else:
            amz_rev_df = pd.DataFrame([{"month": m, "revenue": 0.0} for m in amz_horizon_months])

        existing_amz_months = set(amz_rev_df["month"].tolist())
        for m in amz_horizon_months:
            if m not in existing_amz_months:
                amz_rev_df = pd.concat([amz_rev_df, pd.DataFrame([{"month": m, "revenue": 0.0}])], ignore_index=True)
        amz_rev_df = amz_rev_df[amz_rev_df["month"].isin(amz_horizon_months)].sort_values("month").reset_index(drop=True)

        edited_amz_rev = st.data_editor(
            amz_rev_df,
            column_config={
                "month": st.column_config.TextColumn("Month", disabled=True),
                "revenue": st.column_config.NumberColumn("Projected Revenue ($)", min_value=0, step=5000, format="$%.0f"),
            },
            use_container_width=True,
            hide_index=True,
            key="amz_rev_editor",
        )

        if st.button("Save Amazon Forecast & Recalculate", type="primary"):
            with get_db() as conn:
                for _, row in edited_amz_rev.iterrows():
                    upsert_amazon_revenue_forecast(conn, row["month"], row["revenue"])
            st.cache_data.clear()
            st.rerun()

    st.divider()

    # --- Compute all forecasts ---
    import json as _json

    # Build Amazon revenue forecast dict for the engine
    amz_rev_forecast_dict = {row["month"]: row["revenue"] for _, row in edited_amz_rev.iterrows() if row["revenue"] > 0}

    # Shopify waterfall
    media_plan = edited_spend.to_dict("records")
    media_plan_json = _json.dumps(media_plan, sort_keys=True)
    with st.spinner("Computing demand forecast..."):
        _seasonal_json = _load_seasonal_json()
        waterfall_df = _cached_waterfall(media_plan_json, None, horizon, _seasonal_json)
        shopify_sku_table = _cached_sku_forecast(waterfall_df.to_json(), None) if not waterfall_df.empty else pd.DataFrame()

        # Master DTC forecast
        dtc = build_master_dtc_forecast(
            shopify_waterfall_df=waterfall_df,
            shopify_sku_forecast_df=shopify_sku_table,
            amazon_growth_rate=amz_growth / 100.0,
            horizon_months=horizon,
            forecast_skus=FORECAST_SKUS,
            media_plan=media_plan,
            amazon_revenue_forecast=amz_rev_forecast_dict if amz_rev_forecast_dict else None,
        )

    summary_df = dtc["summary"]
    new_customer_table = dtc["new_customer_table"]
    repeat_customer_table = dtc["repeat_customer_table"]
    amazon_table = dtc["amazon_table"]
    rollup_table = dtc["rollup_table"]
    channel = dtc["channel_split"]
    revenue = dtc.get("revenue", {})

    # Sort all SKU tables by best-seller
    if not new_customer_table.empty:
        new_customer_table = sort_df_by_best_seller(new_customer_table, sku_col="SKU")
    if not repeat_customer_table.empty:
        repeat_customer_table = sort_df_by_best_seller(repeat_customer_table, sku_col="SKU")
    if not amazon_table.empty:
        amazon_table = sort_df_by_best_seller(amazon_table, sku_col="SKU")
    if not rollup_table.empty:
        rollup_table = sort_df_by_best_seller(rollup_table, sku_col="SKU")

    # --- Helper: format a SKU × Month table for display ---
    def _format_sku_month_table(df, pct_col_name, rev_dict=None):
        """
        Format a SKU × Month DataFrame for st.dataframe display.
        If rev_dict is provided, prepend a 'Projected Revenue' row at the top
        with $ formatting and a highlighted background.
        """
        if df is None or df.empty:
            return df, {}, False
        month_cols = [c for c in df.columns if c.startswith("20")]
        short_labels = {}
        for m in month_cols:
            try:
                dt = datetime.strptime(m, "%Y-%m")
                short_labels[m] = dt.strftime("%b '%y")
            except ValueError:
                short_labels[m] = m

        display = df.copy()
        has_rev_row = False

        # Prepend revenue row if provided — values pre-formatted as $ strings
        if rev_dict:
            has_rev_row = True
            rev_row = {"SKU": "💰 PROJECTED REVENUE", "Flavor": ""}
            if pct_col_name in display.columns:
                rev_row[pct_col_name] = ""
            for m in month_cols:
                val = rev_dict.get(m, 0)
                rev_row[m] = f"${val:,.0f}" if val > 0 else ""
            total_val = sum(rev_dict.get(m, 0) for m in month_cols)
            rev_row["Total"] = f"${total_val:,.0f}" if total_val > 0 else ""
            rev_df = pd.DataFrame([rev_row])
            # Convert columns to object type so strings + numbers can coexist
            for col in month_cols + ["Total"]:
                display[col] = display[col].astype(object)
            if pct_col_name in display.columns:
                display[pct_col_name] = display[pct_col_name].astype(object)
            display = pd.concat([rev_df, display], ignore_index=True)

        display = display.rename(columns=short_labels)

        # When there's a revenue row, values are mixed (strings for row 0, numbers for rest)
        # Use a lambda formatter that handles both
        if has_rev_row:
            fmt = {}
            for m in month_cols:
                col_name = short_labels[m]
                fmt[col_name] = lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else (str(v) if v else "")
            fmt["Total"] = lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else (str(v) if v else "")
            if pct_col_name in display.columns:
                fmt[pct_col_name] = lambda v: f"{v:.1f}%" if isinstance(v, (int, float)) and v > 0 else (str(v) if v else "")
        else:
            fmt = {short_labels[m]: "{:,.0f}" for m in month_cols}
            fmt["Total"] = "{:,.0f}"
            if pct_col_name in display.columns:
                fmt[pct_col_name] = "{:.1f}%"
        return display, fmt, has_rev_row

    def _style_with_rev_row(styler, has_rev_row):
        """Apply highlighting + $ formatting to the revenue row (row 0)."""
        if not has_rev_row:
            return styler

        def _highlight_rev(row):
            if row.name == 0:
                return ["background-color: #d1fae5; font-weight: bold; color: #065f46; border-bottom: 2px solid #065f46"] * len(row)
            return [""] * len(row)

        return styler.apply(_highlight_rev, axis=1)

    # --- Key Metrics ---
    metrics = _cached_aov_and_units(None)
    retention_curve = _cached_retention_curve(None)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("DTC Forecast (12mo)", f"{channel['dtc_total']:,} units")
    m2.metric("Shopify", f"{channel['shopify_total']:,}", help=f"{channel['shopify_pct']:.0f}% of DTC")
    m3.metric("Amazon", f"{channel['amazon_total']:,}", help=f"{channel['amazon_pct']:.0f}% of DTC")
    dtc_rev = channel.get("dtc_rev", 0)
    m4.metric("Forecasted Revenue", f"${dtc_rev:,.0f}" if dtc_rev > 0 else "—")
    m5.metric("Units/New Cust", f"{metrics['units_per_new_customer']:.1f}")
    m6.metric("Mo-1 Retention", f"{retention_curve.get(1, 0):.0%}")

    # --- Current Month Drawdown ---
    month_prog = get_current_month_progress()
    if not rollup_table.empty:
        remaining_demand = compute_remaining_month_demand(rollup_table, month_prog)
        total_full = sum(v["full_month"] for v in remaining_demand.values())
        total_consumed = sum(v["already_consumed"] for v in remaining_demand.values())
        total_remaining = sum(v["remaining"] for v in remaining_demand.values())

        st.divider()
        st.subheader(f"Current Month — {datetime.strptime(month_prog['current_month'], '%Y-%m').strftime('%B %Y')}")
        st.caption(
            f"Day {month_prog['day_of_month']} of {month_prog['days_in_month']} "
            f"({month_prog['pct_elapsed']:.0%} through the month). "
            f"Estimated demand already consumed vs. remaining this month."
        )
        prog_c1, prog_c2, prog_c3, prog_c4 = st.columns(4)
        prog_c1.metric("Full Month Forecast", f"{total_full:,} units")
        prog_c2.metric("Est. Already Consumed", f"{total_consumed:,} units", help=f"~{month_prog['pct_elapsed']:.0%} of monthly forecast")
        prog_c3.metric("Est. Remaining", f"{total_remaining:,} units", help=f"~{month_prog['pct_remaining']:.0%} of monthly forecast")
        prog_c4.metric("Days Remaining", f"{month_prog['days_remaining']} days")

        # Progress bar
        st.progress(month_prog["pct_elapsed"], text=f"{month_prog['pct_elapsed']:.0%} of month elapsed")

    st.divider()

    # ================================================================
    # SECTION: Master Waterfall Chart (Shopify + Amazon)
    # ================================================================
    st.subheader("Combined Forecast")

    if summary_df.empty or summary_df["total_units"].sum() == 0:
        st.warning("Not enough data to build a demand forecast. Ensure there is sales history.")
    else:
        fig = go.Figure()

        # Stacked bars: Shopify Repeat → Shopify New → Amazon
        fig.add_trace(go.Bar(
            x=summary_df["month"],
            y=summary_df["shopify_repeat_units"],
            name="Shopify Repeat",
            marker_color="#0F3557",
        ))
        fig.add_trace(go.Bar(
            x=summary_df["month"],
            y=summary_df["shopify_new_units"],
            name="Shopify New Customer",
            marker_color="#2DA87E",
        ))
        fig.add_trace(go.Bar(
            x=summary_df["month"],
            y=summary_df["amazon_units"],
            name="Amazon",
            marker_color="#F58B3D",
        ))

        # Total line
        fig.add_trace(go.Scatter(
            x=summary_df["month"],
            y=summary_df["total_units"],
            mode="lines+markers",
            name="Total DTC",
            line=dict(color="#E05252", width=2.5),
            marker=dict(size=6),
        ))

        fig.update_layout(
            barmode="stack",
            xaxis_title="Month",
            yaxis_title="Units",
            height=450,
            margin=dict(l=0, r=0, t=10, b=0),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        )
        st.plotly_chart(fig, use_container_width=True)

        # --- Monthly Summary Table ---
        disp_summary = summary_df[["month", "shopify_new_units", "shopify_repeat_units",
                                     "shopify_total_units", "amazon_units", "total_units",
                                     "shopify_new_rev", "shopify_repeat_rev",
                                     "shopify_total_rev", "amazon_rev", "total_rev"]].copy()
        disp_summary.columns = ["Month", "New Units", "Repeat Units",
                                  "Shopify Units", "Amazon Units", "DTC Units",
                                  "New Rev", "Repeat Rev", "Shopify Rev", "Amazon Rev", "DTC Rev"]
        for _uc in ["New Units", "Repeat Units", "Shopify Units", "Amazon Units", "DTC Units"]:
            if _uc in disp_summary.columns:
                disp_summary[_uc] = disp_summary[_uc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
        for _rc in ["New Rev", "Repeat Rev", "Shopify Rev", "Amazon Rev", "DTC Rev"]:
            if _rc in disp_summary.columns:
                disp_summary[_rc] = disp_summary[_rc].apply(lambda x: f"${x:,.0f}" if pd.notnull(x) else "")
        _render_df_as_html_global(disp_summary)

    # ================================================================
    # SECTION: New Customer Sales by SKU × Month
    # ================================================================
    st.divider()
    st.subheader("New Customer Sales by SKU")
    st.caption("Forecasted new customer demand per SKU per month. Mix is based on what first-time customers actually buy. Driven by media spend plan.")

    if not new_customer_table.empty:
        nc_display, nc_fmt, nc_has_rev = _format_sku_month_table(
            new_customer_table, "% of New Sales", rev_dict=revenue.get("new_customer"),
        )
        _render_df_as_html_global(nc_display, max_height=min(len(nc_display) * 35 + 38, 600))

        nc_total = new_customer_table["Total"].sum()
        rev_metrics = revenue.get("metrics", {})
        nc_rev_per_unit = rev_metrics.get("new_customer_rev_per_unit", 0)
        st.markdown(f"**Total New Customer Units ({horizon}mo):** {nc_total:,.0f} · **Rev/Unit:** ${nc_rev_per_unit:.2f}")
    else:
        st.info("No new customer sales data available.")

    # ================================================================
    # SECTION: Repeat Customer Sales by SKU × Month
    # ================================================================
    st.divider()
    st.subheader("Repeat Customer Sales by SKU")
    st.caption("Forecasted repeat customer demand per SKU per month. Uses retention curve decay applied to each historical cohort, distributed by repeat customer SKU mix.")

    if not repeat_customer_table.empty:
        rc_display, rc_fmt, rc_has_rev = _format_sku_month_table(
            repeat_customer_table, "% of Repeat Sales", rev_dict=revenue.get("repeat_customer"),
        )
        _render_df_as_html_global(rc_display, max_height=min(len(rc_display) * 35 + 38, 600))

        rc_total = repeat_customer_table["Total"].sum()
        rev_metrics = revenue.get("metrics", {})
        rc_rev_per_unit = rev_metrics.get("repeat_rev_per_unit", 0)
        st.markdown(f"**Total Repeat Customer Units ({horizon}mo):** {rc_total:,.0f} · **Rev/Unit:** ${rc_rev_per_unit:.2f}")
    else:
        st.info("No repeat customer sales data available.")

    # ================================================================
    # SECTION: Amazon Sales by SKU × Month
    # ================================================================
    if "amazon" in active_sources or (not amazon_table.empty):
        st.divider()
        st.subheader("Amazon Sales by SKU")
        st.caption(
            "Amazon demand forecasted from sales velocity (blends 7-day and 30-day averages). "
            "Amazon doesn't provide customer-level data, so this is a trend-based projection."
        )

        if not amazon_table.empty:
            # Amazon metrics (above table)
            amz_total = amazon_table["Total"].sum()
            velocity = get_amazon_sku_velocity()
            total_daily = sum(v["avg_daily"] for v in velocity.values()) if velocity else 0
            rev_metrics = revenue.get("metrics", {})
            amz_rpu = rev_metrics.get("amazon_rev_per_unit", 0)

            amz_c1, amz_c2, amz_c3, amz_c4 = st.columns(4)
            amz_c1.metric("Amazon Daily Run Rate", f"{total_daily:.0f} units/day")
            amz_c2.metric("Amazon Monthly Run Rate", f"{total_daily * 30.44:,.0f} units/month")
            amz_c3.metric(f"Amazon {horizon}mo Forecast", f"{amz_total:,.0f} units")
            amz_c4.metric("Amazon Rev/Unit", f"${amz_rpu:.2f}" if amz_rpu > 0 else "—")

            amz_display, amz_fmt, amz_has_rev = _format_sku_month_table(
                amazon_table, "% of Sales", rev_dict=revenue.get("amazon"),
            )
            _render_df_as_html_global(amz_display, max_height=min(len(amz_display) * 35 + 38, 600))
        else:
            st.info("No Amazon sales data available for forecasting.")

    # ================================================================
    # SECTION: Master DTC Rollup by SKU × Month
    # ================================================================
    st.divider()
    st.subheader("Master Demand by SKU")
    st.caption("Combined Shopify (new + repeat) + Amazon demand per SKU per month. Use this for production planning and reorder decisions.")

    if not rollup_table.empty:
        ru_display, ru_fmt, ru_has_rev = _format_sku_month_table(
            rollup_table, "% of Sales", rev_dict=revenue.get("rollup"),
        )
        _render_df_as_html_global(ru_display, max_height=min(len(ru_display) * 35 + 38, 700))

        grand_total = rollup_table["Total"].sum()
        dtc_rev_total = channel.get("dtc_rev", 0)
        st.markdown(f"**Grand Total DTC Demand ({horizon}mo):** {grand_total:,.0f} units · **Revenue:** ${dtc_rev_total:,.0f}")
    else:
        st.info("No SKU demand data available.")

    # --- Per-SKU Prophet Forecast (kept as expander) ---
    st.divider()
    with st.expander("Per-SKU Forecast (Prophet)", expanded=False):
        skus = load_sku_list()
        if skus.empty:
            st.info("No SKU data available yet.")
            selected_sku = None
        else:
            selected_sku = st.selectbox("Select SKU", skus["sku"].tolist(), key="sku_forecast")

        if selected_sku:
            with st.spinner(f"Running forecast for {selected_sku}..."):
                result = forecast_sku(selected_sku)

            if result is None:
                st.error("No data available for this SKU.")
            else:
                col1, col2, col3 = st.columns(3)
                col1.metric("Forecast Method", result["method"].title())
                col2.metric("Avg Daily Demand", f"{result['reorder_info']['avg_daily_demand']} units")
                col3.metric("Reorder Point", f"{int(result['reorder_info']['reorder_point'])} units")

                hist = result["history"]
                fc = result["forecast"]
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=hist["ds"], y=hist["y"],
                    mode="lines", name="Actual Sales",
                    line=dict(color="#0F3557", width=1.5),
                ))
                fig.add_trace(go.Scatter(
                    x=fc["ds"], y=fc["yhat"],
                    mode="lines", name="Forecast",
                    line=dict(color="#F58B3D", width=2),
                ))
                fig.add_trace(go.Scatter(
                    x=pd.concat([fc["ds"], fc["ds"][::-1]]),
                    y=pd.concat([fc["yhat_upper"], fc["yhat_lower"][::-1]]),
                    fill="toself",
                    fillcolor="rgba(245,158,11,0.15)",
                    line=dict(color="rgba(255,255,255,0)"),
                    name="80% Confidence",
                ))
                fig.update_layout(
                    height=400,
                    margin=dict(l=0, r=0, t=10, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                )
                st.plotly_chart(fig, use_container_width=True)

                ri = result["reorder_info"]
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Lead Time", f"{ri['lead_time_days']} days")
                col2.metric("Lead Time Demand", f"{int(ri['lead_time_demand'])} units")
                col3.metric("Safety Stock", f"{int(ri['safety_stock'])} units")
                col4.metric("Reorder Point", f"{int(ri['reorder_point'])} units")

                monthly = result["monthly_forecast"]
                _render_df_as_html_global(monthly)


# ================================================================
# PAGE: PROJECTED INVENTORY
# ================================================================
elif page == "Projected Inventory":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Projected Inventory")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        _render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
    st.caption("Combines current inventory across all channels (3PL + Amazon FBA) with the full DTC demand forecast (Shopify + Amazon) to project inventory levels per SKU per month.")

    # --- Fetch live inventory from all sources ---
    pi_inv_3pl = []
    pi_inv_amz = []
    pi_has_3pl = False
    pi_has_amz = False

    with st.spinner("Fetching live inventory..."):
        if config.PACKIYO_API_TOKEN:
            try:
                from etl.packiyo_client import get_inventory as pi_get_3pl
                pi_inv_3pl = pi_get_3pl()
                pi_has_3pl = True
            except Exception as e:
                st.warning(f"Could not fetch 3PL inventory: {e}")

        pi_has_amz_creds = all(getattr(config, k, "") for k in [
            "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
        ])
        if pi_has_amz_creds:
            try:
                from etl.amazon_inventory import get_inventory as pi_get_fba
                pi_inv_amz = pi_get_fba()
                pi_has_amz = True
            except Exception as e:
                st.warning(f"Could not fetch Amazon FBA inventory: {e}")

    pi_has_inv = pi_has_3pl or pi_has_amz

    # Merge inventory across sources
    pi_combined = {}
    for item in pi_inv_3pl:
        sku = item["sku"]
        pi_combined[sku] = {
            "sku": sku,
            "name": item.get("name", ""),
            "3pl_available": item.get("quantity_available", 0) or 0,
            "fba_fulfillable": 0,
            "total_available": item.get("quantity_available", 0) or 0,
            "inbound": item.get("quantity_inbound", 0) or 0,
        }
    for item in pi_inv_amz:
        sku = item["sku"]
        # Use total_quantity (fulfillable + reserved + inbound + unfulfillable)
        # for inventory planning, since inbound stock will become available.
        fba_total = item.get("total_quantity", 0) or 0
        fba_inbound = (item.get("inbound_shipped", 0) or 0) + (item.get("inbound_receiving", 0) or 0)
        if sku in pi_combined:
            pi_combined[sku]["fba_fulfillable"] = fba_total
            pi_combined[sku]["total_available"] += fba_total
            pi_combined[sku]["inbound"] += fba_inbound
        else:
            pi_combined[sku] = {
                "sku": sku,
                "name": item.get("product_name", ""),
                "3pl_available": 0,
                "fba_fulfillable": fba_total,
                "total_available": fba_total,
                "inbound": fba_inbound,
            }

    if not pi_has_inv:
        st.warning("No inventory sources connected. Connect Packiyo (3PL) or Amazon (FBA) in **Settings** to see projected inventory.")
    else:
        # --- Get the demand forecast ---
        pi_forecast_ok = False
        pi_rollup = pd.DataFrame()
        pi_revenue = {}
        try:
            import json as _json_pi
            with get_db() as conn:
                pi_spend = get_media_spend(conn, source="All Sources")
                pi_amz_rev = get_amazon_revenue_forecast(conn)
            if not pi_spend:
                pi_spend = [{"month": (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m"),
                             "spend": 0, "new_customer_roas": 0.7} for i in range(12)]
            pi_amz_dict = {r["month"]: r["revenue"] for r in pi_amz_rev if r.get("revenue", 0) > 0} if pi_amz_rev else None

            pi_media_json = _json_pi.dumps(pi_spend, sort_keys=True)
            pi_wf = _cached_waterfall(pi_media_json, None, 12, _load_seasonal_json())
            pi_sku_fc = _cached_sku_forecast(pi_wf.to_json(), None) if not pi_wf.empty else pd.DataFrame()

            pi_dtc = build_master_dtc_forecast(
                shopify_waterfall_df=pi_wf,
                shopify_sku_forecast_df=pi_sku_fc,
                amazon_growth_rate=0.0,
                horizon_months=12,
                forecast_skus=FORECAST_SKUS,
                media_plan=pi_spend,
                amazon_revenue_forecast=pi_amz_dict,
            )
            pi_rollup = pi_dtc["rollup_table"]
            pi_revenue = pi_dtc.get("revenue", {})
            pi_forecast_ok = not pi_rollup.empty
        except Exception as e:
            st.warning(f"Could not load demand forecast: {e}")

        if not pi_forecast_ok:
            st.warning("No demand forecast available. Set up your media spend plan in **Demand Forecast** first.")
        else:
            # --- Month progress ---
            mp = get_current_month_progress()
            current_month = mp["current_month"]

            month_cols = [c for c in pi_rollup.columns if c.startswith("20")]
            month_labels = {}
            for m in month_cols:
                try:
                    dt = datetime.strptime(m, "%Y-%m")
                    month_labels[m] = dt.strftime("%b '%y")
                except ValueError:
                    month_labels[m] = m

            # --- Planned Inbound Editor ---
            with st.expander("Planned Inbound", expanded=True):
                st.caption(
                    "Enter units landing per SKU per month. These get added to projected inventory "
                    "in the month they arrive. Click **Save & Recalculate** to update projections."
                )

                # Load existing planned inbound from DB
                with get_db() as conn:
                    existing_inbound = get_planned_inbound_dict(conn)

                # Build the editable matrix: rows = SKUs (sorted by best-seller), columns = months
                _rank = get_sku_sales_rank()
                _max_rank = len(_rank)
                sorted_skus = sorted(FORECAST_SKUS, key=lambda s: _rank.get(s, _max_rank))
                inbound_rows = []
                for sku in sorted_skus:
                    row_data = {"SKU": sku, "Flavor": get_flavor(sku)}
                    sku_inbound = existing_inbound.get(sku, {})
                    for m in month_cols:
                        row_data[m] = sku_inbound.get(m, 0)
                    inbound_rows.append(row_data)

                inbound_edit_df = pd.DataFrame(inbound_rows)

                # Rename month columns to short labels for display
                inbound_display_cols = {"SKU": "SKU", "Flavor": "Flavor"}
                inbound_col_config = {
                    "SKU": st.column_config.TextColumn("SKU", disabled=True, width="small"),
                    "Flavor": st.column_config.TextColumn("Flavor", disabled=True, width="small"),
                }
                for m in month_cols:
                    short = month_labels[m]
                    inbound_display_cols[m] = short
                    inbound_col_config[short] = st.column_config.NumberColumn(
                        short, min_value=0, step=100, default=0,
                    )

                inbound_edit_display = inbound_edit_df.rename(columns=inbound_display_cols)

                edited_inbound = st.data_editor(
                    inbound_edit_display,
                    column_config=inbound_col_config,
                    use_container_width=True,
                    hide_index=True,
                    height=min(len(inbound_edit_display) * 35 + 38, 700),
                    key="planned_inbound_editor",
                )

                if st.button("Save & Recalculate", type="primary", key="save_inbound"):
                    # Map short labels back to YYYY-MM
                    reverse_labels = {v: k for k, v in month_labels.items()}
                    with get_db() as conn:
                        for _, row in edited_inbound.iterrows():
                            sku = row["SKU"]
                            for col in edited_inbound.columns:
                                if col in ("SKU", "Flavor"):
                                    continue
                                m = reverse_labels.get(col, col)
                                units = int(row[col] or 0)
                                upsert_planned_inbound(conn, sku, m, units)
                    st.rerun()

            # --- Load planned inbound for projection ---
            with get_db() as conn:
                planned_inbound = get_planned_inbound_dict(conn)

            # Also read edited values (before save) so live preview works
            # Map the edited display back to {sku: {month: units}}
            reverse_labels_preview = {v: k for k, v in month_labels.items()}
            live_inbound = {}
            for _, row in edited_inbound.iterrows():
                sku = row["SKU"]
                live_inbound[sku] = {}
                for col in edited_inbound.columns:
                    if col in ("SKU", "Flavor"):
                        continue
                    m = reverse_labels_preview.get(col, col)
                    live_inbound[sku][m] = int(row[col] or 0)

            # Use live (edited but possibly unsaved) values for preview
            inbound_for_projection = live_inbound

            st.divider()

            # --- Build projected inventory table ---
            # For each SKU: start with current inventory (3PL + FBA), subtract monthly demand, add planned inbound
            proj_rows = []
            _pi_rank = get_sku_sales_rank()
            _pi_max_rank = len(_pi_rank)
            for sku in sorted(FORECAST_SKUS, key=lambda s: _pi_rank.get(s, _pi_max_rank)):
                inv = pi_combined.get(sku, {"3pl_available": 0, "fba_fulfillable": 0, "total_available": 0, "inbound": 0, "name": ""})
                fc_row = pi_rollup[pi_rollup["SKU"] == sku]
                sku_planned = inbound_for_projection.get(sku, {})

                flavor = get_flavor(sku)
                starting_inv = inv["total_available"] + inv["inbound"]
                running_inv = starting_inv

                row = {
                    "SKU": sku,
                    "Flavor": flavor,
                    "3PL": inv["3pl_available"],
                    "FBA": inv["fba_fulfillable"],
                    "In Transit": inv["inbound"],
                    "Total Stock": inv["total_available"] + inv["inbound"],
                }

                for m in month_cols:
                    demand = 0
                    if not fc_row.empty and m in fc_row.columns:
                        demand = int(fc_row.iloc[0][m] or 0)

                    # For current month, only subtract remaining demand
                    if m == current_month:
                        demand = round(demand * mp["pct_remaining"])

                    # Add planned inbound for this month
                    landing = sku_planned.get(m, 0)
                    running_inv = running_inv - demand + landing
                    row[m] = round(running_inv)

                proj_rows.append(row)

            proj_df = pd.DataFrame(proj_rows)

            # --- KPI summary ---
            total_3pl = sum(pi_combined.get(s, {}).get("3pl_available", 0) for s in FORECAST_SKUS)
            total_fba = sum(pi_combined.get(s, {}).get("fba_fulfillable", 0) for s in FORECAST_SKUS)
            total_inv = total_3pl + total_fba
            total_in_transit = sum(pi_combined.get(s, {}).get("inbound", 0) for s in FORECAST_SKUS)
            total_planned = sum(
                sum(inbound_for_projection.get(s, {}).get(m, 0) for m in month_cols)
                for s in FORECAST_SKUS
            )

            # Find SKUs that go negative (stockout) and when
            stockout_skus = []
            for _, row in proj_df.iterrows():
                for m in month_cols:
                    if row[m] < 0:
                        stockout_skus.append({
                            "sku": row["SKU"],
                            "flavor": row["Flavor"],
                            "month": m,
                            "deficit": abs(row[m]),
                        })
                        break  # first stockout month only

            kc1, kc2, kc3, kc4, kc5, kc6 = st.columns(6)
            kc1.metric("3PL Stock", f"{total_3pl:,}" if pi_has_3pl else "—")
            kc2.metric("FBA Stock", f"{total_fba:,}" if pi_has_amz else "—")
            kc3.metric("Combined Stock", f"{total_inv:,}")
            kc4.metric("In Transit", f"{total_in_transit:,}")
            kc5.metric("Planned Inbound", f"{total_planned:,}", help="Total units you've entered in the planned inbound table")
            kc6.metric("Projected Stockouts", f"{len(stockout_skus)} SKUs", help="SKUs projected to go negative within forecast window")

            st.caption(
                f"Current month: {datetime.strptime(current_month, '%Y-%m').strftime('%B %Y')} "
                f"(day {mp['day_of_month']}/{mp['days_in_month']} — only remaining {mp['days_remaining']} days of demand deducted for this month)"
            )

            st.divider()

            # --- Projected Inventory Table ---
            st.subheader("Projected Inventory by SKU")
            st.caption(
                "Starting from combined 3PL + FBA stock + in transit, subtract total forecasted demand (Shopify + Amazon) "
                "and add planned inbound each month. Negative (red) = projected stockout. Low stock (yellow) = under 500 units."
            )

            # Format for display
            disp_proj = proj_df.copy()
            disp_proj = disp_proj.rename(columns=month_labels)
            disp_month_labels = [month_labels[m] for m in month_cols]

            # Color function: red for negative, yellow for low
            def _color_inv(val):
                if isinstance(val, (int, float)):
                    if val < 0:
                        return "background-color: #fee2e2; color: #991b1b; font-weight: bold"
                    elif val < 500:
                        return "background-color: #fef3c7; color: #92400e"
                return ""

            inv_fmt = {label: "{:,.0f}" for label in disp_month_labels}
            inv_fmt["3PL"] = "{:,.0f}"
            inv_fmt["FBA"] = "{:,.0f}"
            inv_fmt["In Transit"] = "{:,.0f}"
            inv_fmt["Total Stock"] = "{:,.0f}"

            _render_df_as_html_global(disp_proj, max_height=min(len(disp_proj) * 35 + 38, 700))

            # --- Stockout Alerts ---
            if stockout_skus:
                st.divider()
                st.subheader("Projected Stockouts")
                st.caption("SKUs projected to run out of inventory if no reorder is placed.")

                for so in sorted(stockout_skus, key=lambda x: x["month"]):
                    mo_label = datetime.strptime(so["month"], "%Y-%m").strftime("%B %Y")
                    st.warning(
                        f"**{so['sku']}** ({so['flavor']}) — projected stockout in **{mo_label}** "
                        f"(deficit: {so['deficit']:,} units)"
                    )

            # --- Inventory Runway Chart ---
            st.divider()
            st.subheader("Inventory Runway")
            st.caption("Visual projection of inventory levels over time. Lines that dip below zero indicate stockouts.")

            fig_pi = go.Figure()
            for _, row in proj_df.iterrows():
                sku = row["SKU"]
                flavor = row["Flavor"] or sku
                inv_vals = [row[m] for m in month_cols]

                fig_pi.add_trace(go.Scatter(
                    x=[month_labels[m] for m in month_cols],
                    y=inv_vals,
                    mode="lines+markers",
                    name=flavor,
                    marker=dict(size=4),
                ))

            fig_pi.add_shape(
                type="line", x0=0, x1=1, xref="paper", y0=0, y1=0,
                line=dict(dash="solid", color="#E05252", width=1.5),
            )
            fig_pi.add_annotation(
                x=0, xref="paper", y=0, text="Stockout Line",
                showarrow=False, yanchor="bottom", xanchor="left",
                font=dict(size=11, color="#E05252"),
            )
            fig_pi.update_layout(
                height=500,
                margin=dict(l=0, r=0, t=10, b=0),
                xaxis_title="Month",
                yaxis_title="Projected Inventory (units)",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            )
            st.plotly_chart(fig_pi, use_container_width=True)


# ================================================================
# PAGE: 3PL INVENTORY
# ================================================================
elif page == "3PL Inventory":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("3PL Inventory")
    if not config.PACKIYO_API_TOKEN:
        with _badge_col:
            _render_freshness_badge(is_fallback=True, source='Packiyo (no token)')
        st.caption("Showing cached sales velocity — Packiyo API token not configured.")
        st.info("Live 3PL inventory requires a Packiyo API token. Add it in **Settings** or set the `PACKIYO_API_TOKEN` environment variable.")

        # Fallback: show recent sales velocity from database
        with get_db() as conn:
            _fb_df = pd.read_sql_query("""
                SELECT sku, SUM(units_sold) as units_30d, SUM(revenue) as revenue_30d,
                       ROUND(SUM(units_sold) / 30.0, 1) as daily_velocity
                FROM daily_sku_sales
                WHERE sale_date >= date('now', '-30 days') AND source = 'shopify'
                GROUP BY sku ORDER BY units_30d DESC
            """, conn)
        if not _fb_df.empty:
            _fb_df = sort_df_by_best_seller(_fb_df, sku_col="sku")
            _fb_df.insert(1, "Flavor", _fb_df["sku"].apply(lambda s: get_flavor(s, "")))
            st.subheader("Recent DTC Sales Velocity (last 30 days)")
            st.caption("Showing Shopify sales data as a reference while live 3PL inventory is unavailable.")
            _fb_display = _fb_df.rename(columns={"sku": "SKU", "units_30d": "Units (30d)",
                                                  "revenue_30d": "Revenue (30d)", "daily_velocity": "Daily Avg"})
            for _fc in ["Units (30d)", "Daily Avg"]:
                if _fc in _fb_display.columns:
                    _fb_display[_fc] = _fb_display[_fc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            _fb_display["Revenue (30d)"] = _fb_display["Revenue (30d)"].apply(lambda x: f"${x:,.0f}" if pd.notnull(x) else "")
            _render_df_as_html_global(_fb_display, max_height=min(len(_fb_display) * 35 + 38, 700))
    else:
        from etl.packiyo_client import get_inventory

        with st.spinner("Fetching inventory from Packiyo..."):
            try:
                inv = get_inventory()
            except Exception as e:
                inv = None
                st.error(f"Failed to fetch inventory: {e}")

        with _badge_col:
            if inv:
                _render_freshness_badge(
                    is_live=True,
                    source='Packiyo API',
                    last_refreshed_str=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    new_rows=len(inv),
                )
            else:
                _render_freshness_badge(is_fallback=True, source='Packiyo API (error)')
        st.caption("Live stock from Packiyo.")

        if inv:
            inv_df = pd.DataFrame(inv)

            # Filter options
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                show_option = st.radio(
                    "Show",
                    ["Core Forecast SKUs", "All SKUs with Stock", "All SKUs"],
                    horizontal=True,
                    key="inv_show",
                )
            with col_f2:
                search = st.text_input("Search SKU or Name", key="inv_search")

            if show_option == "Core Forecast SKUs":
                inv_df = inv_df[inv_df["sku"].isin(FORECAST_SKUS)]
            elif show_option == "All SKUs with Stock":
                inv_df = inv_df[inv_df["quantity_on_hand"] > 0]

            if search:
                mask = (
                    inv_df["sku"].str.contains(search, case=False, na=False)
                    | inv_df["name"].str.contains(search, case=False, na=False)
                )
                inv_df = inv_df[mask]

            inv_df = sort_df_by_best_seller(inv_df, sku_col="sku")

            # KPI cards
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total SKUs", len(inv_df))
            col2.metric("Total On Hand", f"{inv_df['quantity_on_hand'].sum():,.0f}")
            col3.metric("Total Available", f"{inv_df['quantity_available'].sum():,.0f}")
            col4.metric("Total Allocated", f"{inv_df['quantity_allocated'].sum():,.0f}")

            st.divider()

            # Main inventory table
            display_cols = ["sku", "name", "quantity_on_hand", "quantity_allocated",
                            "quantity_available", "quantity_backordered", "quantity_inbound"]
            display_df = inv_df[display_cols].copy()
            display_df.columns = ["SKU", "Product", "On Hand", "Allocated",
                                  "Available", "Backordered", "Inbound"]
            display_df.insert(1, "Flavor", [
                get_flavor(row["sku"], row["name"]) for _, row in inv_df[["sku", "name"]].iterrows()
            ])

            for _ic in ["On Hand", "Allocated", "Available", "Backordered", "Inbound"]:
                if _ic in display_df.columns:
                    display_df[_ic] = display_df[_ic].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            _render_df_as_html_global(display_df, max_height=min(len(display_df) * 35 + 38, 700))

            # Forecast vs Inventory comparison (core SKUs only)
            st.divider()
            st.subheader("Forecast vs. Current Inventory")
            st.caption("Compares your demand forecast with current 3PL stock to flag potential shortfalls.")

            # Get current forecast for the core SKUs
            try:
                import json as _json_inv
                wf_source_inv = None
                with get_db() as conn:
                    existing_spend_inv = get_media_spend(conn, source="All Sources")
                if existing_spend_inv:
                    media_plan_inv = existing_spend_inv
                else:
                    media_plan_inv = [{"month": (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m"),
                                       "spend": 0, "new_customer_roas": 0.7} for i in range(12)]
                media_json_inv = _json_inv.dumps(media_plan_inv, sort_keys=True)
                wf_inv = _cached_waterfall(media_json_inv, wf_source_inv, 3, _load_seasonal_json())

                if not wf_inv.empty:
                    sku_fc = _cached_sku_forecast(wf_inv.to_json(), wf_source_inv)
                    if not sku_fc.empty:
                        sku_fc = sku_fc[sku_fc["SKU"].isin(FORECAST_SKUS)].copy()
                        if "Variant" in sku_fc.columns:
                            sku_fc = sku_fc.drop(columns=["Variant"])

                        # Sum next 3 months forecast
                        month_cols = [c for c in sku_fc.columns if c != "SKU"]
                        sku_fc["forecast_3mo"] = sku_fc[month_cols].sum(axis=1)

                        # Merge with inventory
                        inv_core = inv_df[inv_df["sku"].isin(FORECAST_SKUS)][["sku", "quantity_available"]].copy()
                        comparison = sku_fc[["SKU", "forecast_3mo"]].merge(
                            inv_core, left_on="SKU", right_on="sku", how="left"
                        ).drop(columns=["sku"])
                        comparison["quantity_available"] = comparison["quantity_available"].fillna(0)
                        comparison["months_of_stock"] = (
                            comparison["quantity_available"] / (comparison["forecast_3mo"] / 3)
                        ).round(1)
                        comparison["months_of_stock"] = comparison["months_of_stock"].replace(
                            [float("inf"), float("-inf")], 0
                        ).fillna(0)
                        comparison.columns = ["SKU", "Forecast (3mo)", "Available", "Months of Stock"]
                        comparison.insert(1, "Flavor", comparison["SKU"].map(lambda s: get_flavor(s)))
                        comparison = sort_df_by_best_seller(comparison, sku_col="SKU")

                        def _color_stock(val):
                            if val < 1:
                                return "background-color: #fee2e2"  # red — less than 1 month
                            elif val < 2:
                                return "background-color: #fef3c7"  # yellow — 1-2 months
                            return ""

                        for _cc in ["Forecast (3mo)", "Available"]:
                            if _cc in comparison.columns:
                                comparison[_cc] = comparison[_cc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
                        if "Months of Stock" in comparison.columns:
                            comparison["Months of Stock"] = comparison["Months of Stock"].apply(lambda x: f"{x:.1f}" if pd.notnull(x) else "")
                        _render_df_as_html_global(comparison, max_height=min(len(comparison) * 35 + 38, 700))
                    else:
                        st.info("No SKU forecast data available for comparison.")
                else:
                    st.info("No waterfall forecast available for comparison.")
            except Exception as e:
                st.warning(f"Could not load forecast comparison: {e}")


# ================================================================
# PAGE: AMAZON INVENTORY
# ================================================================
elif page == "Amazon Inventory":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Amazon Inventory")

    has_amazon_creds = all(getattr(config, k, "") for k in [
        "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
    ])

    if not has_amazon_creds:
        with _badge_col:
            _render_freshness_badge(is_fallback=True, source='Amazon SP-API (no creds)')
        st.caption("Showing cached sales velocity — Amazon SP-API credentials not configured.")
        st.info("Live Amazon inventory requires SP-API credentials. Add them in **Settings** or set the `AMAZON_REFRESH_TOKEN`, `AMAZON_LWA_CLIENT_ID`, and `AMAZON_LWA_CLIENT_SECRET` environment variables.")

        # Fallback: show recent Amazon sales velocity from database
        with get_db() as conn:
            _amz_fb = pd.read_sql_query("""
                SELECT sku, SUM(units_sold) as units_30d, SUM(revenue) as revenue_30d,
                       ROUND(SUM(units_sold) / 30.0, 1) as daily_velocity
                FROM daily_sku_sales
                WHERE sale_date >= date('now', '-30 days') AND source = 'amazon'
                GROUP BY sku ORDER BY units_30d DESC
            """, conn)
        if not _amz_fb.empty:
            _amz_fb = sort_df_by_best_seller(_amz_fb, sku_col="sku")
            _amz_fb.insert(1, "Flavor", _amz_fb["sku"].apply(lambda s: get_flavor(s, "")))
            st.subheader("Recent Amazon Sales Velocity (last 30 days)")
            st.caption("Showing Amazon sales data as a reference while live FBA inventory is unavailable.")
            _amz_fb_display = _amz_fb.rename(columns={"sku": "SKU", "units_30d": "Units (30d)",
                                                       "revenue_30d": "Revenue (30d)", "daily_velocity": "Daily Avg"})
            for _fc in ["Units (30d)", "Daily Avg"]:
                if _fc in _amz_fb_display.columns:
                    _amz_fb_display[_fc] = _amz_fb_display[_fc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            _amz_fb_display["Revenue (30d)"] = _amz_fb_display["Revenue (30d)"].apply(lambda x: f"${x:,.0f}" if pd.notnull(x) else "")
            _render_df_as_html_global(_amz_fb_display, max_height=min(len(_amz_fb_display) * 35 + 38, 700))
    else:
        from etl.amazon_inventory import get_inventory as get_amz_inventory

        with st.spinner("Fetching FBA inventory from Amazon..."):
            try:
                amz_inv = get_amz_inventory()
            except Exception as e:
                amz_inv = None
                st.error(f"Failed to fetch Amazon inventory: {e}")

        with _badge_col:
            if amz_inv:
                _render_freshness_badge(
                    is_live=True,
                    source='Amazon SP-API',
                    last_refreshed_str=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    new_rows=len(amz_inv),
                )
            else:
                _render_freshness_badge(is_fallback=True, source='Amazon SP-API (error)')
        st.caption("Live FBA stock from Seller Central.")

        if amz_inv:
            amz_df = pd.DataFrame(amz_inv)

            # Filter options
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                amz_show = st.radio(
                    "Show",
                    ["All with Stock", "Core Forecast SKUs", "All SKUs"],
                    horizontal=True,
                    key="amz_inv_show",
                )
            with col_f2:
                amz_search = st.text_input("Search SKU, ASIN, or Name", key="amz_inv_search")

            if amz_show == "Core Forecast SKUs":
                amz_df = amz_df[amz_df["sku"].isin(FORECAST_SKUS)]
            elif amz_show == "All with Stock":
                amz_df = amz_df[amz_df["total_quantity"] > 0]

            if amz_search:
                mask = (
                    amz_df["sku"].str.contains(amz_search, case=False, na=False)
                    | amz_df["asin"].str.contains(amz_search, case=False, na=False)
                    | amz_df["product_name"].str.contains(amz_search, case=False, na=False)
                )
                amz_df = amz_df[mask]

            amz_df = sort_df_by_best_seller(amz_df, sku_col="sku")

            # KPI cards
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total SKUs", len(amz_df))
            col2.metric("Fulfillable", f"{amz_df['fulfillable_quantity'].sum():,.0f}")
            col3.metric("Inbound", f"{(amz_df['inbound_working'].sum() + amz_df['inbound_shipped'].sum() + amz_df['inbound_receiving'].sum()):,.0f}")
            col4.metric("Reserved", f"{amz_df['reserved_quantity'].sum():,.0f}")

            st.divider()

            # Main inventory table
            display_amz = amz_df[["sku", "asin", "product_name", "fulfillable_quantity",
                                   "reserved_quantity", "inbound_shipped", "inbound_receiving",
                                   "unfulfillable_quantity", "total_quantity"]].copy()
            display_amz.columns = ["SKU", "ASIN", "Product", "Fulfillable",
                                   "Reserved", "Inbound Shipped", "Inbound Receiving",
                                   "Unfulfillable", "Total"]
            display_amz.insert(1, "Flavor", [
                get_flavor(row["sku"], row["product_name"]) for _, row in amz_df[["sku", "product_name"]].iterrows()
            ])

            for _ac in ["Fulfillable", "Reserved", "Inbound Shipped", "Inbound Receiving", "Unfulfillable", "Total"]:
                if _ac in display_amz.columns:
                    display_amz[_ac] = display_amz[_ac].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            _render_df_as_html_global(display_amz, max_height=min(len(display_amz) * 35 + 38, 700))

            # Forecast vs Amazon Inventory comparison
            st.divider()
            st.subheader("Forecast vs. Amazon FBA Stock")
            st.caption("Compares Amazon demand forecast with FBA inventory to flag shortfalls.")

            try:
                import json as _json_amz
                with get_db() as conn:
                    existing_spend_amz = get_media_spend(conn, source="All Sources")
                if existing_spend_amz:
                    media_plan_amz = existing_spend_amz
                else:
                    media_plan_amz = [{"month": (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m"),
                                       "spend": 0, "new_customer_roas": 0.7} for i in range(3)]
                media_json_amz = _json_amz.dumps(media_plan_amz, sort_keys=True)

                # Try to get Amazon-specific forecast, fall back to combined
                amz_source = "amazon" if "amazon" in active_sources else None
                wf_amz = _cached_waterfall(media_json_amz, amz_source, 3, _load_seasonal_json())

                if not wf_amz.empty:
                    sku_fc_amz = _cached_sku_forecast(wf_amz.to_json(), amz_source)
                    if not sku_fc_amz.empty:
                        sku_fc_amz = sku_fc_amz[sku_fc_amz["SKU"].isin(FORECAST_SKUS)].copy()
                        if "Variant" in sku_fc_amz.columns:
                            sku_fc_amz = sku_fc_amz.drop(columns=["Variant"])

                        month_cols_amz = [c for c in sku_fc_amz.columns if c != "SKU"]
                        sku_fc_amz["forecast_3mo"] = sku_fc_amz[month_cols_amz].sum(axis=1)

                        amz_core = amz_df[amz_df["sku"].isin(FORECAST_SKUS)][["sku", "total_quantity"]].copy()
                        comparison_amz = sku_fc_amz[["SKU", "forecast_3mo"]].merge(
                            amz_core, left_on="SKU", right_on="sku", how="left"
                        ).drop(columns=["sku"])
                        comparison_amz["total_quantity"] = comparison_amz["total_quantity"].fillna(0)
                        comparison_amz["months_of_stock"] = (
                            comparison_amz["total_quantity"] / (comparison_amz["forecast_3mo"] / 3)
                        ).round(1)
                        comparison_amz["months_of_stock"] = comparison_amz["months_of_stock"].replace(
                            [float("inf"), float("-inf")], 0
                        ).fillna(0)
                        comparison_amz.columns = ["SKU", "Forecast (3mo)", "FBA Total", "Months of Stock"]
                        comparison_amz.insert(1, "Flavor", comparison_amz["SKU"].map(lambda s: get_flavor(s)))
                        comparison_amz = sort_df_by_best_seller(comparison_amz, sku_col="SKU")

                        def _color_amz_stock(val):
                            if val < 1:
                                return "background-color: #fee2e2"
                            elif val < 2:
                                return "background-color: #fef3c7"
                            return ""

                        for _cac in ["Forecast (3mo)", "FBA Fulfillable"]:
                            if _cac in comparison_amz.columns:
                                comparison_amz[_cac] = comparison_amz[_cac].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
                        if "Months of Stock" in comparison_amz.columns:
                            comparison_amz["Months of Stock"] = comparison_amz["Months of Stock"].apply(lambda x: f"{x:.1f}" if pd.notnull(x) else "")
                        _render_df_as_html_global(comparison_amz, max_height=min(len(comparison_amz) * 35 + 38, 700))
                    else:
                        st.info("No SKU forecast data available.")
                else:
                    st.info("No waterfall forecast available.")
            except Exception as e:
                st.warning(f"Could not load forecast comparison: {e}")


# ================================================================
# PAGE: REORDER ALERTS
# ================================================================
elif page == "Reorder Alerts":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Reorder Alerts")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        _render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
    st.caption("Forecast-driven reorder timing based on live 3PL + FBA inventory.")

    from analytics.reorder import build_reorder_plan, build_inventory_runway_chart, LEAD_TIME_WEEKS, MOQ_UNITS, SAFETY_STOCK_WEEKS

    # --- Settings ---
    with st.expander("Settings", expanded=False):
        col_s1, col_s2, col_s3 = st.columns(3)
        with col_s1:
            lt_weeks = st.number_input("Lead Time (weeks)", min_value=1, max_value=52, value=LEAD_TIME_WEEKS, key="ro_lt")
        with col_s2:
            moq = st.number_input("MOQ per SKU (units)", min_value=100, max_value=100000, value=MOQ_UNITS, step=500, key="ro_moq")
        with col_s3:
            safety_wk = st.number_input("Safety Buffer (weeks)", min_value=0, max_value=12, value=SAFETY_STOCK_WEEKS, key="ro_safety")

    # --- Get SKU forecast from waterfall ---
    has_forecast = False
    sku_table = pd.DataFrame()
    try:
        import json as _json_ro
        wf_source_ro = None
        with get_db() as conn:
            existing_spend_ro = get_media_spend(conn, source="All Sources")
        if existing_spend_ro:
            media_plan_ro = existing_spend_ro
        else:
            media_plan_ro = [{"month": (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m"),
                               "spend": 0, "new_customer_roas": 0.7} for i in range(12)]
        media_json_ro = _json_ro.dumps(media_plan_ro, sort_keys=True)
        wf_ro = _cached_waterfall(media_json_ro, wf_source_ro, 12, _load_seasonal_json())

        if not wf_ro.empty:
            sku_table = _cached_sku_forecast(wf_ro.to_json(), wf_source_ro)
            if not sku_table.empty:
                sku_table = sku_table[sku_table["SKU"].isin(FORECAST_SKUS)].copy()
                if "Variant" in sku_table.columns:
                    sku_table = sku_table.drop(columns=["Variant"])
                if "Flavor" not in sku_table.columns:
                    sku_table.insert(1, "Flavor", sku_table["SKU"].map(lambda s: get_flavor(s)))
                has_forecast = True
    except Exception as e:
        st.warning(f"Could not load demand forecast: {e}")

    # --- Get combined inventory (3PL + Amazon FBA) ---
    inv_data_3pl = []
    inv_data_amz = []
    has_3pl = False
    has_amz_inv = False

    with st.spinner("Fetching live inventory..."):
        # Packiyo 3PL
        if config.PACKIYO_API_TOKEN:
            try:
                from etl.packiyo_client import get_inventory as get_3pl_inventory
                inv_data_3pl = get_3pl_inventory()
                has_3pl = True
            except Exception as e:
                st.warning(f"Could not fetch 3PL inventory: {e}")

        # Amazon FBA
        has_amazon_creds_ro = all(getattr(config, k, "") for k in [
            "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
        ])
        if has_amazon_creds_ro:
            try:
                from etl.amazon_inventory import get_inventory as get_fba_inventory
                inv_data_amz = get_fba_inventory()
                has_amz_inv = True
            except Exception as e:
                st.warning(f"Could not fetch Amazon FBA inventory: {e}")

    # Merge: combine available stock across both sources per SKU
    combined_inv = {}
    for item in inv_data_3pl:
        sku = item["sku"]
        combined_inv[sku] = {
            "sku": sku,
            "name": item.get("name", ""),
            "quantity_available": item.get("quantity_available", 0) or 0,
            "quantity_on_hand": item.get("quantity_on_hand", 0) or 0,
            "quantity_inbound": item.get("quantity_inbound", 0) or 0,
            "3pl_available": item.get("quantity_available", 0) or 0,
            "fba_fulfillable": 0,
        }
    for item in inv_data_amz:
        sku = item["sku"]
        # Use total_quantity for planning (includes fulfillable + reserved + inbound)
        fba_total = item.get("total_quantity", 0) or 0
        fba_inbound = (item.get("inbound_shipped", 0) or 0) + (item.get("inbound_receiving", 0) or 0)
        if sku in combined_inv:
            combined_inv[sku]["quantity_available"] += fba_total
            combined_inv[sku]["quantity_inbound"] += fba_inbound
            combined_inv[sku]["fba_fulfillable"] = fba_total
        else:
            combined_inv[sku] = {
                "sku": sku,
                "name": item.get("product_name", ""),
                "quantity_available": fba_total,
                "quantity_on_hand": fba_total,
                "quantity_inbound": fba_inbound,
                "3pl_available": 0,
                "fba_fulfillable": fba_total,
            }

    inv_data = list(combined_inv.values())
    has_inventory = has_3pl or has_amz_inv

    if not has_inventory:
        st.info("No inventory sources configured. Connect Packiyo (3PL) or Amazon (FBA) in **Settings** for the full picture.")

    # --- Inventory Rollup ---
    if has_inventory and inv_data:
        st.subheader("Inventory Rollup")
        total_3pl = sum(i.get("3pl_available", 0) for i in inv_data)
        total_fba = sum(i.get("fba_fulfillable", 0) for i in inv_data)
        total_combined = sum(i.get("quantity_available", 0) for i in inv_data)
        total_inbound = sum(i.get("quantity_inbound", 0) for i in inv_data)

        rc1, rc2, rc3, rc4 = st.columns(4)
        rc1.metric("3PL (Packiyo)", f"{total_3pl:,}" if has_3pl else "N/A")
        rc2.metric("FBA (Amazon)", f"{total_fba:,}" if has_amz_inv else "N/A")
        rc3.metric("Total Available", f"{total_combined:,}")
        rc4.metric("Total Inbound", f"{total_inbound:,}")
        st.divider()

    # Load planned inbound (user's already-placed orders) for reorder calculations
    with get_db() as conn:
        ro_planned_inbound = get_planned_inbound_dict(conn)

    if not has_forecast:
        st.warning("No demand forecast available. Go to **Demand Forecast** to set up your media spend plan first.")
    else:
        with st.spinner("Building reorder plan..."):
            reorder_df, reorder_events = build_reorder_plan(
                sku_forecast_table=sku_table,
                inventory_data=inv_data,
                lead_time_weeks=lt_weeks,
                moq=moq,
                safety_weeks=safety_wk,
                forecast_skus=FORECAST_SKUS,
                planned_inbound=ro_planned_inbound,
            )

        if reorder_df.empty:
            st.warning("No reorder data available for the core forecast SKUs.")
        else:
            # --- KPI cards ---
            overdue = reorder_df[reorder_df["Urgency"] == "OVERDUE"]
            order_now = reorder_df[reorder_df["Urgency"] == "ORDER NOW"]
            order_soon = reorder_df[reorder_df["Urgency"] == "ORDER SOON"]
            en_route = reorder_df[reorder_df["Urgency"].str.startswith("EN ROUTE")]
            en_route_warn = reorder_df[reorder_df["Urgency"] == "EN ROUTE ⚠️"]

            _n_ok = len(reorder_df) - len(overdue) - len(order_now) - len(order_soon) - len(en_route)
            _u1, _u2, _u3, _u4, _u5 = st.columns(5)
            _u1.metric("Overdue", len(overdue))
            _u2.metric("Order Now", len(order_now))
            _u3.metric("Order Soon", len(order_soon))
            _u4.metric("En Route", len(en_route))
            _u5.metric("OK", _n_ok)

            st.markdown("")

            # --- Urgency alerts ---
            urgent = reorder_df[reorder_df["Urgency"].isin(["OVERDUE", "EN ROUTE ⚠️", "ORDER NOW", "ORDER SOON"])]
            if not urgent.empty:
                st.subheader("Action Required")
                for _urg_idx, (_, row) in enumerate(urgent.iterrows()):
                    if row["Urgency"] == "OVERDUE":
                        icon = "🔴"
                        color = "error"
                    elif row["Urgency"] == "EN ROUTE ⚠️":
                        icon = "🚚⚠️"
                        color = "warning"
                    elif row["Urgency"] == "ORDER NOW":
                        icon = "🟠"
                        color = "warning"
                    else:
                        icon = "🟡"
                        color = "info"

                    label_parts = [f"{icon} **{row['SKU']}**"]
                    if row["Flavor"]:
                        label_parts.append(f"({row['Flavor']})")
                    label_parts.append(f"— {row['Urgency']}")
                    if row["Urgency"] == "EN ROUTE ⚠️":
                        label_parts.append("(ordered but may stock out before arrival)")
                    elif row["Reorder By"] != "—":
                        label_parts.append(f"| Reorder by **{row['Reorder By']}**")
                    label_parts.append(f"| Order **{row['Order Qty']:,}** units")

                    with st.expander(" ".join(label_parts)):
                        ec1, ec2, ec3, ec4, ec5 = st.columns(5)
                        ec1.metric("3PL Stock", f"{row.get('3PL Stock', 0):,}")
                        ec2.metric("FBA Stock", f"{row.get('FBA Stock', 0):,}")
                        ec3.metric("Total Stock", f"{row['Total Stock']:,}")
                        ec4.metric("Monthly Demand", f"{row['Monthly Demand']:,}")
                        ec5.metric("Order Qty", f"{row['Order Qty']:,}")

                        ec6, ec7, ec8, ec9, ec10 = st.columns(5)
                        ec6.metric("Inbound", f"{row['Inbound']:,}")
                        ec7.metric("Stockout Date", row["Stockout Date"])
                        ec8.metric("Lead Time", f"{lt_weeks} weeks")
                        ec9.metric("MOQ", f"{moq:,}")
                        ec10.metric("Months of Cover", f"{row['Months of Cover']:.1f}")

                        # Show planned inbound if any
                        _sku_planned = ro_planned_inbound.get(row["SKU"], {})
                        if _sku_planned:
                            _planned_str = ", ".join(
                                f"{datetime.strptime(m, '%Y-%m').strftime('%b %Y')}: {u:,}"
                                for m, u in sorted(_sku_planned.items()) if u > 0
                            )
                            st.success(f"✅ **Planned inbound:** {_planned_str}")

                        # Mark as Ordered button
                        _mark_key = f"mark_{row['SKU']}_{_urg_idx}"
                        with st.popover(f"✅ Mark as Ordered", use_container_width=False):
                            _mk_c1, _mk_c2 = st.columns(2)
                            with _mk_c1:
                                _order_qty = st.number_input(
                                    "Order Qty", min_value=100, value=row["Order Qty"],
                                    step=500, key=f"oq_{_mark_key}",
                                )
                            with _mk_c2:
                                _arrival_options = [
                                    (datetime.utcnow() + relativedelta(months=i)).strftime("%Y-%m")
                                    for i in range(1, 7)
                                ]
                                _arrival_month = st.selectbox(
                                    "Expected Arrival Month", _arrival_options,
                                    key=f"am_{_mark_key}",
                                )
                            if st.button("Save", key=f"save_{_mark_key}", type="primary"):
                                with get_db() as conn:
                                    upsert_planned_inbound(conn, row["SKU"], _arrival_month, _order_qty)
                                st.success(f"Saved: {_order_qty:,} units arriving {_arrival_month}")
                                st.rerun()

                        # Inventory runway chart
                        if has_inventory:
                            inv_item = next((it for it in inv_data if it["sku"] == row["SKU"]), None)
                            if inv_item:
                                meta_cols_r = {"SKU", "Flavor", "Variant"}
                                month_cols_r = [c for c in sku_table.columns if c not in meta_cols_r]
                                fc_row = sku_table[sku_table["SKU"] == row["SKU"]]
                                if not fc_row.empty:
                                    monthly = {m: float(fc_row.iloc[0][m]) for m in month_cols_r if pd.notna(fc_row.iloc[0][m]) and fc_row.iloc[0][m] > 0}
                                    runway_df, ro_info = build_inventory_runway_chart(
                                        row["SKU"], monthly,
                                        inv_item.get("quantity_available", 0),
                                        inv_item.get("quantity_inbound", 0),
                                        lead_time_weeks=lt_weeks, moq=moq, safety_weeks=safety_wk,
                                        planned_inbound_sku=ro_planned_inbound.get(row["SKU"], {}),
                                    )
                                    if not runway_df.empty:
                                        # Convert dates to pd.Timestamp for Plotly compatibility
                                        runway_df["date"] = pd.to_datetime(runway_df["date"])
                                        fig_run = go.Figure()
                                        fig_run.add_trace(go.Scatter(
                                            x=runway_df["date"], y=runway_df["inventory_no_order"],
                                            mode="lines", name="Without Order",
                                            line=dict(color="#E05252", width=2, dash="dash"),
                                        ))
                                        fig_run.add_trace(go.Scatter(
                                            x=runway_df["date"], y=runway_df["inventory_with_order"],
                                            mode="lines", name=f"With Order ({ro_info['order_qty']:,} units)",
                                            line=dict(color="#2DA87E", width=2),
                                        ))
                                        if ro_info.get("arrival_date"):
                                            arrival_str = ro_info["arrival_date"].strftime("%Y-%m-%d")
                                            # Use add_shape + add_annotation instead of
                                            # add_vline(annotation_text=...) to avoid Plotly
                                            # bug where sum() is called on Timestamp x-axis.
                                            fig_run.add_shape(
                                                type="line", x0=arrival_str, x1=arrival_str,
                                                y0=0, y1=1, yref="paper",
                                                line=dict(dash="dot", color="#7ECCE5", width=1),
                                            )
                                            fig_run.add_annotation(
                                                x=arrival_str, y=1, yref="paper",
                                                text=f"Order arrives ({ro_info['arrival_date'].strftime('%b %d')})",
                                                showarrow=False, yanchor="bottom",
                                                font=dict(size=11, color="#7ECCE5"),
                                            )
                                        fig_run.add_hline(y=0, line_dash="solid", line_color="#E05252", line_width=1)
                                        fig_run.update_layout(
                                            height=280,
                                            margin=dict(l=0, r=0, t=30, b=0),
                                            yaxis_title="Units",
                                            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                                        )
                                        st.plotly_chart(fig_run, use_container_width=True)

                st.divider()

            # --- En Route (ordered, on track) ---
            en_route_ok = reorder_df[reorder_df["Urgency"] == "EN ROUTE"]
            if not en_route_ok.empty:
                st.subheader("En Route")
                st.caption("These SKUs have inventory already ordered and arriving soon.")
                for _er_idx, (_, row) in enumerate(en_route_ok.iterrows()):
                    _sku_planned = ro_planned_inbound.get(row["SKU"], {})
                    _planned_str = ", ".join(
                        f"{datetime.strptime(m, '%Y-%m').strftime('%b %Y')}: {u:,}"
                        for m, u in sorted(_sku_planned.items()) if u > 0
                    )
                    flavor_txt = f" ({row['Flavor']})" if row["Flavor"] else ""
                    st.success(
                        f"🚚 **{row['SKU']}**{flavor_txt} — "
                        f"**{row['Planned Inbound']:,}** units arriving ({_planned_str}) | "
                        f"Current stock: {row['Total Stock']:,} | Monthly demand: {row['Monthly Demand']:,}"
                    )
                st.divider()

            # --- Reorder Timeline ---
            if reorder_events:
                st.subheader("Reorder Timeline")
                st.caption(f"When to place orders based on {lt_weeks}-week lead time. Green = order date, blue = arrival, red = stockout if no order.")

                # Gantt-style timeline
                timeline_rows = []
                for ev in sorted(reorder_events, key=lambda x: x["reorder_by"]):
                    label = f"{ev['flavor'] or ev['sku']}"
                    timeline_rows.append({
                        "SKU": label,
                        "Start": ev["reorder_by"],
                        "End": ev["arrival_date"],
                        "Type": "Lead Time (order → arrival)",
                    })
                    timeline_rows.append({
                        "SKU": label,
                        "Start": ev["arrival_date"],
                        "End": ev["stockout_date"],
                        "Type": "Runway (arrival → stockout)",
                    })

                tl_df = pd.DataFrame(timeline_rows)
                # Convert date objects to timestamps for Plotly
                tl_df["Start"] = pd.to_datetime(tl_df["Start"])
                tl_df["End"] = pd.to_datetime(tl_df["End"])
                fig_tl = px.timeline(
                    tl_df, x_start="Start", x_end="End", y="SKU", color="Type",
                    color_discrete_map={
                        "Lead Time (order → arrival)": "#0F3557",
                        "Runway (arrival → stockout)": "#F58B3D",
                    },
                )
                fig_tl.update_layout(
                    height=max(250, len(reorder_events) * 50),
                    margin=dict(l=0, r=0, t=10, b=0),
                    xaxis_title="",
                    yaxis_title="",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                )
                # Add today marker (use add_shape + add_annotation to avoid
                # Plotly bug with sum() on Timestamp x-axis data)
                _today_str = datetime.utcnow().strftime("%Y-%m-%d")
                fig_tl.add_shape(
                    type="line", x0=_today_str, x1=_today_str,
                    y0=0, y1=1, yref="paper",
                    line=dict(dash="dash", color="#E05252", width=1),
                )
                fig_tl.add_annotation(
                    x=_today_str, y=1, yref="paper",
                    text="Today", showarrow=False, yanchor="bottom",
                    font=dict(size=11, color="#E05252"),
                )
                st.plotly_chart(fig_tl, use_container_width=True)

                st.divider()

            # --- Full reorder table ---
            st.subheader("Full Reorder Plan")
            st.caption(f"Lead time: {lt_weeks} weeks | MOQ: {moq:,} units | Safety buffer: {safety_wk} weeks")

            def _color_urgency(val):
                colors = {
                    "OVERDUE": "background-color: #fee2e2; color: #991b1b; font-weight: bold",
                    "ORDER NOW": "background-color: #ffedd5; color: #9a3412; font-weight: bold",
                    "ORDER SOON": "background-color: #fef3c7; color: #92400e",
                    "UPCOMING": "background-color: #ecfdf5; color: #065f46",
                    "OK": "",
                }
                return colors.get(val, "")

            display_reorder = reorder_df.copy()
            # Drop Days Until Reorder column (redundant with Reorder By date)
            if "Days Until Reorder" in display_reorder.columns:
                display_reorder = display_reorder.drop(columns=["Days Until Reorder"])

            for _roc in ["3PL Stock", "FBA Stock", "Total Stock", "Inbound", "Monthly Demand", "Order Qty"]:
                if _roc in display_reorder.columns:
                    display_reorder[_roc] = display_reorder[_roc].apply(lambda x: f"{x:,.0f}" if pd.notnull(x) else "")
            if "Months of Cover" in display_reorder.columns:
                display_reorder["Months of Cover"] = display_reorder["Months of Cover"].apply(lambda x: f"{x:.1f}" if pd.notnull(x) else "")
            _render_df_as_html_global(display_reorder, max_height=min(len(display_reorder) * 35 + 38, 700))

            # --- Export ---
            st.divider()
            csv_ro = reorder_df.to_csv(index=False)
            st.download_button(
                "📥 Export Reorder Plan (CSV)",
                data=csv_ro,
                file_name=f"reorder_plan_{datetime.utcnow().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )



# ================================================================
# PAGE: FBA TRANSFERS
# ================================================================
elif page == "FBA Transfers":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("FBA Transfers")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['amazon'])
            _new = get_new_rows_since_yesterday(conn, ['amazon'])
            _srcs = get_synced_sources(conn, ['amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        _render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
    st.caption("When to ship inventory from your 3PL (Packiyo) to Amazon FBA.")

    # --- Settings ---
    with st.expander("Settings", expanded=False):
        transfer_lt_weeks = st.number_input(
            "DTC→FBA Transfer Lead Time (weeks)", min_value=1, max_value=12, value=4,
            key="fba_transfer_lt",
            help="How many weeks it takes for inventory to ship from your 3PL to Amazon FBA.",
        )

    # --- Fetch inventory from both sources ---
    inv_data_3pl_fba = []
    inv_data_amz_fba = []
    has_amz_inv_fba = False

    with st.spinner("Fetching live inventory..."):
        if config.PACKIYO_API_TOKEN:
            try:
                from etl.packiyo_client import get_inventory as get_3pl_inventory
                inv_data_3pl_fba = get_3pl_inventory()
            except Exception as e:
                st.warning(f"Could not fetch 3PL inventory: {e}")

        has_amazon_creds_fba = all(getattr(config, k, "") for k in [
            "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
        ])
        if has_amazon_creds_fba:
            try:
                from etl.amazon_inventory import get_inventory as get_fba_inventory
                inv_data_amz_fba = get_fba_inventory()
                has_amz_inv_fba = True
            except Exception as e:
                st.warning(f"Could not fetch Amazon FBA inventory: {e}")

    if not has_amz_inv_fba:
        st.info("No Amazon FBA credentials configured. Connect Amazon in **Settings** to see transfer alerts.")
    elif not inv_data_amz_fba:
        st.info("No Amazon FBA inventory data found.")
    else:
        # Compute Amazon-specific demand from daily_sku_sales (last 90 days average)
        with get_db() as conn:
            _amz_demand = pd.read_sql_query("""
                SELECT sku, SUM(units_sold) / 3.0 as monthly_demand
                FROM daily_sku_sales
                WHERE source = 'amazon'
                  AND sale_date >= date('now', '-90 days')
                GROUP BY sku
            """, conn)
        _amz_demand_map = dict(zip(_amz_demand["sku"], _amz_demand["monthly_demand"])) if not _amz_demand.empty else {}

        # Build transfer alerts for each FBA SKU
        transfer_lt_days = transfer_lt_weeks * 7
        today_t = datetime.utcnow().date()
        transfer_rows = []

        for item in inv_data_amz_fba:
            sku = item["sku"]
            if sku not in FORECAST_SKUS:
                continue
            fba_stock = item.get("total_quantity", 0) or 0
            monthly = _amz_demand_map.get(sku, 0)
            daily_rate = monthly / 30.44 if monthly > 0 else 0

            if daily_rate <= 0:
                continue

            days_of_stock = fba_stock / daily_rate if daily_rate > 0 else 999
            fba_stockout = today_t + timedelta(days=int(days_of_stock)) if days_of_stock < 365 else None
            transfer_by = fba_stockout - timedelta(days=transfer_lt_days) if fba_stockout else None
            days_until_transfer = (transfer_by - today_t).days if transfer_by else 999

            # 3PL stock available for transfer
            _3pl_item = next((i for i in inv_data_3pl_fba if i["sku"] == sku), None)
            _3pl_avail = _3pl_item.get("quantity_available", 0) if _3pl_item else 0

            # Transfer quantity: 2 months of Amazon demand, rounded to 100
            transfer_qty = max(100, round(monthly * 2 / 100) * 100) if monthly > 0 else 0

            if days_until_transfer < 0:
                t_urgency = "OVERDUE"
            elif days_until_transfer <= 7:
                t_urgency = "TRANSFER NOW"
            elif days_until_transfer <= 21:
                t_urgency = "TRANSFER SOON"
            elif days_until_transfer <= 42:
                t_urgency = "UPCOMING"
            else:
                t_urgency = "OK"

            transfer_rows.append({
                "SKU": sku,
                "Flavor": get_flavor(sku),
                "FBA Stock": fba_stock,
                "FBA Monthly Demand": round(monthly),
                "FBA Stockout": fba_stockout.strftime("%Y-%m-%d") if fba_stockout else "Beyond 12mo",
                "Transfer By": transfer_by.strftime("%Y-%m-%d") if transfer_by else "—",
                "Days Until Transfer": days_until_transfer if days_until_transfer < 999 else None,
                "Transfer Qty": transfer_qty,
                "3PL Available": _3pl_avail,
                "Can Fulfill": "✅" if _3pl_avail >= transfer_qty else "⚠️ Low",
                "Urgency": t_urgency,
            })

        if transfer_rows:
            transfer_df = pd.DataFrame(transfer_rows)
            transfer_df = sort_df_by_best_seller(transfer_df, sku_col="SKU")

            # KPI summary
            t_urgent = transfer_df[transfer_df["Urgency"].isin(["OVERDUE", "TRANSFER NOW"])]
            t_c1, t_c2, t_c3 = st.columns(3)
            t_c1.metric("Urgent Transfers", len(t_urgent))
            t_c2.metric("Total FBA SKUs", len(transfer_df))
            t_c3.metric("Transfer Lead Time", f"{transfer_lt_weeks} weeks")

            # Color-code urgency
            def _color_transfer_urgency(val):
                colors = {
                    "OVERDUE": "background-color: #FEE2E2; color: #991B1B;",
                    "TRANSFER NOW": "background-color: #FFEDD5; color: #9A3412;",
                    "TRANSFER SOON": "background-color: #FEF9C3; color: #854D0E;",
                    "UPCOMING": "background-color: #E0E7FF; color: #3730A3;",
                    "OK": "background-color: #D1FAE5; color: #065F46;",
                }
                return colors.get(val, "")

            _render_df_as_html_global(transfer_df, max_height=min(len(transfer_df) * 35 + 38, 500))
        else:
            st.info("No Amazon FBA SKUs with measurable demand found.")


# ================================================================
# PAGE: MARKETING (Klaviyo + Google Sheet analytics)
# ================================================================
elif page == "Marketing":
    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title("Marketing")
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        _render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)

    with get_db() as conn:
        _mkt_klaviyo_key = get_setting(conn, "klaviyo_api_key", "")

    with get_db() as conn:
        try:
            _mkt_cols = [d[1] for d in conn.execute("PRAGMA table_info(google_sheet_data)").fetchall()]
            _has_gs_data = "date" in _mkt_cols
        except Exception:
            _has_gs_data = False

    if _has_gs_data:
        with get_db() as conn:
            mkt_df = pd.read_sql_query("SELECT * FROM google_sheet_data ORDER BY id", conn)

        if not mkt_df.empty:
            mkt_df["_date"] = pd.to_datetime(mkt_df["date"], format="mixed", dayfirst=False)
            mkt_df = mkt_df.sort_values("_date")

            def _clean_num(val):
                if pd.isna(val):
                    return 0.0
                s = str(val).replace("$", "").replace(",", "").replace("%", "").strip()
                try:
                    return float(s)
                except (ValueError, TypeError):
                    return 0.0

            # Date range with smart presets — default to MTD
            mkt_start, mkt_end = _smart_date_filter(
                mkt_df["_date"].min().date(), mkt_df["_date"].max().date(), "mkt",
                default_preset="MTD",
            )
            mkt_df = mkt_df[(mkt_df["_date"].dt.date >= mkt_start) & (mkt_df["_date"].dt.date <= mkt_end)]

            # Compute all metrics
            # NOTE: Triple Whale "blended" metrics use dual-attribution that
            # reports ~2x the actual platform spend & Shopify revenue.
            # We halve the affected columns to align with actual Shopify/ad-platform data.
            _TW_ADJ = 0.5  # Triple Whale adjustment factor
            mkt_df["_ad_spend"] = mkt_df["blended_ad_spend"].apply(_clean_num) * _TW_ADJ
            mkt_df["_revenue"] = mkt_df["total_sales"].apply(_clean_num) * _TW_ADJ
            mkt_df["_orders"] = mkt_df["orders"].apply(_clean_num) * _TW_ADJ
            mkt_df["_nc_orders"] = mkt_df["new_customer_orders"].apply(_clean_num) * _TW_ADJ
            mkt_df["_nc_revenue"] = mkt_df["new_customer_revenue"].apply(_clean_num) * _TW_ADJ
            mkt_df["_ret_revenue"] = mkt_df["returning_customer_revenue"].apply(_clean_num) * _TW_ADJ
            mkt_df["_ret_orders"] = mkt_df["_orders"] - mkt_df["_nc_orders"]
            mkt_df["_fb_spend"] = mkt_df.get("facebook_ads_spend", pd.Series(0)).apply(_clean_num) * _TW_ADJ
            mkt_df["_goog_spend"] = mkt_df.get("google_ads_spend", pd.Series(0)).apply(_clean_num) * _TW_ADJ
            mkt_df["_sessions"] = mkt_df.get("sessions", pd.Series(0)).apply(_clean_num)  # Sessions not doubled
            mkt_df["_atc"] = mkt_df.get("sessions_with_add_to_carts", pd.Series(0)).apply(_clean_num)
            mkt_df["_units"] = mkt_df.get("units_sold", pd.Series(0)).apply(_clean_num) * _TW_ADJ
            mkt_df["_nc_aov"] = mkt_df.get("nc_aov", pd.Series(0)).apply(_clean_num)  # AOV is a ratio, not doubled
            mkt_df["_nc_cpa"] = mkt_df.get("new_customers_cpa", pd.Series(0)).apply(_clean_num)  # CPA is a ratio
            mkt_df["_nc_roas"] = mkt_df.get("new_customer_roas", pd.Series(0)).apply(_clean_num)  # ROAS is a ratio

            total_spend = mkt_df["_ad_spend"].sum()
            total_rev = mkt_df["_revenue"].sum()
            total_orders = int(mkt_df["_orders"].sum())
            total_nc = int(mkt_df["_nc_orders"].sum())
            total_ret = int(mkt_df["_ret_orders"].sum())
            nc_rev = mkt_df["_nc_revenue"].sum()
            ret_rev = mkt_df["_ret_revenue"].sum()

            # ============================================================
            # LOAD REVENUE GOALS from Demand Forecast engine
            # ============================================================
            _mkt_summary = None
            _mkt_revenue = {}
            try:
                _seasonal_json_mkt = _load_seasonal_json()
                with get_db() as _goals_conn:
                    _mkt_media = get_media_spend(_goals_conn, source="All Sources")
                    _amz_rev_f = get_amazon_revenue_forecast(_goals_conn)
                import json as _json_mkt
                _mkt_wf = _cached_waterfall(_json_mkt.dumps(_mkt_media, sort_keys=True), None, 12, _seasonal_json_mkt)
                if _mkt_wf is not None and not _mkt_wf.empty:
                    _mkt_sku_table = _cached_sku_forecast(_mkt_wf.to_json(), None)
                    _amz_rev_dict = {r["month"]: r["revenue"] for r in _amz_rev_f if r.get("revenue", 0) > 0}
                    _dtc_mkt = build_master_dtc_forecast(
                        shopify_waterfall_df=_mkt_wf,
                        shopify_sku_forecast_df=_mkt_sku_table,
                        horizon_months=12,
                        forecast_skus=FORECAST_SKUS,
                        media_plan=_mkt_media,
                        amazon_revenue_forecast=_amz_rev_dict if _amz_rev_dict else None,
                    )
                    _mkt_summary = _dtc_mkt["summary"]
                    _mkt_revenue = _dtc_mkt.get("revenue", {})
            except Exception as _mkt_err:
                st.caption(f"⚠️ Could not load revenue goals: {_mkt_err}")

            # Pre-compute current-month pacing vars
            _now = pd.Timestamp.now()  # tz-naive to match mkt_df["_date"]
            _cur_month = _now.strftime("%Y-%m")
            _cur_month_name = _now.strftime("%B %Y")
            _days_in_month = _now.days_in_month
            _day_of_month = _now.day
            _pct_month = _day_of_month / _days_in_month

            # MTD actuals from Google Sheet
            _cm_mask = mkt_df["_date"].dt.strftime("%Y-%m") == _cur_month
            _cm_nc_rev = mkt_df.loc[_cm_mask, "_nc_revenue"].sum()
            _cm_ret_rev = mkt_df.loc[_cm_mask, "_ret_revenue"].sum()
            _cm_spend = mkt_df.loc[_cm_mask, "_ad_spend"].sum()
            _cm_rev = mkt_df.loc[_cm_mask, "_revenue"].sum()
            _cm_orders = int(mkt_df.loc[_cm_mask, "_orders"].sum())
            _cm_nc = int(mkt_df.loc[_cm_mask, "_nc_orders"].sum())
            _cm_ret_orders = int(mkt_df.loc[_cm_mask, "_ret_orders"].sum()) if _cm_mask.any() else 0

            # Amazon MTD from amazon_daily_rollup (New Customers, NC Rev, Spend)
            # Revenue still from daily_sku_sales (more complete)
            _cm_amz_rev = 0
            _cm_amz_spend = 0
            _cm_amz_nc = 0
            _cm_amz_nc_rev = 0
            try:
                with get_db() as _amz_conn:
                    # Revenue from daily_sku_sales
                    _amz_mtd = _amz_conn.execute(
                        "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date >= ? AND sale_date <= ?",
                        (f"{_cur_month}-01", _now.strftime("%Y-%m-%d")),
                    ).fetchone()
                    _cm_amz_rev = float(_amz_mtd[0] or 0)

                    # Spend, NC, NC Rev from amazon_daily_rollup
                    _amz_rollup = _amz_conn.execute(
                        "SELECT SUM(spend), SUM(new_customers), SUM(new_customer_rev) "
                        "FROM amazon_daily_rollup WHERE date >= ? AND date <= ?",
                        (f"{_cur_month}-01", _now.strftime("%Y-%m-%d")),
                    ).fetchone()
                    if _amz_rollup and _amz_rollup[0] is not None:
                        _cm_amz_spend = float(_amz_rollup[0] or 0)
                        _cm_amz_nc = int(float(_amz_rollup[1] or 0))
                        _cm_amz_nc_rev = float(_amz_rollup[2] or 0)
            except Exception:
                pass

            # Revenue GOALS from the forecast engine
            _goal_nc_rev = 0
            _goal_repeat_rev = 0
            _goal_amz_rev = 0
            _has_goals = False
            if _mkt_summary is not None and not _mkt_summary.empty:
                _plan_row = _mkt_summary[_mkt_summary["month"] == _cur_month]
                if not _plan_row.empty:
                    _goal_nc_rev = float(_plan_row.iloc[0].get("shopify_new_rev", 0))
                    _goal_repeat_rev = float(_plan_row.iloc[0].get("shopify_repeat_rev", 0))
                    _goal_amz_rev = float(_plan_row.iloc[0].get("amazon_rev", 0))
                    _has_goals = (_goal_nc_rev + _goal_repeat_rev + _goal_amz_rev) > 0

            _goal_total_rev = _goal_nc_rev + _goal_repeat_rev + _goal_amz_rev

            # Last month actuals (for fallback comparison)
            _last_month = (_now - pd.DateOffset(months=1)).strftime("%Y-%m")
            _lm_mask = mkt_df["_date"].dt.strftime("%Y-%m") == _last_month
            _lm_nc_rev = mkt_df.loc[_lm_mask, "_nc_revenue"].sum()
            _lm_ret_rev = mkt_df.loc[_lm_mask, "_ret_revenue"].sum()
            _lm_spend = mkt_df.loc[_lm_mask, "_ad_spend"].sum()

            # ============================================================
            # PACING DASHBOARD — matches Revenue Model "Automated Pacing" tab
            # ============================================================
            st.header(f"{_cur_month_name} Pacing")
            st.caption(f"Day {_day_of_month} of {_days_in_month}")
            st.progress(_pct_month, text=f"{_pct_month*100:.0f}%")

            # Last 7 days averages from Google Sheet
            _l7d_mask = mkt_df["_date"] >= (_now - pd.Timedelta(days=7))
            _l7d_rev = mkt_df.loc[_l7d_mask, "_revenue"].sum() / 7 if _l7d_mask.any() else 0
            _l7d_nc_rev = mkt_df.loc[_l7d_mask, "_nc_revenue"].sum() / 7 if _l7d_mask.any() else 0
            _l7d_ret_rev = mkt_df.loc[_l7d_mask, "_ret_revenue"].sum() / 7 if _l7d_mask.any() else 0
            _l7d_spend = mkt_df.loc[_l7d_mask, "_ad_spend"].sum() / 7 if _l7d_mask.any() else 0
            _l7d_nc = mkt_df.loc[_l7d_mask, "_nc_orders"].sum() / 7 if _l7d_mask.any() else 0

            # Amazon L7D from daily_sku_sales (revenue) + amazon_daily_rollup (spend/NC)
            _l7d_amz_rev = 0
            _l7d_amz_spend = 0
            _l7d_amz_nc = 0
            _l7d_amz_nc_rev = 0
            try:
                _l7d_start = (_now - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
                with get_db() as _l7_conn:
                    _l7_amz = _l7_conn.execute(
                        "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date >= ?",
                        (_l7d_start,),
                    ).fetchone()
                    _l7d_amz_rev = float(_l7_amz[0] or 0) / 7
                    # Rollup L7D
                    _l7_rollup = _l7_conn.execute(
                        "SELECT SUM(spend), SUM(new_customers), SUM(new_customer_rev) "
                        "FROM amazon_daily_rollup WHERE date >= ?",
                        (_l7d_start,),
                    ).fetchone()
                    if _l7_rollup and _l7_rollup[0] is not None:
                        _l7d_amz_spend = float(_l7_rollup[0] or 0) / 7
                        _l7d_amz_nc = float(_l7_rollup[1] or 0) / 7
                        _l7d_amz_nc_rev = float(_l7_rollup[2] or 0) / 7
            except Exception:
                pass

            # Yesterday actuals from Google Sheet
            _yesterday = (_now - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            _yd_mask = mkt_df["_date"].dt.strftime("%Y-%m-%d") == _yesterday
            _yd_rev = mkt_df.loc[_yd_mask, "_revenue"].sum()
            _yd_nc_rev = mkt_df.loc[_yd_mask, "_nc_revenue"].sum()
            _yd_ret_rev = mkt_df.loc[_yd_mask, "_ret_revenue"].sum()
            _yd_spend = mkt_df.loc[_yd_mask, "_ad_spend"].sum()
            _yd_nc = int(mkt_df.loc[_yd_mask, "_nc_orders"].sum())

            # Amazon yesterday from daily_sku_sales + amazon_daily_rollup
            _yd_amz_rev = 0
            _yd_amz_spend = 0
            _yd_amz_nc = 0
            _yd_amz_nc_rev = 0
            try:
                with get_db() as _yd_conn:
                    _yd_amz = _yd_conn.execute(
                        "SELECT SUM(revenue) FROM daily_sku_sales WHERE source = 'amazon' AND sale_date = ?",
                        (_yesterday,),
                    ).fetchone()
                    _yd_amz_rev = float(_yd_amz[0] or 0)
                    _yd_rollup = _yd_conn.execute(
                        "SELECT spend, new_customers, new_customer_rev "
                        "FROM amazon_daily_rollup WHERE date = ?",
                        (_yesterday,),
                    ).fetchone()
                    if _yd_rollup:
                        _yd_amz_spend = float(_yd_rollup[0] or 0)
                        _yd_amz_nc = int(float(_yd_rollup[1] or 0))
                        _yd_amz_nc_rev = float(_yd_rollup[2] or 0)
            except Exception:
                pass

            _remaining_days = _days_in_month - _day_of_month
            _total_actual_rev = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev

            if _has_goals:
                # Build pacing row helper
                # --- Color helpers ---
                # For revenue: above pace = green, below = red
                # For spend: above pace = red (overspending), below = green
                def _pace_color(pct, invert=False):
                    """Return CSS for pacing cells with colored bg tint + bold text. invert=True for spend."""
                    if invert:
                        if pct <= 0.95:
                            return "color: #0a7a3e; font-weight: 700; background-color: rgba(16,185,80,0.12)"
                        elif pct <= 1.05:
                            return "color: #92700c; font-weight: 700; background-color: rgba(245,166,35,0.12)"
                        else:
                            return "color: #b91c1c; font-weight: 700; background-color: rgba(220,38,38,0.10)"
                    else:
                        if pct >= 1.05:
                            return "color: #0a7a3e; font-weight: 700; background-color: rgba(16,185,80,0.12)"
                        elif pct >= 0.95:
                            return "color: #92700c; font-weight: 700; background-color: rgba(245,166,35,0.12)"
                        else:
                            return "color: #b91c1c; font-weight: 700; background-color: rgba(220,38,38,0.10)"

                def _plus_minus_color(val, invert=False):
                    """Color for +/- column. Positive = green for rev, red for spend."""
                    if invert:
                        if val > 0:
                            return "color: #b91c1c; font-weight: 700; background-color: rgba(220,38,38,0.08)"
                        elif val < 0:
                            return "color: #0a7a3e; font-weight: 700; background-color: rgba(16,185,80,0.08)"
                        return ""
                    else:
                        if val > 0:
                            return "color: #0a7a3e; font-weight: 700; background-color: rgba(16,185,80,0.08)"
                        elif val < 0:
                            return "color: #b91c1c; font-weight: 700; background-color: rgba(220,38,38,0.08)"
                        return ""

                def _build_pace_row(label, mtd_actual, goal, l7d_avg, yd_actual, is_spend=False):
                    should_be = goal * _pct_month
                    pacing = (mtd_actual / should_be) if should_be > 0 else 0
                    plus_minus = mtd_actual - should_be
                    remaining = goal - mtd_actual
                    adjusted_daily = remaining / _remaining_days if _remaining_days > 0 else 0
                    eom_pacing = (mtd_actual / _pct_month) / goal if goal > 0 and _pct_month > 0 else 0
                    projection = mtd_actual / _pct_month if _pct_month > 0 else 0
                    yd_goal = goal / _days_in_month
                    yd_pacing = (yd_actual / yd_goal) if yd_goal > 0 else 0
                    return {
                        "_label": label,
                        "_is_spend": is_spend,
                        "_pacing_raw": pacing,
                        "_plus_minus_raw": plus_minus,
                        "_eom_pacing_raw": eom_pacing,
                        "_yd_pacing_raw": yd_pacing,
                        "": label,
                        "Pacing": f"{pacing:.0%}",
                        "MTD Actual": f"${mtd_actual:,.0f}",
                        "Should Be": f"${should_be:,.0f}",
                        "+/- Pacing": f"${plus_minus:+,.0f}",
                        "L7D Avg": f"${l7d_avg:,.0f}",
                        "Adj. Daily": f"${adjusted_daily:,.0f}",
                        "EOM Pacing": f"{eom_pacing:.0%}",
                        "Remaining": f"${remaining:,.0f}",
                        "Projection": f"${projection:,.0f}",
                        "EOM Goal": f"${goal:,.0f}",
                        "Yest. Actual": f"${yd_actual:,.0f}",
                        "Yest. Goal": f"${yd_goal:,.0f}",
                        "Yest. Pace": f"{yd_pacing:.0%}",
                    }

                def _style_pace_df(df_raw):
                    """Apply red/yellow/green styling to a pacing DataFrame."""
                    # Extract hidden columns for styling, then drop them
                    display_cols = [c for c in df_raw.columns if not c.startswith("_")]
                    style_data = df_raw.copy()
                    df_display = df_raw[display_cols].copy()

                    def _apply_styles(row_styler):
                        idx = row_styler.name
                        is_spend = style_data.loc[idx, "_is_spend"]
                        pacing_raw = style_data.loc[idx, "_pacing_raw"]
                        plus_raw = style_data.loc[idx, "_plus_minus_raw"]
                        eom_raw = style_data.loc[idx, "_eom_pacing_raw"]
                        yd_raw = style_data.loc[idx, "_yd_pacing_raw"]
                        styles = [""] * len(row_styler)
                        col_map = {c: i for i, c in enumerate(display_cols)}
                        # Bold label column
                        if "" in col_map:
                            styles[col_map[""]] = "font-weight: 600; color: #0F3557"
                        if "Pacing" in col_map:
                            styles[col_map["Pacing"]] = _pace_color(pacing_raw, invert=is_spend)
                        if "+/- Pacing" in col_map:
                            styles[col_map["+/- Pacing"]] = _plus_minus_color(plus_raw, invert=is_spend)
                        if "EOM Pacing" in col_map:
                            styles[col_map["EOM Pacing"]] = _pace_color(eom_raw, invert=is_spend)
                        if "Yest. Pace" in col_map:
                            styles[col_map["Yest. Pace"]] = _pace_color(yd_raw, invert=is_spend)
                        if "Projection" in col_map:
                            styles[col_map["Projection"]] = _pace_color(eom_raw, invert=is_spend)
                        return styles

                    styled = (
                        df_display.style
                        .set_properties(**{
                            "font-size": "0.84rem",
                            "font-family": "Visby CF, DM Sans, -apple-system, sans-serif",
                            "color": "#1e2d3d",
                            "background-color": "#ffffff",
                        })
                        .apply(_apply_styles, axis=1)
                        .set_table_styles([
                            {"selector": "th", "props": [
                                ("background-color", "#F0F4F8"),
                                ("color", "#0F3557"),
                                ("font-weight", "600"),
                                ("font-size", "0.74rem"),
                                ("text-transform", "uppercase"),
                                ("letter-spacing", "0.05em"),
                                ("border-bottom", "2px solid #D6DEE8"),
                                ("padding", "11px 14px"),
                            ]},
                            {"selector": "td", "props": [
                                ("border-bottom", "1px solid #F0F4F8"),
                                ("padding", "9px 14px"),
                            ]},
                        ])
                    )
                    return styled

                def _render_white_table(styled_df):
                    """Render a Pandas Styler as an HTML table with white bg, preserving cell-level colors."""
                    html = styled_df.hide(axis="index").to_html()
                    st.markdown(
                        '<div style="background:#ffffff;border-radius:12px;overflow:hidden;'
                        'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
                        'width:100%;overflow-x:auto;">'
                        '<style>'
                        '.pace-table table { width:100%; border-collapse:collapse; }'
                        '.pace-table th, .pace-table td { white-space:nowrap; }'
                        '.pace-table tr:hover td:not([style*="background-color"]) { background:#F7FAFC !important; }'
                        '.pace-table td[style*="color"] { -webkit-text-fill-color: unset; }'
                        '</style>'
                        f'<div class="pace-table">{html}</div></div>',
                        unsafe_allow_html=True,
                    )

                # Use global HTML table renderer
                _render_df_as_html = _render_df_as_html_global

                # ── DTC actuals (Google + Meta spend, Shopify revenue) ──
                _goal_dtc_rev = _goal_nc_rev + _goal_repeat_rev
                _cm_dtc_rev = _cm_nc_rev + _cm_ret_rev
                _l7d_dtc_rev = _l7d_nc_rev + _l7d_ret_rev
                _yd_dtc_rev = _yd_nc_rev + _yd_ret_rev
                _cm_dtc_spend = _cm_spend
                _l7d_dtc_spend = _l7d_spend
                _yd_dtc_spend = _yd_spend

                # ── Amazon data from amazon_daily_rollup (spend, NC, NC rev already loaded above) ──
                # _cm_amz_spend, _cm_amz_nc, _cm_amz_nc_rev set above from DB
                # _l7d_amz_spend, _l7d_amz_nc, _l7d_amz_nc_rev set above from DB
                # _yd_amz_spend, _yd_amz_nc, _yd_amz_nc_rev set above from DB

                # ── Combined Roll Up totals (DTC + Amazon) ──
                _cm_total_spend = _cm_dtc_spend + _cm_amz_spend
                _l7d_total_spend = _l7d_dtc_spend + _l7d_amz_spend
                _yd_total_spend = _yd_dtc_spend + _yd_amz_spend
                _cm_total_nc_rev = _cm_nc_rev + _cm_amz_nc_rev
                _cm_total_nc = _cm_nc + _cm_amz_nc
                _cm_total_repeat_rev = _cm_ret_rev  # Amazon repeat not tracked in this source

                # Efficiency metrics
                _business_mer = _total_actual_rev / _cm_total_spend if _cm_total_spend > 0 else 0
                _total_nc_roas = _cm_total_nc_rev / _cm_total_spend if _cm_total_spend > 0 else 0
                _blended_cpa = _cm_total_spend / _cm_total_nc if _cm_total_nc > 0 else 0

                # DTC efficiency
                _dtc_nc_roas = _cm_nc_rev / _cm_dtc_spend if _cm_dtc_spend > 0 else 0
                _dtc_nc_aov = _cm_nc_rev / _cm_nc if _cm_nc > 0 else 0
                _dtc_cpa = _cm_dtc_spend / _cm_nc if _cm_nc > 0 else 0
                _dtc_nc_conv = 0
                _cm_sessions = mkt_df.loc[_cm_mask, "_sessions"].sum() if _cm_mask.any() else 0
                if _cm_sessions > 0:
                    _dtc_nc_conv = _cm_nc / _cm_sessions * 100

                # ── Spend goal from media plan ──
                _goal_spend = 0
                _spend_plan = [m for m in _mkt_media if m.get("month") == _cur_month]
                if _spend_plan:
                    _goal_spend = float(_spend_plan[0].get("spend", 0))

                # ============================================================
                # CHANNEL FILTER — toggle between All / Roll Up / DTC / Amazon
                # ============================================================
                _chan_sel = st.segmented_control(
                    "Channel",
                    options=["All", "Roll Up", "DTC", "Amazon"],
                    default="All",
                    key="_mkt_channel",
                    label_visibility="collapsed",
                )
                if _chan_sel is None:
                    _chan_sel = "All"

                # ============================================================
                # ROLL UP — DTC + Amazon combined
                # ============================================================
                if _chan_sel in ("All", "Roll Up"):
                    st.subheader("Roll Up")

                    # Roll Up KPIs (above table)
                    eff1, eff2, eff3, eff4 = st.columns(4)
                    eff1.metric("Business MER", f"{_business_mer:.2f}x",
                                help="Total Revenue / Total Spend")
                    eff2.metric("Total NC ROAS", f"{_total_nc_roas:.2f}x",
                                help="Total NC Revenue / Total Spend")
                    eff3.metric("New Customers", f"{_cm_total_nc:,}",
                                help="DTC new customer orders")
                    eff4.metric("Blended CPA", f"${_blended_cpa:,.0f}",
                                help="Total Spend / Total New Customers")

                    _rollup_rows = []
                    if _goal_spend > 0:
                        _rollup_rows.append(_build_pace_row("Total Spend", _cm_total_spend, _goal_spend,
                                                            _l7d_total_spend, _yd_total_spend, is_spend=True))
                    _rollup_rows.extend([
                        _build_pace_row("Total Revenue", _total_actual_rev, _goal_total_rev,
                                        _l7d_rev + _l7d_amz_rev, _yd_rev + _yd_amz_rev),
                        _build_pace_row("Total NC Rev", _cm_total_nc_rev, _goal_nc_rev,
                                        _l7d_nc_rev + _l7d_amz_nc_rev, _yd_nc_rev + _yd_amz_nc_rev),
                        _build_pace_row("Repeat Revenue", _cm_total_repeat_rev, _goal_repeat_rev,
                                        _l7d_ret_rev, _yd_ret_rev),
                    ])
                    _rollup_df = pd.DataFrame(_rollup_rows)
                    _render_white_table(_style_pace_df(_rollup_df))

                # ============================================================
                # DTC (Shopify) — Google + Meta spend
                # ============================================================
                if _chan_sel in ("All", "DTC"):
                    if _chan_sel == "All":
                        st.markdown("---")
                    st.subheader("DTC (Shopify)")

                    # DTC KPIs (above table)
                    dtc_k1, dtc_k2, dtc_k3, dtc_k4 = st.columns(4)
                    dtc_k1.metric("NC ROAS", f"{_dtc_nc_roas:.2f}x")
                    dtc_k2.metric("NC Orders", f"{_cm_nc:,}")
                    dtc_k3.metric("NC AOV", f"${_dtc_nc_aov:,.0f}")
                    dtc_k4.metric("CPA", f"${_dtc_cpa:,.0f}")

                    _dtc_rows = []
                    if _goal_spend > 0:
                        _goal_dtc_spend = _goal_spend  # DTC spend goal from media plan
                        _dtc_rows.append(_build_pace_row("Spend", _cm_dtc_spend, _goal_dtc_spend,
                                                         _l7d_dtc_spend, _yd_dtc_spend, is_spend=True))
                    _dtc_rows.extend([
                        _build_pace_row("Revenue", _cm_dtc_rev, _goal_dtc_rev, _l7d_dtc_rev, _yd_dtc_rev),
                        _build_pace_row("New Customer Rev", _cm_nc_rev, _goal_nc_rev, _l7d_nc_rev, _yd_nc_rev),
                        _build_pace_row("Repeat Customer Rev", _cm_ret_rev, _goal_repeat_rev, _l7d_ret_rev, _yd_ret_rev),
                    ])
                    _dtc_df = pd.DataFrame(_dtc_rows)
                    _render_white_table(_style_pace_df(_dtc_df))

                # ============================================================
                # AMAZON — from amazon_daily_rollup
                # ============================================================
                if _chan_sel in ("All", "Amazon"):
                    if _chan_sel == "All":
                        st.markdown("---")
                    st.subheader("Amazon")

                    # Amazon KPIs (above table)
                    _amz_cpa = _cm_amz_spend / _cm_amz_nc if _cm_amz_nc > 0 else 0
                    amz_k1, amz_k2, amz_k3, amz_k4 = st.columns(4)
                    amz_k1.metric("Revenue MTD", f"${_cm_amz_rev:,.0f}")
                    amz_k2.metric("Spend MTD", f"${_cm_amz_spend:,.0f}")
                    amz_k3.metric("New Customers", f"{_cm_amz_nc:,}")
                    amz_k4.metric("CPA", f"${_amz_cpa:,.0f}")

                    _amz_rows = []
                    _amz_rows.append(
                        _build_pace_row("Spend", _cm_amz_spend, 0, _l7d_amz_spend, _yd_amz_spend, is_spend=True),
                    )
                    _amz_rows.append(
                        _build_pace_row("Revenue", _cm_amz_rev, _goal_amz_rev, _l7d_amz_rev, _yd_amz_rev),
                    )
                    _amz_rows.append(
                        _build_pace_row("NC Revenue", _cm_amz_nc_rev, 0, _l7d_amz_nc_rev, _yd_amz_nc_rev),
                    )
                    _amz_df = pd.DataFrame(_amz_rows)
                    _render_white_table(_style_pace_df(_amz_df))

            else:
                # No goals — show MTD summary with last month comparison
                st.info("Set up a media spend plan and Amazon forecast on the **Demand Forecast** page for pacing.")
                st.subheader("MTD Summary")
                ts1, ts2, ts3, ts4 = st.columns(4)
                ts1.metric("NC Revenue (MTD)", f"${_cm_nc_rev:,.0f}")
                ts2.metric("Repeat Revenue (MTD)", f"${_cm_ret_rev:,.0f}")
                ts3.metric("Amazon Revenue (MTD)", f"${_cm_amz_rev:,.0f}")
                _total_mtd = _cm_nc_rev + _cm_ret_rev + _cm_amz_rev
                ts4.metric("Total Revenue (MTD)", f"${_total_mtd:,.0f}")

            st.divider()

            # ============================================================
            # DoD / WoW / MoM PERFORMANCE TABS
            # ============================================================
            st.header("Performance")
            _perf_tab_dod, _perf_tab_wow, _perf_tab_mom = st.tabs(
                ["Daily", "Weekly", "Monthly"]
            )

            # --- Helper: build a performance summary table from aggregated data ---
            def _build_perf_table(agg_df, period_col, amz_agg=None):
                """Build DTC / Roll Up / Amazon summary tables from aggregated Google Sheet data.

                agg_df: DataFrame grouped by period with summed metrics.
                period_col: column name for the period label.
                amz_agg: optional DataFrame of Amazon revenue/spend grouped by the same period.
                Returns (dtc_df, rollup_df, amz_df) formatted for display.
                """
                rows_dtc = []
                rows_rollup = []
                rows_amz = []

                for _, r in agg_df.iterrows():
                    period_label = r[period_col]
                    spend = r.get("_ad_spend", 0)
                    total_rev = r.get("_revenue", 0)
                    nc_orders = r.get("_nc_orders", 0)
                    nc_rev = r.get("_nc_revenue", 0)
                    sessions = r.get("_sessions", 0)
                    orders = r.get("_orders", 0)
                    nc_aov = nc_rev / nc_orders if nc_orders > 0 else 0
                    nc_cpa = spend / nc_orders if nc_orders > 0 else 0
                    nc_roas = nc_rev / spend if spend > 0 else 0
                    nc_conv = nc_orders / sessions * 100 if sessions > 0 else 0
                    cost_per_nc = spend / nc_orders if nc_orders > 0 else 0

                    # DTC row
                    rows_dtc.append({
                        period_col: period_label,
                        "Spend": f"${spend:,.0f}",
                        "Total Rev": f"${total_rev:,.0f}",
                        "New Users": int(nc_orders),
                        "Cost/New User": f"${cost_per_nc:,.0f}",
                        "NC Conv %": f"{nc_conv:.2f}%",
                        "NC Rev": f"${nc_rev:,.0f}",
                        "NC Orders": int(nc_orders),
                        "NC AOV": f"${nc_aov:,.0f}",
                        "NC ROAS": f"{nc_roas:.2f}x",
                        "NC CPA": f"${nc_cpa:,.0f}",
                    })

                    # Roll Up: combine with Amazon if available
                    amz_rev_val = 0
                    amz_spend_val = 0
                    if amz_agg is not None and period_col in amz_agg.columns:
                        amz_match = amz_agg[amz_agg[period_col] == period_label]
                        if not amz_match.empty:
                            amz_rev_val = float(amz_match.iloc[0].get("_amz_revenue", 0))
                            amz_spend_val = float(amz_match.iloc[0].get("_amz_spend", 0))

                    combined_rev = total_rev + amz_rev_val
                    combined_spend = spend + amz_spend_val
                    nc_mer = nc_rev / combined_spend if combined_spend > 0 else 0
                    mer = combined_rev / combined_spend if combined_spend > 0 else 0

                    rows_rollup.append({
                        period_col: period_label,
                        "Spend": f"${combined_spend:,.0f}",
                        "Revenue": f"${combined_rev:,.0f}",
                        "NC Rev": f"${nc_rev:,.0f}",
                        "NC Orders": int(nc_orders),
                        "NC MER": f"{nc_mer:.2f}x",
                        "NC CPA": f"${nc_cpa:,.0f}",
                        "MER": f"{mer:.2f}x",
                    })

                    # Amazon row
                    rows_amz.append({
                        period_col: period_label,
                        "Spend": f"${amz_spend_val:,.0f}",
                        "Total Revenue": f"${amz_rev_val:,.0f}",
                    })

                return (
                    pd.DataFrame(rows_dtc),
                    pd.DataFrame(rows_rollup),
                    pd.DataFrame(rows_amz),
                )

            # --- Period-over-period color helper for DoD/WoW/MoM tables ---
            # For each numeric column, compare to previous period and color green/red
            # "higher_is_good" = True: increase = green, decrease = red
            # "higher_is_good" = False: increase = red, decrease = green (e.g. CPA)
            _PERF_COL_DIRECTION = {
                # Roll Up columns
                "Spend": True,          # spending more = investing = good
                "Revenue": True,        # more revenue = good
                "Total Rev": True,
                "Total Revenue": True,
                "NC Rev": True,         # more NC revenue = good
                "NC Orders": True,      # more new customers = good
                "New Users": True,
                "NC MER": True,         # higher efficiency = good
                "MER": True,            # higher efficiency = good
                "NC ROAS": True,        # higher return = good
                "NC Conv %": True,      # higher conversion = good
                "NC AOV": True,         # higher AOV = good
                "NC CPA": False,        # lower CPA = good
                "Cost/New User": False,  # lower cost = good
            }

            def _parse_perf_num(val):
                """Extract numeric value from formatted string like '$1,234' or '1.50x' or '2.3%'."""
                if isinstance(val, (int, float)):
                    return float(val)
                if not isinstance(val, str):
                    return None
                s = val.replace("$", "").replace(",", "").replace("x", "").replace("%", "").strip()
                try:
                    return float(s)
                except (ValueError, TypeError):
                    return None

            def _gradient_perf_style(pct_change, higher_is_good):
                """Return CSS for a cell based on gradient intensity of change.

                Maps pct_change to a smooth red ↔ yellow ↔ green gradient:
                  - Deep green   (+20%+ good change)
                  - Light green  (+10% good change)
                  - Yellow/amber (~0% or small change, within ±3%)
                  - Light red    (-10% bad change)
                  - Deep red     (-20%+ bad change)
                """
                # Determine if the change is favorable
                signed = pct_change if higher_is_good else -pct_change

                # Clamp to ±30% for gradient mapping
                t = max(-0.30, min(0.30, signed))

                if abs(t) < 0.03:
                    # Neutral zone — soft amber/yellow
                    return "color: #92700c; font-weight: 500; background-color: rgba(245,166,35,0.10)"

                if t > 0:
                    # Positive (good) — interpolate from light green to deep green
                    # t ranges 0.03 → 0.30, map to intensity 0.0 → 1.0
                    intensity = min(1.0, (t - 0.03) / 0.27)
                    # Text color: light green #15803d → deep green #065f26
                    r = int(21 - 11 * intensity)      # 21 → 6
                    g = int(128 - 33 * intensity)     # 128 → 95
                    b = int(61 - 23 * intensity)      # 61 → 38
                    # Background opacity: 0.06 → 0.18
                    bg_alpha = 0.06 + 0.12 * intensity
                    return (
                        f"color: rgb({r},{g},{b}); font-weight: 600; "
                        f"background-color: rgba(16,185,80,{bg_alpha:.2f})"
                    )
                else:
                    # Negative (bad) — interpolate from light red to deep red
                    intensity = min(1.0, (abs(t) - 0.03) / 0.27)
                    # Text color: light red #dc2626 → deep red #991b1b
                    r = int(220 - 67 * intensity)     # 220 → 153
                    g = int(38 - 11 * intensity)      # 38 → 27
                    b = int(38 - 11 * intensity)      # 38 → 27
                    # Background opacity: 0.05 → 0.16
                    bg_alpha = 0.05 + 0.11 * intensity
                    return (
                        f"color: rgb({r},{g},{b}); font-weight: 600; "
                        f"background-color: rgba(220,38,38,{bg_alpha:.2f})"
                    )

            def _render_perf_table_colored(df, period_col, max_height=420):
                """Render a performance table with gradient period-over-period color coding."""
                if df.empty or len(df) < 2:
                    _render_df_as_html(df, max_height=max_height)
                    return

                # Sort descending for display (most recent first)
                display_df = df.sort_values(period_col, ascending=False).reset_index(drop=True)
                numeric_cols = [c for c in display_df.columns if c != period_col]

                def _apply_display_styles(row):
                    idx = row.name
                    styles = [""] * len(row)
                    col_map = {c: i for i, c in enumerate(display_df.columns)}

                    # Bold period column
                    if period_col in col_map:
                        styles[col_map[period_col]] = "font-weight: 600; color: #0F3557"

                    # Compare with next row (since sorted desc, next row = previous period)
                    if idx >= len(display_df) - 1:
                        return styles

                    for col in numeric_cols:
                        if col not in col_map:
                            continue
                        curr_val = _parse_perf_num(display_df.iloc[idx][col])
                        prev_val = _parse_perf_num(display_df.iloc[idx + 1][col])

                        if curr_val is None or prev_val is None or prev_val == 0:
                            continue

                        pct_change = (curr_val - prev_val) / abs(prev_val)
                        higher_is_good = _PERF_COL_DIRECTION.get(col, True)

                        styles[col_map[col]] = _gradient_perf_style(pct_change, higher_is_good)

                    return styles

                styled = (
                    display_df.style
                    .set_properties(**{
                        "font-size": "0.84rem",
                        "font-family": "Visby CF, DM Sans, -apple-system, sans-serif",
                        "color": "#1e2d3d",
                        "background-color": "#ffffff",
                    })
                    .apply(_apply_display_styles, axis=1)
                    .set_table_styles([
                        {"selector": "th", "props": [
                            ("background-color", "#F0F4F8"),
                            ("color", "#0F3557"),
                            ("font-weight", "600"),
                            ("font-size", "0.74rem"),
                            ("text-transform", "uppercase"),
                            ("letter-spacing", "0.05em"),
                            ("border-bottom", "2px solid #D6DEE8"),
                            ("padding", "11px 14px"),
                            ("position", "sticky"),
                            ("top", "0"),
                            ("z-index", "1"),
                        ]},
                        {"selector": "td", "props": [
                            ("border-bottom", "1px solid #F0F4F8"),
                            ("padding", "9px 14px"),
                            ("white-space", "nowrap"),
                        ]},
                    ])
                    .hide(axis="index")
                )
                html = styled.to_html()
                height_style = f"max-height:{max_height}px;overflow-y:auto;" if max_height else ""
                st.markdown(
                    f'<div style="background:#ffffff;border-radius:12px;'
                    f'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
                    f'width:100%;overflow:hidden;">'
                    f'<div class="perf-table" style="overflow-x:auto;{height_style}">'
                    '<style>'
                    '.perf-table table { width:100%; border-collapse:collapse; }'
                    '.perf-table th, .perf-table td { white-space:nowrap; }'
                    '.perf-table th { position:sticky; top:0; z-index:1; background-color:#F0F4F8 !important; }'
                    '.perf-table tr:hover td:not([style*="background-color"]) { background:#F7FAFC !important; }'
                    '</style>'
                    f'{html}</div></div>',
                    unsafe_allow_html=True,
                )

            # --- Aggregate helpers ---
            # We use the full (unfiltered by date range) dataset for these tabs
            # Reload the full data to avoid date-filter distortion
            with get_db() as _perf_conn:
                _perf_df = pd.read_sql_query("SELECT * FROM google_sheet_data ORDER BY id", _perf_conn)
            _perf_df["_date"] = pd.to_datetime(_perf_df["date"], format="mixed", dayfirst=False)
            _perf_df = _perf_df.sort_values("_date")
            # Apply same Triple Whale 0.5x adjustment
            for _pc in ["blended_ad_spend", "total_sales", "new_customer_orders",
                         "new_customer_revenue", "returning_customer_revenue",
                         "sessions", "orders", "units_sold"]:
                if _pc in _perf_df.columns:
                    _perf_df[f"_{_pc}_n"] = _perf_df[_pc].apply(_clean_num)
            _perf_df["_ad_spend"] = _perf_df.get("_blended_ad_spend_n", pd.Series(0, index=_perf_df.index)) * _TW_ADJ
            _perf_df["_revenue"] = _perf_df.get("_total_sales_n", pd.Series(0, index=_perf_df.index)) * _TW_ADJ
            _perf_df["_nc_orders"] = _perf_df.get("_new_customer_orders_n", pd.Series(0, index=_perf_df.index)) * _TW_ADJ
            _perf_df["_nc_revenue"] = _perf_df.get("_new_customer_revenue_n", pd.Series(0, index=_perf_df.index)) * _TW_ADJ
            _perf_df["_ret_revenue"] = _perf_df.get("_returning_customer_revenue_n", pd.Series(0, index=_perf_df.index)) * _TW_ADJ
            _perf_df["_sessions"] = _perf_df.get("_sessions_n", pd.Series(0, index=_perf_df.index))  # Sessions not doubled
            _perf_df["_orders"] = _perf_df.get("_orders_n", pd.Series(0, index=_perf_df.index)) * _TW_ADJ
            _perf_df["_units"] = _perf_df.get("_units_sold_n", pd.Series(0, index=_perf_df.index)) * _TW_ADJ

            # Amazon daily data from daily_sku_sales
            _amz_daily = pd.DataFrame()
            try:
                with get_db() as _amz_perf_conn:
                    _amz_daily = pd.read_sql_query(
                        "SELECT sale_date, SUM(revenue) as _amz_revenue, 0 as _amz_spend "
                        "FROM daily_sku_sales WHERE source = 'amazon' "
                        "GROUP BY sale_date ORDER BY sale_date",
                        _amz_perf_conn,
                    )
                if not _amz_daily.empty:
                    _amz_daily["_date"] = pd.to_datetime(_amz_daily["sale_date"])
            except Exception:
                pass

            # --- DAY OVER DAY ---
            with _perf_tab_dod:
                _dod_df = _perf_df.copy()
                _dod_df["Day"] = _dod_df["_date"].dt.strftime("%Y-%m-%d")
                _dod_agg_cols = {"_ad_spend": "sum", "_revenue": "sum", "_nc_orders": "sum",
                                 "_nc_revenue": "sum", "_ret_revenue": "sum",
                                 "_sessions": "sum", "_orders": "sum", "_units": "sum"}
                _dod_agg = _dod_df.groupby("Day", sort=True).agg(_dod_agg_cols).reset_index()

                # Amazon daily
                _amz_dod = pd.DataFrame(columns=["Day", "_amz_revenue", "_amz_spend"])
                if not _amz_daily.empty:
                    _amz_dod = _amz_daily.copy()
                    _amz_dod["Day"] = _amz_dod["_date"].dt.strftime("%Y-%m-%d")
                    _amz_dod = _amz_dod.groupby("Day", sort=True).agg(
                        _amz_revenue=("_amz_revenue", "sum"),
                        _amz_spend=("_amz_spend", "sum"),
                    ).reset_index()

                _dtc_dod, _rollup_dod, _amz_tbl_dod = _build_perf_table(_dod_agg, "Day", _amz_dod)

                if _chan_sel in ("All", "Roll Up"):
                    st.subheader("Roll Up")
                    _render_perf_table_colored(_rollup_dod, "Day", max_height=420)
                if _chan_sel in ("All", "DTC"):
                    st.subheader("DTC (Shopify)")
                    _render_perf_table_colored(_dtc_dod, "Day", max_height=420)
                if _chan_sel in ("All", "Amazon"):
                    st.subheader("Amazon")
                    _render_perf_table_colored(_amz_tbl_dod, "Day", max_height=420)

            # --- WEEK OVER WEEK ---
            with _perf_tab_wow:
                _wow_df = _perf_df.copy()
                _wow_df["_week_start"] = _wow_df["_date"].dt.to_period("W-SAT").apply(lambda x: x.start_time)
                _wow_df["Week"] = _wow_df["_week_start"].dt.strftime("%Y-%m-%d")
                _wow_agg = _wow_df.groupby("Week", sort=True).agg(
                    _ad_spend=("_ad_spend", "sum"), _revenue=("_revenue", "sum"),
                    _nc_orders=("_nc_orders", "sum"), _nc_revenue=("_nc_revenue", "sum"),
                    _ret_revenue=("_ret_revenue", "sum"), _sessions=("_sessions", "sum"),
                    _orders=("_orders", "sum"), _units=("_units", "sum"),
                ).reset_index()

                # Amazon weekly
                _amz_wow = pd.DataFrame(columns=["Week", "_amz_revenue", "_amz_spend"])
                if not _amz_daily.empty:
                    _amz_wow_tmp = _amz_daily.copy()
                    _amz_wow_tmp["_week_start"] = _amz_wow_tmp["_date"].dt.to_period("W-SAT").apply(lambda x: x.start_time)
                    _amz_wow_tmp["Week"] = _amz_wow_tmp["_week_start"].dt.strftime("%Y-%m-%d")
                    _amz_wow = _amz_wow_tmp.groupby("Week", sort=True).agg(
                        _amz_revenue=("_amz_revenue", "sum"),
                        _amz_spend=("_amz_spend", "sum"),
                    ).reset_index()

                _dtc_wow, _rollup_wow, _amz_tbl_wow = _build_perf_table(_wow_agg, "Week", _amz_wow)

                if _chan_sel in ("All", "Roll Up"):
                    st.subheader("Roll Up")
                    _render_perf_table_colored(_rollup_wow, "Week", max_height=420)
                if _chan_sel in ("All", "DTC"):
                    st.subheader("DTC (Shopify)")
                    _render_perf_table_colored(_dtc_wow, "Week", max_height=420)
                if _chan_sel in ("All", "Amazon"):
                    st.subheader("Amazon")
                    _render_perf_table_colored(_amz_tbl_wow, "Week", max_height=420)

            # --- MONTH OVER MONTH ---
            with _perf_tab_mom:
                _mom_df = _perf_df.copy()
                _mom_df["Month"] = _mom_df["_date"].dt.to_period("M").astype(str)
                _mom_agg = _mom_df.groupby("Month", sort=True).agg(
                    _ad_spend=("_ad_spend", "sum"), _revenue=("_revenue", "sum"),
                    _nc_orders=("_nc_orders", "sum"), _nc_revenue=("_nc_revenue", "sum"),
                    _ret_revenue=("_ret_revenue", "sum"), _sessions=("_sessions", "sum"),
                    _orders=("_orders", "sum"), _units=("_units", "sum"),
                ).reset_index()

                # Amazon monthly
                _amz_mom = pd.DataFrame(columns=["Month", "_amz_revenue", "_amz_spend"])
                if not _amz_daily.empty:
                    _amz_mom_tmp = _amz_daily.copy()
                    _amz_mom_tmp["Month"] = _amz_mom_tmp["_date"].dt.to_period("M").astype(str)
                    _amz_mom = _amz_mom_tmp.groupby("Month", sort=True).agg(
                        _amz_revenue=("_amz_revenue", "sum"),
                        _amz_spend=("_amz_spend", "sum"),
                    ).reset_index()

                _dtc_mom, _rollup_mom, _amz_tbl_mom = _build_perf_table(_mom_agg, "Month", _amz_mom)

                if _chan_sel in ("All", "Roll Up"):
                    st.subheader("Roll Up")
                    _render_perf_table_colored(_rollup_mom, "Month")
                if _chan_sel in ("All", "DTC"):
                    st.subheader("DTC (Shopify)")
                    _render_perf_table_colored(_dtc_mom, "Month")
                if _chan_sel in ("All", "Amazon"):
                    st.subheader("Amazon")
                    _render_perf_table_colored(_amz_tbl_mom, "Month")

            st.divider()

            # ============================================================
            # SECTION 1: NEW CUSTOMER ACQUISITION (Period Detail)
            # ============================================================
            st.header("New Customer Acquisition")

            nc_cpa_avg = total_spend / total_nc if total_nc > 0 else 0
            nc_roas = nc_rev / total_spend if total_spend > 0 else 0
            nc_aov_avg = nc_rev / total_nc if total_nc > 0 else 0

            nc1, nc2, nc3, nc4, nc5 = st.columns(5)
            nc1.metric("New Customers", f"{total_nc:,}")
            nc2.metric("NC Revenue", f"${nc_rev:,.0f}")
            nc3.metric("Ad Spend", f"${total_spend:,.0f}")
            nc4.metric("NC CPA", f"${nc_cpa_avg:,.0f}", help="Total ad spend / new customer orders")
            nc5.metric("NC ROAS", f"{nc_roas:.2f}x", help="New customer revenue / ad spend")

            st.divider()

            # NC Revenue & Spend over time
            st.subheader("NC Revenue vs Ad Spend")
            fig_nc = go.Figure()
            fig_nc.add_trace(go.Bar(
                x=mkt_df["_date"], y=mkt_df["_nc_revenue"],
                name="NC Revenue", marker_color="#F58B3D",
            ))
            fig_nc.add_trace(go.Bar(
                x=mkt_df["_date"], y=mkt_df["_ad_spend"],
                name="Ad Spend", marker_color="#E05252",
            ))
            fig_nc.update_layout(
                barmode="group", height=320,
                margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(gridcolor="#E8EDF3"),
            )
            st.plotly_chart(fig_nc, use_container_width=True)

            # Channel breakdown for NC
            st.subheader("Spend by Channel")
            ch1, ch2 = st.columns(2)
            with ch1:
                st.markdown("**Facebook / Meta**")
                fb_spend = mkt_df["_fb_spend"].sum()
                fb_cpa_vals = mkt_df.get("facebook_cpa", pd.Series(0)).apply(_clean_num)
                fb_cpa = fb_cpa_vals[fb_cpa_vals > 0].mean() if (fb_cpa_vals > 0).any() else 0
                fb1, fb2 = st.columns(2)
                fb1.metric("Spend", f"${fb_spend:,.0f}")
                fb2.metric("CPA", f"${fb_cpa:.0f}")
                fig_fb = go.Figure()
                fig_fb.add_trace(go.Scatter(x=mkt_df["_date"], y=mkt_df["_fb_spend"], fill="tozeroy", line=dict(color="#1877F2")))
                fig_fb.update_layout(height=160, margin=dict(l=0, r=0, t=10, b=0),
                                     plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig_fb, use_container_width=True)
            with ch2:
                st.markdown("**Google Ads**")
                goog_spend = mkt_df["_goog_spend"].sum()
                goog_cpa_vals = mkt_df.get("google_cpa", pd.Series(0)).apply(_clean_num)
                goog_cpa = goog_cpa_vals[goog_cpa_vals > 0].mean() if (goog_cpa_vals > 0).any() else 0
                g1, g2 = st.columns(2)
                g1.metric("Spend", f"${goog_spend:,.0f}")
                g2.metric("CPA", f"${goog_cpa:.0f}")
                fig_goog = go.Figure()
                fig_goog.add_trace(go.Scatter(x=mkt_df["_date"], y=mkt_df["_goog_spend"], fill="tozeroy", line=dict(color="#34A853")))
                fig_goog.update_layout(height=160, margin=dict(l=0, r=0, t=10, b=0),
                                       plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig_goog, use_container_width=True)

            st.divider()

            # ============================================================
            # SECTION 2: REPEAT / RETURNING CUSTOMERS
            # ============================================================
            st.header("Repeat Revenue")

            ret_aov = ret_rev / total_ret if total_ret > 0 else 0
            ret_pct = ret_rev / total_rev * 100 if total_rev > 0 else 0

            rc1, rc2, rc3, rc4 = st.columns(4)
            rc1.metric("Returning Orders", f"{total_ret:,}")
            rc2.metric("Repeat Revenue", f"${ret_rev:,.0f}")
            rc3.metric("Repeat AOV", f"${ret_aov:,.0f}")
            rc4.metric("% of Total Revenue", f"{ret_pct:.0f}%")

            # Repeat revenue over time
            fig_ret = go.Figure()
            fig_ret.add_trace(go.Bar(
                x=mkt_df["_date"], y=mkt_df["_ret_revenue"],
                name="Repeat Revenue", marker_color="#5BB8D4",
            ))
            fig_ret.update_layout(
                height=280, margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(gridcolor="#E8EDF3"),
            )
            st.plotly_chart(fig_ret, use_container_width=True)

            # Subscription metrics
            if "total_active_subscriptions" in mkt_df.columns:
                st.subheader("Subscriptions")
                mkt_df["_active_subs"] = mkt_df["total_active_subscriptions"].apply(_clean_num)
                mkt_df["_new_subs"] = mkt_df.get("total_new_subscriptions", pd.Series(0)).apply(_clean_num)
                mkt_df["_cancelled_subs"] = mkt_df.get("total_cancelled_subscriptions", pd.Series(0)).apply(_clean_num)
                mkt_df["_sub_rev"] = mkt_df.get("total_subscription_order_revenue", pd.Series(0)).apply(_clean_num)

                sub1, sub2, sub3, sub4 = st.columns(4)
                sub1.metric("Active Subscriptions", f"{mkt_df['_active_subs'].iloc[-1]:,.0f}")
                sub2.metric("New Subs (Period)", f"{mkt_df['_new_subs'].sum():,.0f}")
                sub3.metric("Cancelled (Period)", f"{mkt_df['_cancelled_subs'].sum():,.0f}")
                sub4.metric("Sub Revenue", f"${mkt_df['_sub_rev'].sum():,.0f}")

                fig_subs = go.Figure()
                fig_subs.add_trace(go.Scatter(
                    x=mkt_df["_date"], y=mkt_df["_active_subs"],
                    name="Active Subscriptions", fill="tozeroy",
                    line=dict(color="#6B8FA3"),
                ))
                fig_subs.update_layout(height=200, margin=dict(l=0, r=0, t=10, b=0),
                                       plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                                       yaxis=dict(gridcolor="#E8EDF3"))
                st.plotly_chart(fig_subs, use_container_width=True)

                fig_net_sub = go.Figure()
                fig_net_sub.add_trace(go.Bar(x=mkt_df["_date"], y=mkt_df["_new_subs"], name="New", marker_color="#2DA87E"))
                fig_net_sub.add_trace(go.Bar(x=mkt_df["_date"], y=-mkt_df["_cancelled_subs"], name="Cancelled", marker_color="#E05252"))
                fig_net_sub.update_layout(
                    barmode="relative", height=200, margin=dict(l=0, r=0, t=10, b=0),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    yaxis=dict(gridcolor="#E8EDF3"),
                )
                st.plotly_chart(fig_net_sub, use_container_width=True)

            st.divider()

            # ============================================================
            # SECTION 3: COMBINED SUMMARY
            # ============================================================
            st.header("Weekly Summary")
            mkt_df["_week"] = mkt_df["_date"].dt.to_period("W").apply(lambda x: x.start_time)
            weekly = mkt_df.groupby("_week").agg(
                Revenue=("_revenue", "sum"),
                NC_Revenue=("_nc_revenue", "sum"),
                Ret_Revenue=("_ret_revenue", "sum"),
                Ad_Spend=("_ad_spend", "sum"),
                Orders=("_orders", "sum"),
                NC_Orders=("_nc_orders", "sum"),
                Sessions=("_sessions", "sum"),
            ).reset_index()
            weekly["NC ROAS"] = (weekly["NC_Revenue"] / weekly["Ad_Spend"]).round(2).replace([float("inf")], 0)
            weekly["NC CPA"] = (weekly["Ad_Spend"] / weekly["NC_Orders"]).round(0).replace([float("inf")], 0)
            weekly["_week"] = weekly["_week"].dt.strftime("%Y-%m-%d")
            weekly = weekly.rename(columns={
                "_week": "Week", "NC_Revenue": "NC Rev", "Ret_Revenue": "Repeat Rev",
                "Ad_Spend": "Ad Spend", "NC_Orders": "New Custs",
            })
            for col in ["Revenue", "NC Rev", "Repeat Rev", "Ad Spend", "NC CPA"]:
                weekly[col] = weekly[col].apply(lambda x: f"${x:,.0f}")
            weekly["Orders"] = weekly["Orders"].astype(int)
            weekly["New Custs"] = weekly["New Custs"].astype(int)
            weekly["Sessions"] = weekly["Sessions"].astype(int)
            weekly["NC ROAS"] = weekly["NC ROAS"].apply(lambda x: f"{x:.2f}x")
            _render_df_as_html_global(weekly.sort_values("Week", ascending=False))

            # Conversion funnel
            st.subheader("Conversion Funnel")
            total_sessions = int(mkt_df["_sessions"].sum())
            total_atc = int(mkt_df["_atc"].sum())
            atc_rate = total_atc / total_sessions * 100 if total_sessions > 0 else 0
            cvr = total_orders / total_sessions * 100 if total_sessions > 0 else 0

            fn1, fn2, fn3, fn4 = st.columns(4)
            fn1.metric("Sessions", f"{total_sessions:,}")
            fn2.metric("Add to Cart", f"{total_atc:,}", f"{atc_rate:.1f}%")
            fn3.metric("Orders", f"{total_orders:,}", f"{cvr:.2f}% CVR")
            fn4.metric("Units Sold", f"{int(mkt_df['_units'].sum()):,}")

        else:
            st.info("No marketing data available. Sync your Google Sheet from the Settings page.")
    else:
        st.info("No marketing data available. Sync your Google Sheet from the **Settings** page.")

    # --- Klaviyo Section ---
    st.divider()
    st.subheader("Klaviyo")
    if _mkt_klaviyo_key:
        kl_tab1, kl_tab2, kl_tab3 = st.tabs(["Campaigns", "Flows", "Lists"])
        with kl_tab1:
            if st.button("Load Campaigns", key="kl_load_campaigns"):
                with st.spinner("Fetching from Klaviyo..."):
                    from etl.klaviyo_client import fetch_campaigns
                    kl_campaigns = fetch_campaigns(_mkt_klaviyo_key, status="sent")
                    if kl_campaigns:
                        _render_df_as_html_global(pd.DataFrame(kl_campaigns))
                    else:
                        st.warning("No sent campaigns found.")
        with kl_tab2:
            if st.button("Load Flows", key="kl_load_flows"):
                with st.spinner("Fetching from Klaviyo..."):
                    from etl.klaviyo_client import fetch_flows
                    kl_flows = fetch_flows(_mkt_klaviyo_key)
                    if kl_flows:
                        _render_df_as_html_global(pd.DataFrame(kl_flows))
                    else:
                        st.warning("No flows found.")
        with kl_tab3:
            if st.button("Load Lists", key="kl_load_lists"):
                with st.spinner("Fetching from Klaviyo..."):
                    from etl.klaviyo_client import fetch_lists
                    kl_lists = fetch_lists(_mkt_klaviyo_key)
                    if kl_lists:
                        _render_df_as_html_global(pd.DataFrame(kl_lists))
                    else:
                        st.warning("No lists found.")
    else:
        st.info("Add your Klaviyo API key on the **Settings** page to see email/SMS analytics.")


# ================================================================
# PAGE: FINANCIALS (Highbeam/Ramp bank data)
# ================================================================
elif page == "Financials":
    st.title("Financials")

    with get_db() as conn:
        try:
            fin_cnt = conn.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0]
        except Exception:
            fin_cnt = 0

    if fin_cnt == 0:
        st.info("No transaction data yet. Upload a Ramp or Highbeam CSV on the **Settings** page.")
    else:
        with get_db() as conn:
            # Load all data — we'll filter intelligently
            fin_all = pd.read_sql_query("SELECT * FROM bank_transactions ORDER BY date", conn)

        fin_all["_date"] = pd.to_datetime(fin_all["date"], format="mixed", dayfirst=False)
        fin_all = fin_all.sort_values("_date")
        fin_all["_amount"] = fin_all["amount"].astype(float)

        # Determine data source type
        has_highbeam = (fin_all["source"] == "highbeam").any() if "source" in fin_all.columns else False
        has_ramp = (fin_all["source"] == "ramp").any() if "source" in fin_all.columns else False
        has_direction = "direction" in fin_all.columns and fin_all["direction"].notna().any()

        # Date range filter with smart presets
        fin_start, fin_end = _smart_date_filter(
            fin_all["_date"].min().date(), fin_all["_date"].max().date(), "fin"
        )
        fin_all = fin_all[(fin_all["_date"].dt.date >= fin_start) & (fin_all["_date"].dt.date <= fin_end)]

        if fin_all.empty:
            st.warning("No transactions in the selected date range.")
        else:
            # Exclude internal transfers and Ramp CC payments (avoid double-counting)
            _is_transfer = fin_all.get("is_transfer", pd.Series(0, index=fin_all.index)).fillna(0).astype(int)
            _is_ramp_dup = fin_all.get("is_ramp_duplicate", pd.Series(0, index=fin_all.index)).fillna(0).astype(int)
            fin_clean = fin_all[(_is_transfer == 0) & (_is_ramp_dup == 0)].copy()

            excluded_count = len(fin_all) - len(fin_clean)
            if excluded_count > 0:
                st.caption(
                    f"{excluded_count} transactions excluded (internal transfers & CC payments)."
                )

            # Separate inflows and outflows
            if has_direction and has_highbeam:
                # Highbeam data has Credit/Debit direction
                inflows = fin_clean[fin_clean["direction"] == "credit"]
                outflows = fin_clean[fin_clean["direction"] == "debit"]
            else:
                # Ramp data is all outflows (card charges)
                inflows = pd.DataFrame()
                outflows = fin_clean

            total_in = inflows["_amount"].sum() if not inflows.empty else 0
            total_out = outflows["_amount"].sum() if not outflows.empty else 0
            net_cash = total_in - total_out
            days_range = max((fin_clean["_date"].max() - fin_clean["_date"].min()).days, 1)

            # === KPI CARDS ===
            fk1, fk2, fk3, fk4, fk5 = st.columns(5)
            fk1.metric("Cash In", f"${total_in:,.0f}")
            fk2.metric("Cash Out", f"${total_out:,.0f}")
            fk3.metric("Net Cash Flow", f"${net_cash:,.0f}",
                        delta=f"${net_cash/days_range*30:,.0f}/mo",
                        delta_color="normal" if net_cash >= 0 else "inverse")
            fk4.metric("Avg Daily In", f"${total_in/days_range:,.0f}")
            fk5.metric("Avg Daily Out", f"${total_out/days_range:,.0f}")

            st.divider()

            # === CASH FLOW CHART ===
            st.subheader("Cash Flow")
            _cf_group = st.radio("Group by", ["Daily", "Weekly", "Monthly"], horizontal=True, key="cf_group")
            if _cf_group == "Weekly":
                fin_clean["_period"] = fin_clean["_date"].dt.to_period("W").apply(lambda x: x.start_time)
            elif _cf_group == "Monthly":
                fin_clean["_period"] = fin_clean["_date"].dt.to_period("M").apply(lambda x: x.start_time)
            else:
                fin_clean["_period"] = fin_clean["_date"]

            if has_direction and not inflows.empty:
                # Show inflows vs outflows
                inflows["_period"] = fin_clean.loc[inflows.index, "_period"] if not inflows.empty else pd.Series()
                outflows["_period"] = fin_clean.loc[outflows.index, "_period"] if not outflows.empty else pd.Series()

                cf_in = inflows.groupby("_period")["_amount"].sum().reset_index().rename(columns={"_amount": "Cash In"})
                cf_out = outflows.groupby("_period")["_amount"].sum().reset_index().rename(columns={"_amount": "Cash Out"})
                cf_merged = cf_in.merge(cf_out, on="_period", how="outer").fillna(0).sort_values("_period")
                cf_merged["Net"] = cf_merged["Cash In"] - cf_merged["Cash Out"]

                fig_cf = go.Figure()
                fig_cf.add_trace(go.Bar(
                    x=cf_merged["_period"], y=cf_merged["Cash In"],
                    name="Cash In", marker_color="#2DA87E",
                ))
                fig_cf.add_trace(go.Bar(
                    x=cf_merged["_period"], y=-cf_merged["Cash Out"],
                    name="Cash Out", marker_color="#E05252",
                ))
                fig_cf.add_trace(go.Scatter(
                    x=cf_merged["_period"], y=cf_merged["Net"],
                    name="Net Cash Flow", mode="lines+markers",
                    line=dict(color="#0F3557", width=2),
                ))
                fig_cf.update_layout(
                    barmode="relative", height=400,
                    margin=dict(l=0, r=0, t=30, b=0),
                    yaxis_title="$",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                )
                st.plotly_chart(fig_cf, use_container_width=True)
            else:
                # Outflows only (Ramp data)
                cf_out = outflows.groupby("_period")["_amount"].sum().reset_index()
                fig_cf = go.Figure()
                fig_cf.add_trace(go.Bar(
                    x=cf_out["_period"], y=cf_out["_amount"],
                    name="Spend", marker_color="#5BB8D4",
                ))
                if len(cf_out) > 5:
                    cf_out["_ma"] = cf_out["_amount"].rolling(window=min(7, len(cf_out)), min_periods=1).mean()
                    fig_cf.add_trace(go.Scatter(
                        x=cf_out["_period"], y=cf_out["_ma"],
                        name="Moving Avg", mode="lines",
                        line=dict(color="#E05252", width=2),
                    ))
                fig_cf.update_layout(
                    height=400, margin=dict(l=0, r=0, t=30, b=0), yaxis_title="$",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                )
                st.plotly_chart(fig_cf, use_container_width=True)

            st.divider()

            # === INFLOWS BREAKDOWN (if Highbeam data) ===
            if not inflows.empty:
                st.subheader("Cash Inflows")
                in_cats = inflows.groupby("category")["_amount"].sum().sort_values(ascending=False).reset_index()
                in_cats.columns = ["Source", "Total"]

                in_c1, in_c2 = st.columns([2, 1])
                with in_c1:
                    fig_in = go.Figure(data=[go.Pie(
                        labels=in_cats["Source"], values=in_cats["Total"],
                        hole=0.4,
                        marker_colors=["#2DA87E", "#5BB8D4", "#F58B3D", "#6B8FA3", "#F4A3A0"][:len(in_cats)],
                    )])
                    fig_in.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
                    st.plotly_chart(fig_in, use_container_width=True)
                with in_c2:
                    in_cats["Pct"] = (in_cats["Total"] / in_cats["Total"].sum() * 100).round(1).apply(lambda x: f"{x:.1f}%")
                    in_cats["Total"] = in_cats["Total"].apply(lambda x: f"${x:,.0f}")
                    _render_df_as_html_global(in_cats)
                st.divider()

            # === OUTFLOWS BREAKDOWN ===
            if not outflows.empty:
                st.subheader("Cash Outflows")
                out_cats = outflows.groupby("category")["_amount"].sum().sort_values(ascending=False).reset_index()
                out_cats.columns = ["Category", "Total"]

                out_c1, out_c2 = st.columns([2, 1])
                with out_c1:
                    fig_out = go.Figure()
                    _colors = ["#E05252", "#F58B3D", "#5BB8D4", "#6B8FA3", "#F4A3A0",
                               "#0F3557", "#2DA87E", "#FFC857", "#7a8da0", "#C5E0A5"]
                    fig_out.add_trace(go.Bar(
                        x=out_cats["Category"], y=out_cats["Total"],
                        marker_color=_colors[:len(out_cats)],
                    ))
                    fig_out.update_layout(height=350, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="$")
                    st.plotly_chart(fig_out, use_container_width=True)

                with out_c2:
                    out_cats["Pct"] = (out_cats["Total"] / out_cats["Total"].sum() * 100).round(1).apply(lambda x: f"{x:.1f}%")
                    out_cats["Total"] = out_cats["Total"].apply(lambda x: f"${x:,.0f}")
                    _render_df_as_html_global(out_cats)

                st.divider()

                # --- Top Merchants (outflows only) ---
                st.subheader("Top Vendors")
                merchant_col = "merchant" if "merchant" in outflows.columns else "description"
                merch_spend = outflows.groupby(merchant_col).agg(
                    Total=("_amount", "sum"),
                    Count=("_amount", "count"),
                    Avg=("_amount", "mean"),
                ).sort_values("Total", ascending=False).reset_index().head(20)
                merch_spend.columns = ["Vendor", "Total Spend", "# Payments", "Avg Payment"]
                merch_spend["Total Spend"] = merch_spend["Total Spend"].apply(lambda x: f"${x:,.0f}")
                merch_spend["Avg Payment"] = merch_spend["Avg Payment"].apply(lambda x: f"${x:,.0f}")
                _render_df_as_html_global(merch_spend)

                st.divider()

            # === MONTHLY P&L SUMMARY ===
            st.subheader("Monthly P&L")
            fin_clean["_month"] = fin_clean["_date"].dt.to_period("M").astype(str)
            if has_direction:
                monthly_in = fin_clean[fin_clean["direction"] == "credit"].groupby("_month")["_amount"].sum()
                monthly_out = fin_clean[fin_clean["direction"] == "debit"].groupby("_month")["_amount"].sum()
                pnl = pd.DataFrame({"Cash In": monthly_in, "Cash Out": monthly_out}).fillna(0)
                pnl["Net"] = pnl["Cash In"] - pnl["Cash Out"]
                pnl = pnl.sort_index()
                pnl_display = pnl.copy()
                for c in pnl_display.columns:
                    pnl_display[c] = pnl_display[c].apply(lambda x: f"${x:,.0f}")
                _render_df_as_html_global(pnl_display)
            else:
                # Outflows only — show by category
                monthly_cat = outflows.pivot_table(
                    index="category", columns="_month" if "_month" in outflows.columns else outflows["_date"].dt.to_period("M").astype(str),
                    values="_amount", aggfunc="sum", fill_value=0,
                ).round(0)
                monthly_cat.loc["TOTAL"] = monthly_cat.sum()
                monthly_display = monthly_cat.map(lambda x: f"${x:,.0f}")
                _render_df_as_html_global(monthly_display)

            st.divider()

            # --- Spend by Department (Ramp data) ---
            if "department" in fin_clean.columns:
                depts = outflows[outflows["department"].notna() & (outflows["department"] != "") & (outflows["department"] != "nan")] if not outflows.empty else pd.DataFrame()
                if not depts.empty:
                    st.subheader("Spend by Department")
                    dept_spend = depts.groupby("department")["_amount"].sum().sort_values(ascending=False).reset_index()
                    dept_spend.columns = ["Department", "Total"]
                    fig_dept = go.Figure(data=[go.Pie(
                        labels=dept_spend["Department"], values=dept_spend["Total"], hole=0.4,
                    )])
                    fig_dept.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
                    st.plotly_chart(fig_dept, use_container_width=True)
                    st.divider()

            # --- Spend by User (Ramp data) ---
            if "user_name" in fin_clean.columns:
                users_df = outflows[outflows["user_name"].notna() & (outflows["user_name"] != "") & (outflows["user_name"] != "nan")] if not outflows.empty else pd.DataFrame()
                if not users_df.empty:
                    st.subheader("Spend by Team Member")
                    user_spend = users_df.groupby("user_name")["_amount"].agg(["sum", "count"]).sort_values("sum", ascending=False).reset_index()
                    user_spend.columns = ["Team Member", "Total Spend", "Transactions"]
                    user_spend["Total Spend"] = user_spend["Total Spend"].apply(lambda x: f"${x:,.0f}")
                    _render_df_as_html_global(user_spend)
                    st.divider()

            # --- Raw Transaction Table ---
            with st.expander("All Transactions"):
                _show_cols = ["_date", "direction", "merchant", "_amount", "category"]
                if "department" in fin_clean.columns:
                    _show_cols.append("department")
                if "user_name" in fin_clean.columns:
                    _show_cols.append("user_name")
                display_df = fin_clean[[c for c in _show_cols if c in fin_clean.columns]].copy()
                display_df.columns = [c.replace("_", "").title() for c in display_df.columns]
                if "Amount" in display_df.columns:
                    display_df["Amount"] = display_df["Amount"].apply(lambda x: f"${x:,.2f}")
                if "Date" in display_df.columns:
                    display_df["Date"] = pd.to_datetime(display_df["Date"]).dt.strftime("%Y-%m-%d")
                _render_df_as_html_global(display_df.sort_values("Date", ascending=False))


# ================================================================
# PAGE: SETTINGS
# ================================================================
elif page == "Settings":
    st.title("Settings")

    # --- Amazon SP-API ---
    st.subheader("Amazon SP-API")
    amazon_vals = {}
    amazon_vals["AMAZON_REFRESH_TOKEN"] = st.text_input(
        "Refresh Token",
        value=getattr(config, "AMAZON_REFRESH_TOKEN", ""),
        type="password",
        key="settings_AMAZON_REFRESH_TOKEN",
        help="From Seller Central → Develop Apps → Your App → Sandbox/Authorize page",
    )
    amazon_vals["AMAZON_LWA_CLIENT_ID"] = st.text_input(
        "LWA Client ID",
        value=getattr(config, "AMAZON_LWA_CLIENT_ID", ""),
        key="settings_AMAZON_LWA_CLIENT_ID",
        help="From Seller Central → Develop Apps → Your App → LWA Credentials (starts with amzn1.application-oa2-client...)",
    )
    amazon_vals["AMAZON_LWA_CLIENT_SECRET"] = st.text_input(
        "LWA Client Secret",
        value=getattr(config, "AMAZON_LWA_CLIENT_SECRET", ""),
        type="password",
        key="settings_AMAZON_LWA_CLIENT_SECRET",
        help="From the same LWA Credentials section",
    )
    amazon_vals["AMAZON_MARKETPLACE_ID"] = st.text_input(
        "Marketplace ID",
        value=getattr(config, "AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER"),
        key="settings_AMAZON_MARKETPLACE_ID",
        help="US marketplace is ATVPDKIKX0DER (default). Only change if selling in a different country.",
    )

    st.divider()

    # --- Shopify ---
    st.subheader("Shopify Admin API")
    shopify_vals = {}
    shopify_vals["SHOPIFY_STORE_URL"] = st.text_input(
        "Shopify Store URL",
        value=getattr(config, "SHOPIFY_STORE_URL", ""),
        placeholder="your-store.myshopify.com",
        key="settings_SHOPIFY_STORE_URL",
    )
    shopify_vals["SHOPIFY_CLIENT_ID"] = st.text_input(
        "Shopify Client ID",
        value=getattr(config, "SHOPIFY_CLIENT_ID", ""),
        key="settings_SHOPIFY_CLIENT_ID",
    )
    shopify_vals["SHOPIFY_CLIENT_SECRET"] = st.text_input(
        "Shopify Client Secret",
        value=getattr(config, "SHOPIFY_CLIENT_SECRET", ""),
        type="password",
        key="settings_SHOPIFY_CLIENT_SECRET",
    )
    shopify_vals["SHOPIFY_API_VERSION"] = st.text_input(
        "Shopify API Version",
        value=getattr(config, "SHOPIFY_API_VERSION", "2024-01"),
        key="settings_SHOPIFY_API_VERSION",
    )

    if getattr(config, "SHOPIFY_ACCESS_TOKEN", ""):
        st.success("Access token is saved.")
        if st.button("Test Shopify Connection"):
            from etl.shopify_client import test_connection
            ok, msg = test_connection()
            if ok:
                st.success(msg)
            else:
                st.error(msg)
    else:
        st.caption("Enter your Client ID and Secret from the Shopify Dev Dashboard, then click **Connect Shopify**.")

    st.divider()

    # --- Packiyo 3PL ---
    st.subheader("Packiyo 3PL")
    packiyo_vals = {}
    packiyo_vals["PACKIYO_API_TOKEN"] = st.text_input(
        "API Token",
        value=getattr(config, "PACKIYO_API_TOKEN", ""),
        type="password",
        key="settings_PACKIYO_API_TOKEN",
        help="Bearer token from Packiyo (e.g. 256|jKuI...)",
    )
    packiyo_vals["PACKIYO_API_URL"] = st.text_input(
        "API URL",
        value=getattr(config, "PACKIYO_API_URL", "https://aveshops.packiyo.com/api/v1"),
        key="settings_PACKIYO_API_URL",
        help="Packiyo API base URL",
    )
    packiyo_vals["PACKIYO_CUSTOMER_ID"] = st.text_input(
        "Customer ID",
        value=getattr(config, "PACKIYO_CUSTOMER_ID", "12"),
        key="settings_PACKIYO_CUSTOMER_ID",
        help="Your Packiyo customer ID",
    )

    if getattr(config, "PACKIYO_API_TOKEN", ""):
        if st.button("Test Packiyo Connection"):
            from etl.packiyo_client import test_connection as test_packiyo
            ok, msg = test_packiyo()
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    st.divider()

    # --- Save ---
    col_save, col_oauth = st.columns(2)
    with col_save:
        if st.button("Save Credentials", type="primary"):
            all_vals = {**amazon_vals, **shopify_vals, **packiyo_vals}
            save_env(all_vals)
            st.success("Credentials saved!")
            st.rerun()

    with col_oauth:
        if st.button("Connect Shopify", type="secondary"):
            # Save current values first
            save_env(shopify_vals)
            from etl.shopify_oauth import get_access_token
            with st.spinner("Requesting access token from Shopify..."):
                token, error = get_access_token()
            if token:
                st.success("Shopify connected! Access token saved.")
                st.rerun()
            else:
                st.error(f"Connection failed: {error}")

    # --- Status ---
    st.divider()
    st.subheader("Connection Status")
    col1, col2, col3 = st.columns(3)
    with col1:
        has_amazon = all(getattr(config, k, "") for k in [
            "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
        ])
        if has_amazon:
            st.success("Amazon SP-API: Configured")
        else:
            missing = [k.replace("AMAZON_", "") for k in [
                "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
            ] if not getattr(config, k, "")]
            st.warning(f"Amazon SP-API: Missing {', '.join(missing)}")
    with col2:
        has_shopify = bool(getattr(config, "SHOPIFY_ACCESS_TOKEN", ""))
        if has_shopify:
            st.success("Shopify Admin API: Connected")
        else:
            has_creds = all(getattr(config, k, "") for k in [
                "SHOPIFY_STORE_URL", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
            ])
            if has_creds:
                st.warning("Shopify: Credentials saved, click **Authorize Shopify** to connect")
            else:
                st.warning("Shopify Admin API: Not configured")
    with col3:
        if getattr(config, "PACKIYO_API_TOKEN", ""):
            st.success("Packiyo 3PL: Configured")
        else:
            st.warning("Packiyo 3PL: Not configured")

    if config.USE_MOCK_DATA:
        st.info("Currently using **mock data**. Add API credentials above to switch to live data.")
    else:
        st.info("Currently using **live API data**.")

    # --- Backfill Full History ---
    st.divider()
    st.subheader("Data Management")

    with get_db() as conn:
        order_range = conn.execute(
            "SELECT MIN(order_date) as min_d, MAX(order_date) as max_d, COUNT(*) as cnt FROM orders"
        ).fetchone()
        cust_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]

    if order_range and order_range["cnt"] > 0:
        st.markdown(
            f"**Current data:** {order_range['cnt']:,} orders from "
            f"**{order_range['min_d'][:10]}** to **{order_range['max_d'][:10]}** "
            f"| {cust_count:,} customers"
        )
    else:
        st.markdown("**No order data loaded yet.**")

    st.caption(
        "Backfill pulls your full order history (up to 5 years) from Shopify using bulk export. "
        "This improves forecast accuracy by correctly identifying each customer's true first order date. "
        "Existing data is preserved — duplicates are handled automatically."
    )

    if st.button("Backfill Full Order History", type="secondary"):
        from etl.shopify_bulk_import import run_bulk_backfill

        backfill_progress = st.progress(0, text="Starting bulk export from Shopify...")
        backfill_status = st.empty()

        def _on_backfill_progress(imported, total):
            pct = min(imported / max(total, 1), 1.0)
            backfill_progress.progress(pct, text=f"Importing orders: {imported:,} / {total:,}")

        try:
            count = run_bulk_backfill(on_progress=_on_backfill_progress)
            backfill_progress.empty()
            backfill_status.success(f"Backfill complete! {count:,} orders imported.")
            clear_waterfall_cache()
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            backfill_progress.empty()
            backfill_status.error(f"Backfill failed: {e}")

    # --- Amazon Revenue CSV Upload ---
    st.divider()
    st.subheader("Amazon Order Import")
    st.caption(
        "Upload Amazon order-level data (from Seller Central or cohort exports). "
        "Supports both order-level CSVs (with Order ID, SKU, Item Price) and daily summary CSVs. "
        "Order data will populate sales forecasts, customer retention, and revenue tracking."
    )

    amazon_upload = st.file_uploader(
        "Upload Amazon Sales CSV/Excel", type=["csv", "xlsx", "xls"], key="amazon_rev_upload",
    )
    if amazon_upload is not None:
        try:
            if amazon_upload.name.endswith((".xlsx", ".xls")):
                amz_df = pd.read_excel(amazon_upload)
            else:
                amz_df = pd.read_csv(amazon_upload, low_memory=False)

            # Detect format: order-level (has Order Id) vs daily summary
            is_order_level = any("order id" in c.lower() for c in amz_df.columns)

            if is_order_level:
                # --- ORDER-LEVEL FORMAT (e.g. Amazon Cohort MASTER file) ---
                st.info(f"Detected **order-level** format: {len(amz_df):,} line items.")

                # Auto-detect columns
                _order_col = next((c for c in amz_df.columns if "amazon order id" in c.lower()), None)
                _sku_col = next((c for c in amz_df.columns if "merchant sku" in c.lower() or c.lower() == "sku"), None)
                _date_col = next((c for c in amz_df.columns if "purchase date" in c.lower()), None)
                _price_col = next((c for c in amz_df.columns if "item price" in c.lower()), None)
                _qty_col = next((c for c in amz_df.columns if "shipped quantity" in c.lower() or "quantity" in c.lower()), None)
                _title_col = next((c for c in amz_df.columns if "title" in c.lower()), None)
                _email_col = next((c for c in amz_df.columns if "buyer email" in c.lower()), None)

                if not all([_order_col, _sku_col, _date_col, _price_col, _qty_col]):
                    st.error("Could not detect required columns (Order ID, SKU, Date, Price, Quantity).")
                else:
                    # Preview
                    preview_cols = [c for c in [_date_col, _order_col, _sku_col, _qty_col, _price_col, _title_col] if c]
                    _render_df_as_html_global(amz_df[preview_cols].head(10))

                    _amz_unique_orders = amz_df[_order_col].nunique()
                    _amz_unique_skus = amz_df[_sku_col].nunique()
                    _amz_total_rev = amz_df[_price_col].sum()
                    ac1, ac2, ac3 = st.columns(3)
                    ac1.metric("Orders", f"{_amz_unique_orders:,}")
                    ac2.metric("Unique SKUs", f"{_amz_unique_skus}")
                    ac3.metric("Total Revenue", f"${_amz_total_rev:,.0f}")

                    if st.button("Import", key="import_amz_rev", type="primary"):
                        import hashlib
                        _amz_progress = st.progress(0, text="Importing Amazon orders...")
                        with get_db() as conn:
                            imported_orders = 0
                            imported_items = 0
                            imported_customers = 0
                            total_rows = len(amz_df)

                            for idx, (_, arow) in enumerate(amz_df.iterrows()):
                                try:
                                    _order_id = f"amz-{arow[_order_col]}"
                                    _sku = str(arow[_sku_col]).strip()
                                    _raw_date = str(arow[_date_col])
                                    _price = float(arow[_price_col]) if pd.notna(arow[_price_col]) else 0
                                    _qty = int(float(arow[_qty_col])) if pd.notna(arow[_qty_col]) else 1
                                    _title = str(arow[_title_col]) if _title_col and pd.notna(arow.get(_title_col)) else ""
                                    _email = str(arow[_email_col]) if _email_col and pd.notna(arow.get(_email_col)) else ""

                                    if not _sku or _qty == 0:
                                        continue

                                    # Parse date (handles ISO format and M/D/YYYY)
                                    _date_str = pd.Timestamp(_raw_date).strftime("%Y-%m-%d")

                                    # Customer (hash email for privacy)
                                    if _email:
                                        _cust_id = f"amz-{hashlib.md5(_email.encode()).hexdigest()[:12]}"
                                        upsert_customer(conn, _cust_id, _email, "amazon", _date_str)
                                        imported_customers += 1
                                    else:
                                        _cust_id = None

                                    # Order
                                    upsert_order(conn, _order_id, "amazon", str(arow[_order_col]),
                                                 _cust_id, _date_str, _price)
                                    imported_orders += 1

                                    # Order item
                                    _unit_price = _price / _qty if _qty > 0 else _price
                                    upsert_order_item(conn, _order_id, _sku, _title, _qty, _unit_price)
                                    imported_items += 1

                                    # SKU master
                                    upsert_sku(conn, _sku, _title, None, _date_str, "amazon")

                                except (ValueError, KeyError, TypeError):
                                    continue

                                if idx % 1000 == 0:
                                    _amz_progress.progress(
                                        min(idx / total_rows, 1.0),
                                        text=f"Importing: {idx:,} / {total_rows:,} rows..."
                                    )

                            # Aggregate into daily_sku_sales
                            _amz_progress.progress(0.95, text="Building daily sales aggregates...")

                            # Build Amazon daily sales from the imported order_items
                            conn.execute("DELETE FROM daily_sku_sales WHERE source = 'amazon'")
                            conn.execute("""
                                INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
                                SELECT
                                    DATE(o.order_date) as sale_date,
                                    oi.sku,
                                    'amazon' as source,
                                    SUM(oi.quantity) as units_sold,
                                    SUM(oi.total_price) as revenue,
                                    COUNT(DISTINCT o.order_id) as order_count
                                FROM order_items oi
                                JOIN orders o ON oi.order_id = o.order_id
                                WHERE o.source = 'amazon' AND o.status = 'completed'
                                GROUP BY DATE(o.order_date), oi.sku
                            """)

                        _amz_progress.progress(1.0, text="Done!")
                        st.success(
                            f"Imported **{imported_orders:,}** Amazon orders, "
                            f"**{imported_items:,}** line items, "
                            f"**{imported_customers:,}** customers."
                        )
                        clear_waterfall_cache()
                        st.cache_data.clear()
                        st.rerun()

            else:
                # --- DAILY SUMMARY FORMAT ---
                st.info(f"Detected **daily summary** format: {len(amz_df):,} rows.")
                _render_df_as_html_global(amz_df.head(10))

                _date_cols = [c for c in amz_df.columns if any(k in c.lower() for k in ["date", "day"])]
                _sku_cols = [c for c in amz_df.columns if any(k in c.lower() for k in ["sku", "asin", "child"])]
                _units_cols = [c for c in amz_df.columns if any(k in c.lower() for k in ["unit", "quantity", "qty"])]
                _rev_cols = [c for c in amz_df.columns if any(k in c.lower() for k in ["revenue", "sales", "amount", "ordered product sales"])]

                ac1, ac2, ac3, ac4 = st.columns(4)
                with ac1:
                    amz_date_col = st.selectbox("Date column", _date_cols if _date_cols else amz_df.columns.tolist(), key="amz_date_col")
                with ac2:
                    amz_sku_col = st.selectbox("SKU/ASIN column", _sku_cols if _sku_cols else amz_df.columns.tolist(), key="amz_sku_col")
                with ac3:
                    amz_units_col = st.selectbox("Units column", _units_cols if _units_cols else amz_df.columns.tolist(), key="amz_units_col")
                with ac4:
                    amz_rev_col = st.selectbox("Revenue column", _rev_cols if _rev_cols else amz_df.columns.tolist(), key="amz_rev_col")

                if st.button("Import", key="import_amz_rev", type="primary"):
                    with get_db() as conn:
                        imported_amz = 0
                        for _, arow in amz_df.iterrows():
                            try:
                                _d = str(arow[amz_date_col]).strip()[:10]
                                _sku = str(arow[amz_sku_col]).strip()
                                _units = int(float(str(arow[amz_units_col]).replace(",", "").replace("$", "")))
                                _rev = float(str(arow[amz_rev_col]).replace(",", "").replace("$", ""))
                                if not _sku or _units == 0:
                                    continue
                                conn.execute("""
                                    INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
                                    VALUES (?, ?, 'amazon', ?, ?, 1)
                                    ON CONFLICT(sale_date, sku, source) DO UPDATE SET
                                        units_sold = excluded.units_sold,
                                        revenue = excluded.revenue
                                """, (_d, _sku, _units, _rev))
                                imported_amz += 1
                            except (ValueError, KeyError):
                                continue
                    st.success(f"Imported {imported_amz} Amazon daily sales records.")
                    clear_waterfall_cache()
                    st.cache_data.clear()
                    st.rerun()
        except Exception as e:
            st.error(f"Failed to parse file: {e}")

    # --- Google Sheets Integration ---
    st.divider()
    st.subheader("Google Sheets")
    st.caption("Import data from a Google Sheet's 'Daily Data' tab. The sheet must be shared as 'Anyone with the link can view'.")

    with get_db() as conn:
        _gs_sheet_id = get_setting(conn, "google_sheet_id", "1GE_upVue9Fh4okDs7s3qE1ae54DStY-H0K7MovgCcxk")
        _gs_gid = get_setting(conn, "google_sheet_gid", "1661449750")
        _gs_last_sync = get_setting(conn, "google_sheet_last_sync", "Never")

    gs_sheet_id = st.text_input("Sheet ID", value=_gs_sheet_id, key="gs_sheet_id",
                                 help="The long ID in the Google Sheets URL between /d/ and /edit")
    gs_gid = st.text_input("Tab GID", value=_gs_gid, key="gs_gid",
                            help="The gid= parameter in the URL for the specific tab")
    st.caption(f"Last sync: {_gs_last_sync}")

    gs_c1, gs_c2 = st.columns(2)
    with gs_c1:
        if st.button("Test Connection", key="gs_test"):
            from etl.google_sheets import fetch_daily_data_tab
            df = fetch_daily_data_tab(gs_sheet_id, gs_gid)
            if df.empty:
                st.error("Could not fetch data. Make sure the sheet is shared as 'Anyone with the link can view'.")
            else:
                st.success(f"Connected! Found {len(df)} rows, {len(df.columns)} columns.")
                _render_df_as_html_global(df.head(5))
    with gs_c2:
        if st.button("Sync Now", key="gs_sync", type="primary"):
            from etl.google_sheets import sync_google_sheet, sync_amazon_rollup
            with get_db() as conn:
                set_setting(conn, "google_sheet_id", gs_sheet_id)
                set_setting(conn, "google_sheet_gid", gs_gid)
                result = sync_google_sheet(conn, gs_sheet_id, gs_gid)
                if result > 0:
                    set_setting(conn, "google_sheet_last_sync",
                                datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
                    st.success(f"Synced {result} rows from Google Sheet.")
                else:
                    st.error("Sync failed. Check that the sheet is publicly accessible.")
                # Also sync Amazon Roll Up Date tab
                amz_result = sync_amazon_rollup(conn)
                if amz_result > 0:
                    st.success(f"Synced {amz_result} rows from Amazon Roll Up Date.")
                else:
                    st.warning("Amazon Roll Up Date sync returned no data.")

    # --- Klaviyo ---
    st.divider()
    st.subheader("Klaviyo API")
    st.caption("Pull email campaign metrics and revenue attribution from Klaviyo.")

    with get_db() as conn:
        _klaviyo_key = get_setting(conn, "klaviyo_api_key", "")

    klaviyo_key = st.text_input("Klaviyo Private API Key", value=_klaviyo_key,
                                 type="password", key="klaviyo_key",
                                 help="From Klaviyo → Settings → API Keys → Create Private API Key")
    if st.button("Save", key="save_klaviyo"):
        with get_db() as conn:
            set_setting(conn, "klaviyo_api_key", klaviyo_key)
        st.success("Klaviyo API key saved.")

    if _klaviyo_key:
        st.info("🔌 Klaviyo connected. Campaign data sync coming soon.")
    else:
        st.caption("Enter your API key above to enable Klaviyo integration.")

    # --- Postscript ---
    st.divider()
    st.subheader("Postscript (SMS)")
    st.warning(
        "**Postscript's API does not expose campaign analytics or revenue attribution.** "
        "It only supports subscriber management. For SMS campaign performance data, "
        "export directly from the Postscript dashboard or track via Google Analytics UTM parameters."
    )

    # --- Bank / Ramp Transactions ---
    st.divider()
    st.subheader("Bank & Card Transactions")
    st.caption(
        "Import bank or credit card transaction data via CSV. "
        "Supports Ramp and Highbeam export formats. Internal transfers, CC statement payments, "
        "and Ramp-paid vendor bills are automatically flagged to prevent double-counting."
    )

    uploaded_csv = st.file_uploader("Upload Transaction CSV", type=["csv"], key="highbeam_csv")
    if uploaded_csv is not None:
        try:
            bank_df = pd.read_csv(uploaded_csv)
            st.success(f"Loaded {len(bank_df)} rows from CSV.")

            # Detect format
            is_ramp = "Merchant Name" in bank_df.columns or "Ramp Category" in bank_df.columns
            is_highbeam = "Direction" in bank_df.columns and "Summary" in bank_df.columns

            if is_ramp:
                st.info("Detected **Ramp** CSV format.")
                if "Type" in bank_df.columns:
                    transfers = bank_df["Type"].str.contains("Transfer", case=False, na=False)
                    st.caption(f"Filtering out {transfers.sum()} internal transfers.")
                    bank_df = bank_df[~transfers].copy()
                preview_cols = [c for c in ["Transaction Date", "Amount", "Merchant Name", "Ramp Category", "Ramp Department"] if c in bank_df.columns]
                _render_df_as_html_global(bank_df[preview_cols].head(10))

            elif is_highbeam:
                st.info("Detected **Highbeam** CSV format.")

                def _classify_hb(row):
                    s = str(row.get("Summary", "")).lower()
                    d = row.get("Direction", "")
                    if "internal transfer" in s:
                        return "Internal Transfer", True
                    if "highbeam" in s and "transfer" in s:
                        return "Internal Transfer", True
                    if "ramp statement" in s:
                        return "CC Payment (Ramp)", True  # Already itemized in Ramp data
                    if "via ramp" in s:
                        return "Vendor (via Ramp)", True  # Already itemized in Ramp data
                    if d == "Credit":
                        if any(k in s for k in ["shopify", "stripe", "paypal", "braintree"]):
                            return "Revenue Payout", False
                        if "amazon" in s:
                            return "Amazon Payout", False
                        return "Other Income", False
                    # Debits
                    if any(k in s for k in ["tax", "revenue", "dept", "treasury", "dor ", "dor|"]):
                        return "Sales Tax", False
                    if any(k in s for k in ["stikpak", "avenue shops"]):
                        return "Manufacturing / COGS", False
                    if any(k in s for k in ["unishippers", "ups", "fedex", "usps", "dhl"]):
                        return "Shipping", False
                    return "Operating Expense", False

                bank_df[["_hb_cat", "_hb_exclude"]] = bank_df.apply(
                    lambda r: pd.Series(_classify_hb(r)), axis=1
                )
                excluded = bank_df["_hb_exclude"].sum()
                st.caption(
                    f"Auto-classified: {excluded} rows flagged as non-cash-flow "
                    f"(internal transfers, Ramp CC payments, Ramp-paid vendor bills)."
                )
                _render_df_as_html_global(
                    bank_df[["Date", "Direction", "Amount", "Summary", "_hb_cat"]].head(15)
                )
            else:
                _render_df_as_html_global(bank_df.head(10))

            if st.button("Import", key="import_bank", type="primary"):
                with get_db() as conn:
                    conn.execute("DROP TABLE IF EXISTS bank_transactions")
                    conn.execute("""
                        CREATE TABLE bank_transactions (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            date TEXT NOT NULL,
                            description TEXT,
                            merchant TEXT,
                            amount REAL NOT NULL,
                            direction TEXT DEFAULT 'debit',
                            category TEXT DEFAULT 'Uncategorized',
                            department TEXT,
                            user_name TEXT,
                            is_transfer INTEGER DEFAULT 0,
                            is_ramp_duplicate INTEGER DEFAULT 0,
                            source TEXT DEFAULT 'unknown',
                            created_at TEXT DEFAULT CURRENT_TIMESTAMP
                        )
                    """)

                    imported = 0
                    for _, row in bank_df.iterrows():
                        try:
                            if is_ramp:
                                _date = str(row.get("Transaction Date", row.get("Clearing Date", "")))
                                _desc = str(row.get("Merchant Description", ""))
                                _merchant = str(row.get("Merchant Name", ""))
                                _amt = float(str(row.get("Amount", 0)).replace(",", "").replace("$", ""))
                                _cat = str(row.get("Ramp Category", row.get("Accounting Category", "Uncategorized")))
                                _dept = str(row.get("Ramp Department", ""))
                                _user = str(row.get("User", ""))
                                _is_transfer = 1 if "transfer" in str(row.get("Type", "")).lower() else 0
                                _direction = "debit"
                                _is_ramp_dup = 0
                                _source = "ramp"
                            elif is_highbeam:
                                _raw_date = str(row.get("Date", ""))
                                _date = pd.Timestamp(_raw_date).strftime("%Y-%m-%d") if _raw_date else ""
                                _desc = str(row.get("Summary", ""))
                                _merchant = _desc.split("|")[0].strip() if "|" in _desc else _desc[:40]
                                _amt = float(str(row.get("Amount", 0)).replace(",", "").replace("$", ""))
                                _direction = str(row.get("Direction", "")).lower()
                                _cat = row.get("_hb_cat", "Uncategorized")
                                _dept = ""
                                _user = ""
                                _is_transfer = 1 if _cat == "Internal Transfer" else 0
                                _is_ramp_dup = 1 if _cat in ("CC Payment (Ramp)", "Vendor (via Ramp)") else 0
                                _source = "highbeam"
                            else:
                                _date_col = next((c for c in bank_df.columns if "date" in c.lower()), bank_df.columns[0])
                                _desc_col = next((c for c in bank_df.columns if "desc" in c.lower() or "memo" in c.lower() or "summary" in c.lower()), bank_df.columns[min(1, len(bank_df.columns)-1)])
                                _amt_col = next((c for c in bank_df.columns if "amount" in c.lower()), bank_df.columns[min(2, len(bank_df.columns)-1)])
                                _date = str(row[_date_col])
                                _desc = str(row[_desc_col])
                                _merchant = _desc
                                _amt = float(str(row[_amt_col]).replace(",", "").replace("$", ""))
                                _direction = "debit"
                                _cat = "Uncategorized"
                                _dept = ""
                                _user = ""
                                _is_transfer = 0
                                _is_ramp_dup = 0
                                _source = "other"

                            conn.execute("""
                                INSERT INTO bank_transactions (date, description, merchant, amount, direction, category, department, user_name, is_transfer, is_ramp_duplicate, source)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (_date, _desc, _merchant, _amt, _direction, _cat, _dept, _user, _is_transfer, _is_ramp_dup, _source))
                            imported += 1
                        except (ValueError, KeyError):
                            continue

                st.success(f"Imported {imported} transactions.")
                st.rerun()
        except Exception as e:
            st.error(f"Failed to parse CSV: {e}")
