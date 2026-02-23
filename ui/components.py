"""Reusable Streamlit UI components for the Hydrant dashboard."""
import os
import streamlit as st
from datetime import datetime, timedelta


def render_html_table(df, max_height=None):
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


def render_freshness_badge(last_refreshed_str=None, new_rows=None, is_live=False,
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


def smart_date_filter(data_min, data_max, key_prefix, show_presets=True, default_preset='MTD'):
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


def check_password():
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
