"""
Centralized Business Variables panel.

Defines all user-configurable planning variables (NOT API keys),
provides DB-backed get/save helpers, and renders a persistent
sidebar expander accessible from every page.

Three tabs:
  1. Variables — scalar planning parameters
  2. Media Spend — per-month DTC spend, ROAS, Amazon ad spend, Amazon revenue
  3. Orders — planned inbound per SKU per month
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
# Tab 1: Scalar variables
# ---------------------------------------------------------------------------
def _render_variables_tab():
    """Render scalar variable inputs. Returns dict of edits."""
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
# Tab 2: Media Spend
# ---------------------------------------------------------------------------
def _render_media_spend_tab():
    """Render per-month media spend + Amazon revenue editors. Returns True if saved."""
    current = get_business_vars()
    horizon = current.get("forecast_horizon", 12)
    now = datetime.utcnow()
    months = [(now + relativedelta(months=i)).strftime("%Y-%m") for i in range(horizon)]

    # --- Load existing data ---
    with get_db() as conn:
        dtc_rows = get_media_spend(conn, source="All Sources")
        amz_spend_rows = get_media_spend(conn, source="Amazon")
        amz_rev_rows = get_amazon_revenue_forecast(conn)

    dtc_lookup = {r["month"]: r for r in dtc_rows}
    amz_spend_lookup = {r["month"]: r["spend"] for r in amz_spend_rows}
    amz_rev_lookup = {r["month"]: r["revenue"] for r in amz_rev_rows}

    # --- DTC Ad Spend & ROAS ---
    st.markdown(_GROUP_HEADER.format("DTC Ad Spend & ROAS"), unsafe_allow_html=True)
    spend_edits = []
    for i, m in enumerate(months):
        existing = dtc_lookup.get(m, {"spend": 5000.0, "new_customer_roas": 2.0})
        try:
            dt = datetime.strptime(m, "%Y-%m")
            label = dt.strftime("%b '%y")
        except ValueError:
            label = m

        st.markdown(f'<span style="font-size:0.75rem;color:rgba(255,255,255,0.6);">{label}</span>',
                    unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            sv = st.number_input(
                "Spend $", value=float(existing["spend"]), min_value=0.0,
                step=500.0, format="%.0f", key=f"bvms_s_{i}", label_visibility="collapsed",
            )
        with c2:
            rv = st.number_input(
                "ROAS", value=float(existing["new_customer_roas"]), min_value=0.1,
                step=0.1, format="%.1f", key=f"bvms_r_{i}", label_visibility="collapsed",
            )
        spend_edits.append({"month": m, "spend": sv, "roas": rv})

    st.markdown('<div style="margin-bottom:8px;"></div>', unsafe_allow_html=True)

    # --- Amazon Ad Spend ---
    st.markdown(_GROUP_HEADER.format("Amazon Ad Spend"), unsafe_allow_html=True)
    amz_spend_edits = []
    for i, m in enumerate(months):
        try:
            dt = datetime.strptime(m, "%Y-%m")
            label = dt.strftime("%b '%y")
        except ValueError:
            label = m

        st.markdown(f'<span style="font-size:0.75rem;color:rgba(255,255,255,0.6);">{label}</span>',
                    unsafe_allow_html=True)
        av = st.number_input(
            "AMZ $", value=float(amz_spend_lookup.get(m, 0.0)), min_value=0.0,
            step=500.0, format="%.0f", key=f"bvms_a_{i}", label_visibility="collapsed",
        )
        amz_spend_edits.append({"month": m, "spend": av})

    st.markdown('<div style="margin-bottom:8px;"></div>', unsafe_allow_html=True)

    # --- Amazon Revenue Forecast ---
    st.markdown(_GROUP_HEADER.format("Amazon Revenue Forecast"), unsafe_allow_html=True)
    st.caption("$0 = use velocity-based projection")
    amz_rev_edits = []
    for i, m in enumerate(months):
        try:
            dt = datetime.strptime(m, "%Y-%m")
            label = dt.strftime("%b '%y")
        except ValueError:
            label = m

        st.markdown(f'<span style="font-size:0.75rem;color:rgba(255,255,255,0.6);">{label}</span>',
                    unsafe_allow_html=True)
        rr = st.number_input(
            "Rev $", value=float(amz_rev_lookup.get(m, 0.0)), min_value=0.0,
            step=5000.0, format="%.0f", key=f"bvms_ar_{i}", label_visibility="collapsed",
        )
        amz_rev_edits.append({"month": m, "revenue": rr})

    # --- Save ---
    if st.button("Apply Media Spend", type="primary", key="bv_media_apply",
                 use_container_width=True):
        with get_db() as conn:
            for row in spend_edits:
                upsert_media_spend(conn, row["month"], row["spend"], row["roas"], source="All Sources")
            for row in amz_spend_edits:
                upsert_media_spend(conn, row["month"], row["spend"], 0.0, source="Amazon")
            for row in amz_rev_edits:
                upsert_amazon_revenue_forecast(conn, row["month"], row["revenue"])
        return True
    return False


# ---------------------------------------------------------------------------
# Tab 3: Planned Inbound Orders
# ---------------------------------------------------------------------------
def _render_orders_tab(forecast_skus):
    """Render per-SKU per-month planned inbound editor. Returns True if saved."""
    from analytics.sku_flavors import get_flavor

    current = get_business_vars()
    horizon = current.get("forecast_horizon", 12)
    now = datetime.utcnow()
    months = [(now + relativedelta(months=i)).strftime("%Y-%m") for i in range(horizon)]

    with get_db() as conn:
        existing = get_planned_inbound_dict(conn)

    st.markdown(_GROUP_HEADER.format("Planned Inbound (units)"), unsafe_allow_html=True)
    st.caption("Units arriving per SKU per month.")

    # Build a DataFrame for st.data_editor
    rows = []
    for sku in sorted(forecast_skus):
        flavor = get_flavor(sku)
        row = {"SKU": sku, "Flavor": flavor}
        sku_data = existing.get(sku, {})
        for m in months:
            try:
                dt = datetime.strptime(m, "%Y-%m")
                col_label = dt.strftime("%b '%y")
            except ValueError:
                col_label = m
            row[col_label] = int(sku_data.get(m, 0))
        rows.append(row)

    df = pd.DataFrame(rows)

    # Build column config: SKU/Flavor read-only, month columns editable
    col_config = {
        "SKU": st.column_config.TextColumn("SKU", disabled=True, width="small"),
        "Flavor": st.column_config.TextColumn("Flavor", disabled=True, width="small"),
    }
    for m in months:
        try:
            dt = datetime.strptime(m, "%Y-%m")
            col_label = dt.strftime("%b '%y")
        except ValueError:
            col_label = m
        col_config[col_label] = st.column_config.NumberColumn(
            col_label, min_value=0, step=100, width="small",
        )

    edited_df = st.data_editor(
        df, column_config=col_config, hide_index=True,
        use_container_width=True, key="bv_inbound_editor",
    )

    if st.button("Apply Orders", type="primary", key="bv_orders_apply",
                 use_container_width=True):
        # Map column labels back to month strings
        label_to_month = {}
        for m in months:
            try:
                dt = datetime.strptime(m, "%Y-%m")
                label_to_month[dt.strftime("%b '%y")] = m
            except ValueError:
                label_to_month[m] = m

        with get_db() as conn:
            for _, row in edited_df.iterrows():
                sku = row["SKU"]
                for col, month_str in label_to_month.items():
                    units = int(row.get(col, 0) or 0)
                    upsert_planned_inbound(conn, sku, month_str, units)
        return True
    return False


# ---------------------------------------------------------------------------
# Main sidebar panel renderer
# ---------------------------------------------------------------------------
def render_sidebar_panel(forecast_skus=None):
    """Render the Business Variables expander in the sidebar.

    Returns True if the user saved changes (caller should clear caches & rerun).
    """
    with st.sidebar.expander("\u2699\uFE0F  Business Variables", expanded=False):
        tab_vars, tab_spend, tab_orders = st.tabs(["Variables", "Media Spend", "Orders"])

        with tab_vars:
            edits = _render_variables_tab()
            if st.button("Apply Changes", type="primary", key="bv_apply",
                         use_container_width=True):
                save_business_vars(edits)
                return True

        with tab_spend:
            if _render_media_spend_tab():
                return True

        with tab_orders:
            skus = forecast_skus or ()
            if skus and _render_orders_tab(skus):
                return True

    return False
