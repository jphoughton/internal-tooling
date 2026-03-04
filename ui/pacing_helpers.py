"""Shared pacing table builders, styling, and rendering.

Used by views/pacing.py and views/marketing.py to avoid duplicated
_build_pace_row / _style_pace_df / _render_white_table code.
"""
import streamlit as st
import pandas as pd


# ──────────────────────────────────────────────────────────────────
# Color helpers
# ──────────────────────────────────────────────────────────────────

def pace_color(pct, invert=False):
    """CSS for pacing cells. invert=True for spend (above pace = red)."""
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


def plus_minus_color(val, invert=False):
    """CSS for +/- column. Positive = green for rev, red for spend."""
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


# ──────────────────────────────────────────────────────────────────
# Pace row builder
# ──────────────────────────────────────────────────────────────────

def build_pace_row(label, mtd_actual, goal, l7d_avg, yd_actual,
                   pct_month, days_in_month, remaining_days,
                   is_spend=False, fmt='dollar'):
    """Build a single pacing row dict for the pacing table.

    Args:
        label: Row label (e.g. "NC Revenue", "Total Spend")
        mtd_actual: Month-to-date actual value
        goal: End-of-month goal
        l7d_avg: Last 7 day daily average
        yd_actual: Yesterday's actual value
        pct_month: Fraction of month elapsed (0-1)
        days_in_month: Total days in the month
        remaining_days: Days left in the month
        is_spend: True if this is a spend row (inverts color logic)
        fmt: 'dollar', 'count', or 'ratio'
    """
    should_be = goal * pct_month
    pacing = (mtd_actual / should_be) if should_be > 0 else 0
    plus_minus = mtd_actual - should_be
    remaining = goal - mtd_actual
    adjusted_daily = remaining / remaining_days if remaining_days > 0 else 0
    eom_pacing = (mtd_actual / pct_month) / goal if goal > 0 and pct_month > 0 else 0
    projection = mtd_actual / pct_month if pct_month > 0 else 0
    yd_goal = goal / days_in_month
    yd_pacing = (yd_actual / yd_goal) if yd_goal > 0 else 0

    if fmt == 'ratio':
        should_be = goal
        pacing = mtd_actual / goal if goal > 0 else 0
        plus_minus = mtd_actual - goal
        eom_pacing = pacing
        projection = mtd_actual
        remaining = goal - mtd_actual
        adjusted_daily = 0
        yd_pacing = yd_actual / goal if goal > 0 else 0

    if fmt == 'dollar':
        _fv = lambda v: f"${v:,.0f}"
        _fpm = lambda v: f"${v:+,.0f}"
    elif fmt == 'count':
        _fv = lambda v: f"{v:,.0f}"
        _fpm = lambda v: f"{v:+,.0f}"
    elif fmt == 'ratio':
        _fv = lambda v: f"{v:.2f}x"
        _fpm = lambda v: f"{v:+.2f}x"
    else:
        _fv = lambda v: f"${v:,.0f}"
        _fpm = lambda v: f"${v:+,.0f}"

    return {
        "_label": label,
        "_is_spend": is_spend,
        "_pacing_raw": pacing,
        "_plus_minus_raw": plus_minus,
        "_eom_pacing_raw": eom_pacing,
        "_yd_pacing_raw": yd_pacing,
        "": label,
        "Pacing": f"{pacing:.0%}",
        "MTD Actual": _fv(mtd_actual),
        "Should Be": _fv(should_be),
        "+/- Pacing": _fpm(plus_minus),
        "L7D Avg": _fv(l7d_avg),
        "Adj. Daily": "\u2014" if fmt == 'ratio' else _fv(adjusted_daily),
        "EOM Pacing": f"{eom_pacing:.0%}",
        "Remaining": "\u2014" if fmt == 'ratio' else _fv(remaining),
        "Projection": _fv(projection),
        "EOM Goal": _fv(goal),
        "Yest. Actual": _fv(yd_actual),
        "Yest. Goal": _fv(goal) if fmt == 'ratio' else _fv(yd_goal),
        "Yest. Pace": f"{yd_pacing:.0%}",
    }


# ──────────────────────────────────────────────────────────────────
# Styling + rendering
# ──────────────────────────────────────────────────────────────────

def style_pace_df(df_raw):
    """Apply red/yellow/green styling to a pacing DataFrame."""
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
        if "" in col_map:
            styles[col_map[""]] = "font-weight: 600; color: #0F3557"
        if "Pacing" in col_map:
            styles[col_map["Pacing"]] = pace_color(pacing_raw, invert=is_spend)
        if "+/- Pacing" in col_map:
            styles[col_map["+/- Pacing"]] = plus_minus_color(plus_raw, invert=is_spend)
        if "EOM Pacing" in col_map:
            styles[col_map["EOM Pacing"]] = pace_color(eom_raw, invert=is_spend)
        if "Yest. Pace" in col_map:
            styles[col_map["Yest. Pace"]] = pace_color(yd_raw, invert=is_spend)
        if "Projection" in col_map:
            styles[col_map["Projection"]] = pace_color(eom_raw, invert=is_spend)
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
                ("position", "sticky"),
                ("top", "0"),
                ("z-index", "1"),
            ]},
            {"selector": "td", "props": [
                ("border-bottom", "1px solid #F0F4F8"),
                ("padding", "9px 14px"),
            ]},
        ])
    )
    return styled


def render_white_table(styled_df):
    """Render a Pandas Styler as an HTML table with white bg."""
    html = styled_df.hide(axis="index").to_html()
    st.markdown(
        '<div style="background:#ffffff;border-radius:12px;overflow:hidden;'
        'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
        'width:100%;">'
        '<style>'
        '.pace-table table { width:100%; border-collapse:collapse; }'
        '.pace-table th, .pace-table td { white-space:nowrap; }'
        '.pace-table th { position:sticky; top:0; z-index:1; background-color:#F0F4F8 !important; }'
        '.pace-table tr:hover td:not([style*="background-color"]) { background:#F7FAFC !important; }'
        '.pace-table td[style*="color"] { -webkit-text-fill-color: unset; }'
        '</style>'
        f'<div class="pace-table" style="overflow-x:auto;max-height:600px;overflow-y:auto;">{html}</div></div>',
        unsafe_allow_html=True,
    )
