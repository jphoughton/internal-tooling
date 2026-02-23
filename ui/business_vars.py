"""
Centralized Business Variables panel.

Defines all user-configurable planning variables (NOT API keys),
provides DB-backed get/save helpers, and renders a persistent
sidebar expander accessible from every page.
"""
import streamlit as st
from db import get_db, get_setting, set_setting, get_seasonal_indices

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


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_business_vars():
    """Load all business variables from DB, returning typed defaults for unset keys.

    Returns dict like::

        {'forecast_horizon': 12, 'amazon_growth_pct': 0.0, ...}
    """
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

        # Seasonality toggle (already has its own key in app_settings)
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
# Sidebar panel renderer
# ---------------------------------------------------------------------------
def render_sidebar_panel():
    """Render the Business Variables expander in the sidebar.

    Returns True if the user saved changes (caller should clear caches & rerun).
    """
    current = get_business_vars()

    with st.sidebar.expander("\u2699\uFE0F  Business Variables", expanded=False):
        edits = {}

        for group in _GROUPS:
            st.markdown(f'<p style="margin:0 0 4px;font-size:0.7rem;font-weight:700;'
                        f'text-transform:uppercase;letter-spacing:0.06em;'
                        f'color:rgba(255,255,255,0.55);">{group}</p>',
                        unsafe_allow_html=True)

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

            # Small spacer between groups
            st.markdown('<div style="margin-bottom:8px;"></div>', unsafe_allow_html=True)

        # --- Seasonality toggle ---
        st.markdown('<p style="margin:0 0 4px;font-size:0.7rem;font-weight:700;'
                    'text-transform:uppercase;letter-spacing:0.06em;'
                    'color:rgba(255,255,255,0.55);">Seasonality</p>',
                    unsafe_allow_html=True)

        seas_val = st.toggle(
            "Enable Seasonality",
            value=current["seasonality_enabled"],
            key="bvp_seasonality_enabled",
        )
        edits["seasonality_enabled"] = seas_val

        # Show peak/low summary
        try:
            with get_db() as conn:
                indices = get_seasonal_indices(conn)
            if indices:
                _mnames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                peak_m = max(indices, key=indices.get)
                low_m = min(indices, key=indices.get)
                st.caption(
                    f"Peak: {_mnames[peak_m - 1]} ({indices[peak_m]:.2f}) · "
                    f"Low: {_mnames[low_m - 1]} ({indices[low_m]:.2f})"
                )
        except Exception:
            pass

        # --- Media Spend summary (read-only) ---
        st.markdown('<p style="margin:8px 0 4px;font-size:0.7rem;font-weight:700;'
                    'text-transform:uppercase;letter-spacing:0.06em;'
                    'color:rgba(255,255,255,0.55);">Media Spend</p>',
                    unsafe_allow_html=True)
        try:
            from db import get_media_spend
            with get_db() as conn:
                spend_rows = get_media_spend(conn, source="All Sources")
            if spend_rows:
                avg_spend = sum(r["spend"] for r in spend_rows) / len(spend_rows)
                avg_roas = sum(r.get("new_customer_roas", 0) for r in spend_rows) / len(spend_rows)
                st.caption(f"DTC: ${avg_spend:,.0f}/mo avg · ROAS: {avg_roas:.1f}x")
            else:
                st.caption("No media spend configured.")
        except Exception:
            st.caption("Media spend unavailable.")
        st.caption("Edit on **Demand Forecast** page.")

        # --- Save button ---
        if st.button("Apply Changes", type="primary", key="bv_apply",
                     use_container_width=True):
            save_business_vars(edits)
            return True

    return False
