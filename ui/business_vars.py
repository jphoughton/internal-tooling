"""
Centralized Business Variables.

Defines all user-configurable planning variables (NOT API keys),
provides DB-backed get/save helpers, and month-list utilities
shared by the Variables page and other views.
"""
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import (
    get_db, get_setting, set_setting,
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
    # --- Cash Flow ---
    "bv.cogs_pct": {
        "label": "COGS %",
        "group": "Cash Flow",
        "type": "float",
        "min": 5.0, "max": 60.0, "step": 1.0,
        "default": 25.0,
        "unit": "%",
        "help": "Cost of goods as % of revenue (auto-computed from actuals, overridable).",
    },
    "bv.dtc_payout_ratio": {
        "label": "DTC Payout Ratio",
        "group": "Cash Flow",
        "type": "float",
        "min": 0.50, "max": 1.00, "step": 0.01,
        "default": 0.94,
        "unit": "ratio",
        "help": "DTC Total Sales to Cash ratio (~94%, auto-calibrated from bank actuals).",
    },
    "bv.amazon_payout_ratio": {
        "label": "Amazon Payout Ratio",
        "group": "Cash Flow",
        "type": "float",
        "min": 0.30, "max": 0.90, "step": 0.01,
        "default": 0.62,
        "unit": "ratio",
        "help": "Amazon Revenue to Cash ratio (~62%, auto-calibrated from bank actuals).",
    },
    "bv.min_cash_threshold": {
        "label": "Min Cash Target",
        "group": "Cash Flow",
        "type": "int",
        "min": 0, "max": 1000000, "step": 10000,
        "default": 100000,
        "unit": "$",
        "help": "Alert threshold for projected cash balance.",
    },
    "bv.loc_balance": {
        "label": "Line of Credit",
        "group": "Cash Flow",
        "type": "int",
        "min": 0, "max": 2000000, "step": 10000,
        "default": 510000,
        "unit": "$",
        "help": "Remaining line of credit balance (declining).",
    },
}

# Ordered groups for display
_GROUPS = ["Forecasting", "Supply Chain", "Cash Flow"]


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


_CASHFLOW_SETTINGS_KEYS = {
    'cogs_pct', 'dtc_payout_ratio', 'amazon_payout_ratio',
    'min_cash_threshold', 'loc_balance',
}


def save_business_vars(values: dict):
    """Batch-save a dict of short_key -> value to DB.

    Cash Flow variables are also synced to the cashflow_settings table
    so the projection engine picks up user overrides immediately.
    """
    with get_db() as conn:
        for short_key, value in values.items():
            if short_key == "seasonality_enabled":
                set_setting(conn, "seasonality_enabled", "true" if value else "false")
            else:
                full_key = f"bv.{short_key}"
                set_setting(conn, full_key, str(value))

            # Sync Cash Flow vars to cashflow_settings table
            if short_key in _CASHFLOW_SETTINGS_KEYS:
                try:
                    from db import set_cashflow_setting
                    # Convert percentage display values back to decimals
                    cf_value = str(value)
                    if short_key == 'cogs_pct':
                        cf_value = str(float(value) / 100)
                    set_cashflow_setting(conn, short_key, cf_value)
                except Exception:
                    pass  # cashflow_settings table may not exist yet


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
