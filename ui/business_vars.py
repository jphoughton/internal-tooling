"""
Centralized Business Variables panel.

Defines all user-configurable planning variables (NOT API keys),
provides DB-backed get/save helpers, and renders a persistent
sidebar expander accessible from every page.

Layout:
  - Sidebar expander: scalar variables inline + summary cards with Edit buttons
  - Media Spend dialog: full-width st.data_editor (months x 4 columns)
  - Planned Inbound dialog: full-width st.data_editor (SKUs x months)
"""
import streamlit as st
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import (
    get_db, get_setting, set_setting, get_seasonal_indices,
    get_media_spend, upsert_media_spend,
    get_amazon_revenue_forecast, upsert_amazon_revenue_forecast,
    get_planned_inbound_dict, upsert_planned_inbound,
)

# ---------------------------------------------------------------------------
# Variable registry — single source of truth for names, defaults, and ranges
# ---------------------------------------------------------------------------
BUSINESS_VARS = {
    # --- Forecasting ---
    "bv.forecast_horizon": {
        "label": "Forecast Horizon",
        "group": "Forecasting",
        "type": "select",
        "options": [6, 12, 18],
        "default": 12,
        "unit": "months",
        "help": "How many months to project forward.",
    },
    "bv.amazon_growth_pct": {
        "label": "Amazon Monthly Growth",
        "group": "Forecasting",
        "type": "float",
        "min": -20.0, "max": 50.0, "step": 1.0,
        "default": 0.0,
        "unit": "%",
        "help": "Expected monthly growth rate for Amazon sales.",
    },
    # --- Supply Chain ---
    "bv.lead_time_weeks": {
        "label": "Lead Time",
        "group": "Supply Chain",
        "type": "int",
        "min": 1, "max": 52, "step": 1,
        "default": 12,
        "unit": "wks",
        "help": "Manufacturing + shipping lead time.",
    },
    "bv.moq_units": {
        "label": "MOQ per SKU",
        "group": "Supply Chain",
        "type": "int",
        "min": 100, "max": 100000, "step": 500,
        "default": 5000,
        "unit": "units",
        "help": "Minimum order quantity per production run.",
    },
    "bv.safety_buffer_weeks": {
        "label": "Safety Buffer",
        "group": "Supply Chain",
        "type": "int",
        "min": 0, "max": 12, "step": 1,
        "default": 2,
        "unit": "wks",
        "help": "Extra weeks of stock as safety buffer.",
    },
    "bv.fba_transfer_lt_weeks": {
        "label": "FBA Transfer Lead Time",
        "group": "Supply Chain",
        "type": "int",
        "min": 1, "max": 12, "step": 1,
        "default": 4,
        "unit": "wks",
        "help": "Weeks to ship from 3PL to Amazon FBA.",
    },
    # --- Marketing ---
    "bv.tw_adjustment_factor": {
        "label": "Triple Whale Adj.",
        "group": "Marketing",
        "type": "float",
        "min": 0.1, "max": 1.0, "step": 0.05,
        "default": 0.5,
        "unit": "x",
        "help": "TW over-reports ~2x; 0.5 halves to match actuals.",
    },
}

# Ordered groups for display
_GROUPS = ["Forecasting", "Supply Chain", "Marketing"]

_GROUP_HEADER = ('<p style="margin:0 0 4px;font-size:0.7rem;font-weight:700;'
                 'text-transform:uppercase;letter-spacing:0.06em;'
                 'color:rgba(255,255,255,0.55);">{}</p>')

_CARD_STYLE = (
    'background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.10);'
    'border-radius:8px;padding:10px 12px;margin-bottom:6px;'
)
_CARD_LABEL = 'font-size:0.68rem;text-transform:uppercase;letter-spacing:0.06em;color:rgba(255,255,255,0.5);margin:0;'
_CARD_VALUE = 'font-size:0.88rem;font-weight:600;color:#ffffff;margin:2px 0 0;'


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_business_vars():
    """Load all business variables from DB, returning typed defaults for unset keys."""
    with get_db() as conn:
        result = {}
        for key, spec in BUSINESS_VARS.items():
            raw = get_setting(conn, key, None)
            short_key = key.replace("bv.", "")
            if raw is None:
                result[short_key] = spec["default"]
            elif spec["type"] == "int":
                result[short_key] = int(float(raw))
            elif spec["type"] == "float":
                result[short_key] = float(raw)
            elif spec["type"] == "select":
                result[short_key] = type(spec["default"])(raw)
            else:
                result[short_key] = raw

        seas = get_setting(conn, "seasonality_enabled", "true")
        result["seasonality_enabled"] = seas == "true"

    return result


def save_business_vars(values: dict):
    """Batch-save a dict of short_key -> value to DB."""
    with get_db() as conn:
        for short_key, value in values.items():
            if short_key == "seasonality_enabled":
                set_setting(conn, "seasonality_enabled", "true" if value else "false")
            else:
                full_key = f"bv.{short_key}"
                set_setting(conn, full_key, str(value))


# ---------------------------------------------------------------------------
# Helpers: month list + formatters
# ---------------------------------------------------------------------------
def _month_list(horizon):
    now = datetime.utcnow()
    return [(now + relativedelta(months=i)).strftime("%Y-%m") for i in range(horizon)]


def _month_label(m):
    try:
        return datetime.strptime(m, "%Y-%m").strftime("%b '%y")
    except ValueError:
        return m


# ---------------------------------------------------------------------------
# Scalar variables (rendered inline in sidebar)
# ---------------------------------------------------------------------------
def _render_variables_inline():
    """Render scalar variable inputs inline in sidebar. Returns dict of edits."""
    current = get_business_vars()
    edits = {}

    for group in _GROUPS:
        st.markdown(_GROUP_HEADER.format(group), unsafe_allow_html=True)
        group_vars = [(k, s) for k, s in BUSINESS_VARS.items() if s["group"] == group]

        for full_key, spec in group_vars:
            short_key = full_key.replace("bv.", "")
            cur_val = current[short_key]
            label = f"{spec['label']} ({spec['unit']})"
            wkey = f"bvp_{short_key}"

            if spec["type"] == "select":
                options = spec["options"]
                idx = options.index(cur_val) if cur_val in options else 0
                val = st.selectbox(
                    label, options, index=idx, key=wkey,
                    format_func=lambda x, u=spec["unit"]: f"{x} {u}",
                )
            elif spec["type"] == "int":
                val = st.number_input(
                    label, min_value=spec["min"], max_value=spec["max"],
                    value=cur_val, step=spec["step"], key=wkey,
                )
            elif spec["type"] == "float":
                val = st.number_input(
                    label, min_value=spec["min"], max_value=spec["max"],
                    value=float(cur_val), step=spec["step"],
                    format="%.2f", key=wkey,
                )
            edits[short_key] = val

        st.markdown('<div style="margin-bottom:8px;"></div>', unsafe_allow_html=True)

    # Seasonality
    st.markdown(_GROUP_HEADER.format("Seasonality"), unsafe_allow_html=True)
    seas_val = st.toggle(
        "Enable Seasonality", value=current["seasonality_enabled"],
        key="bvp_seasonality_enabled",
    )
    edits["seasonality_enabled"] = seas_val
    try:
        with get_db() as conn:
            indices = get_seasonal_indices(conn)
        if indices:
            _mn = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            pk = max(indices, key=indices.get)
            lo = min(indices, key=indices.get)
            st.caption(f"Peak: {_mn[pk-1]} ({indices[pk]:.2f}) · Low: {_mn[lo-1]} ({indices[lo]:.2f})")
    except Exception:
        pass

    return edits


# ---------------------------------------------------------------------------
# Media Spend summary card (sidebar)
# ---------------------------------------------------------------------------
def _render_media_spend_summary():
    """Show a compact summary of media spend with an Edit button."""
    with get_db() as conn:
        dtc_rows = get_media_spend(conn, source="All Sources")
        amz_spend_rows = get_media_spend(conn, source="Amazon")

    total_dtc = sum(float(r.get("spend", 0) or 0) for r in dtc_rows)
    total_amz = sum(float(r.get("spend", 0) or 0) for r in amz_spend_rows)
    n_months = max(len(dtc_rows), len(amz_spend_rows), 1)

    st.markdown(_GROUP_HEADER.format("Media Spend Plan"), unsafe_allow_html=True)
    st.markdown(
        f'<div style="{_CARD_STYLE}">'
        f'<p style="{_CARD_LABEL}">DTC: ${total_dtc:,.0f} · AMZ: ${total_amz:,.0f}</p>'
        f'<p style="{_CARD_VALUE}">{n_months} months planned</p>'
        f'</div>',
        unsafe_allow_html=True,
    )
    return st.button("Edit Media Spend", key="bv_open_media_dialog",
                      use_container_width=True)


# ---------------------------------------------------------------------------
# Orders summary card (sidebar)
# ---------------------------------------------------------------------------
def _render_orders_summary(forecast_skus):
    """Show a compact summary of planned inbound with an Edit button."""
    with get_db() as conn:
        existing = get_planned_inbound_dict(conn)

    total_units = sum(
        sum(months.values()) for months in existing.values()
    )
    n_skus = len([s for s in existing if any(v > 0 for v in existing[s].values())])

    st.markdown(_GROUP_HEADER.format("Planned Inbound Orders"), unsafe_allow_html=True)
    st.markdown(
        f'<div style="{_CARD_STYLE}">'
        f'<p style="{_CARD_LABEL}">{n_skus} SKUs with orders planned</p>'
        f'<p style="{_CARD_VALUE}">{total_units:,.0f} total units</p>'
        f'</div>',
        unsafe_allow_html=True,
    )
    return st.button("Edit Inbound Orders", key="bv_open_orders_dialog",
                      use_container_width=True)


# ---------------------------------------------------------------------------
# Media Spend dialog
# ---------------------------------------------------------------------------
@st.dialog("Media Spend Plan", width="large")
def _media_spend_dialog():
    """Full-screen dialog with a single data_editor for all media spend data."""
    current = get_business_vars()
    horizon = current.get("forecast_horizon", 12)
    months = _month_list(horizon)

    with get_db() as conn:
        dtc_rows = get_media_spend(conn, source="All Sources")
        amz_spend_rows = get_media_spend(conn, source="Amazon")
        amz_rev_rows = get_amazon_revenue_forecast(conn)

    dtc_lookup = {r["month"]: r for r in dtc_rows}
    amz_spend_lookup = {r["month"]: r["spend"] for r in amz_spend_rows}
    amz_rev_lookup = {r["month"]: r["revenue"] for r in amz_rev_rows}

    # Build a single DataFrame: one row per month, 4 editable columns
    rows = []
    for m in months:
        existing = dtc_lookup.get(m, {"spend": 5000.0, "new_customer_roas": 2.0})
        rows.append({
            "Month": _month_label(m),
            "DTC Spend ($)": float(existing.get("spend", 5000.0)),
            "ROAS": float(existing.get("new_customer_roas", 2.0)),
            "AMZ Ad Spend ($)": float(amz_spend_lookup.get(m, 0.0)),
            "AMZ Revenue ($)": float(amz_rev_lookup.get(m, 0.0)),
        })

    df = pd.DataFrame(rows)

    st.caption("Edit monthly media spend across all channels. AMZ Revenue $0 = use velocity-based projection.")

    col_config = {
        "Month": st.column_config.TextColumn("Month", disabled=True, width="small"),
        "DTC Spend ($)": st.column_config.NumberColumn(
            "DTC Spend ($)", min_value=0.0, step=500.0, format="$%.0f",
        ),
        "ROAS": st.column_config.NumberColumn(
            "ROAS", min_value=0.1, step=0.1, format="%.1f", width="small",
        ),
        "AMZ Ad Spend ($)": st.column_config.NumberColumn(
            "AMZ Ad Spend ($)", min_value=0.0, step=500.0, format="$%.0f",
        ),
        "AMZ Revenue ($)": st.column_config.NumberColumn(
            "AMZ Revenue ($)", min_value=0.0, step=5000.0, format="$%.0f",
        ),
    }

    edited_df = st.data_editor(
        df, column_config=col_config, hide_index=True,
        use_container_width=True, key="bv_media_editor",
    )

    if st.button("Save Media Spend", type="primary", key="bv_media_save",
                  use_container_width=True):
        with get_db() as conn:
            for i, m in enumerate(months):
                row = edited_df.iloc[i]
                upsert_media_spend(conn, m, row["DTC Spend ($)"], row["ROAS"], source="All Sources")
                upsert_media_spend(conn, m, row["AMZ Ad Spend ($)"], 0.0, source="Amazon")
                upsert_amazon_revenue_forecast(conn, m, row["AMZ Revenue ($)"])
        st.session_state["_bv_media_saved"] = True
        st.rerun()


# ---------------------------------------------------------------------------
# Planned Inbound Orders dialog
# ---------------------------------------------------------------------------
@st.dialog("Planned Inbound Orders", width="large")
def _orders_dialog(forecast_skus):
    """Full-screen dialog with a data_editor for per-SKU per-month inbound."""
    from analytics.sku_flavors import get_flavor

    current = get_business_vars()
    horizon = current.get("forecast_horizon", 12)
    months = _month_list(horizon)

    with get_db() as conn:
        existing = get_planned_inbound_dict(conn)

    rows = []
    for sku in sorted(forecast_skus):
        flavor = get_flavor(sku)
        row = {"SKU": sku, "Flavor": flavor}
        sku_data = existing.get(sku, {})
        for m in months:
            row[_month_label(m)] = int(sku_data.get(m, 0))
        rows.append(row)

    df = pd.DataFrame(rows)

    st.caption("Units arriving per SKU per month.")

    col_config = {
        "SKU": st.column_config.TextColumn("SKU", disabled=True, width="small"),
        "Flavor": st.column_config.TextColumn("Flavor", disabled=True, width="small"),
    }
    for m in months:
        lbl = _month_label(m)
        col_config[lbl] = st.column_config.NumberColumn(
            lbl, min_value=0, step=100, width="small",
        )

    edited_df = st.data_editor(
        df, column_config=col_config, hide_index=True,
        use_container_width=True, key="bv_inbound_editor",
    )

    if st.button("Save Orders", type="primary", key="bv_orders_save",
                  use_container_width=True):
        label_to_month = {_month_label(m): m for m in months}
        with get_db() as conn:
            for _, row in edited_df.iterrows():
                sku = row["SKU"]
                for col, month_str in label_to_month.items():
                    units = int(row.get(col, 0) or 0)
                    upsert_planned_inbound(conn, sku, month_str, units)
        st.session_state["_bv_orders_saved"] = True
        st.rerun()


# ---------------------------------------------------------------------------
# Main sidebar panel renderer
# ---------------------------------------------------------------------------
def render_sidebar_panel(forecast_skus=None):
    """Render the Business Variables expander in the sidebar.

    Returns True if the user saved changes (caller should clear caches & rerun).
    """
    # Check for dialog save flags (set inside dialogs before rerun)
    if st.session_state.pop("_bv_media_saved", False):
        return True
    if st.session_state.pop("_bv_orders_saved", False):
        return True

    with st.sidebar.expander("\u2699\uFE0F  Business Variables", expanded=False):
        # --- Scalar variables inline ---
        edits = _render_variables_inline()

        if st.button("Apply Changes", type="primary", key="bv_apply",
                      use_container_width=True):
            save_business_vars(edits)
            return True

        st.markdown('<div style="margin-top:12px;"></div>', unsafe_allow_html=True)

        # --- Media Spend summary + edit button ---
        open_media = _render_media_spend_summary()

        # --- Orders summary + edit button ---
        skus = forecast_skus or ()
        open_orders = _render_orders_summary(skus) if skus else False

    # Open dialogs (must be outside the expander context)
    if open_media:
        _media_spend_dialog()

    if open_orders and skus:
        _orders_dialog(tuple(skus))

    return False
