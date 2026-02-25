"""DataFrame formatting and styling helpers for the Hydrant dashboard."""
import secrets
import pandas as pd
import streamlit as st


# ---------------------------------------------------------------------------
# SKU display preparation
# ---------------------------------------------------------------------------

def prepare_sku_display_df(df, sku_col='SKU', flavor_col='Flavor', sort_by_seller=True):
    """Standard preparation: inject flavor names, sort by best-seller, return display-ready df."""
    from analytics.sku_flavors import get_flavor, sort_df_by_best_seller
    if flavor_col not in df.columns and sku_col in df.columns:
        idx = list(df.columns).index(sku_col) + 1
        df.insert(idx, flavor_col, df[sku_col].map(get_flavor))
    if sort_by_seller and sku_col in df.columns:
        df = sort_df_by_best_seller(df, sku_col=sku_col)
    return df


# ---------------------------------------------------------------------------
# Column formatting helpers
# ---------------------------------------------------------------------------

def format_currency_cols(df, cols):
    """Format specified columns as $X,XXX."""
    for col in cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f'${x:,.0f}' if pd.notna(x) and x != 0 else '\u2014')
    return df


def format_number_cols(df, cols):
    """Format specified columns as X,XXX."""
    for col in cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f'{x:,.0f}' if pd.notna(x) and x != 0 else '\u2014')
    return df


def format_pct_cols(df, cols):
    """Format specified columns as X.X%."""
    for col in cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f'{x:.1f}%' if pd.notna(x) else '\u2014')
    return df


# ---------------------------------------------------------------------------
# Gradient performance table (DoD / WoW / MoM)
# ---------------------------------------------------------------------------

# Default column-direction map: True = higher is good, False = lower is good.
# Callers can override with their own dict via the `col_direction` parameter.
DEFAULT_PERF_COL_DIRECTION = {
    # Roll Up columns
    'Spend': True,           # spending more = investing = good
    'Revenue': True,         # more revenue = good
    'Total Rev': True,
    'Total Revenue': True,
    'NC Rev': True,          # more NC revenue = good
    'NC Orders': True,       # more new customers = good
    'New Users': True,
    'NC MER': True,          # higher efficiency = good
    'MER': True,             # higher efficiency = good
    'NC ROAS': True,         # higher return = good
    'NC Conv %': True,       # higher conversion = good
    'NC AOV': True,          # higher AOV = good
    'NC CPA': False,         # lower CPA = good
    'Cost/New User': False,  # lower cost = good
}


def _parse_perf_num(val):
    """Extract numeric value from formatted string like '$1,234' or '1.50x' or '2.3%'."""
    if isinstance(val, (int, float)):
        return float(val)
    if not isinstance(val, str):
        return None
    s = val.replace('$', '').replace(',', '').replace('x', '').replace('%', '').strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def make_cohort_cell_style(vmin, vmax):
    """Return a style_fn for render_html_table that colors cohort cells.

    Maps absolute cell values to a teal gradient between vmin and vmax.
    Parses formatted strings ('$1,234' or '12.3%') back to float.
    """
    span = vmax - vmin if vmax != vmin else 1.0

    def _style(val):
        num = _parse_perf_num(val)
        if num is None:
            return ''
        t = max(0.0, min(1.0, (num - vmin) / span))
        # Teal gradient: transparent -> Hydrant navy with increasing opacity
        bg_alpha = 0.03 + 0.22 * t
        text_color = '#0F3557' if t < 0.65 else '#ffffff'
        weight = '500' if t < 0.65 else '600'
        return (
            f'background-color: rgba(15,53,87,{bg_alpha:.2f}); '
            f'color: {text_color}; font-weight: {weight}'
        )

    return _style


def gradient_perf_style(pct_change, higher_is_good):
    """Return CSS for a cell based on gradient intensity of change.

    Maps pct_change to a smooth red <-> yellow <-> green gradient:
      - Deep green   (+20%+ good change)
      - Light green  (+10% good change)
      - Yellow/amber (~0% or small change, within +/-3%)
      - Light red    (-10% bad change)
      - Deep red     (-20%+ bad change)
    """
    # Determine if the change is favorable
    signed = pct_change if higher_is_good else -pct_change

    # Clamp to +/-30% for gradient mapping
    t = max(-0.30, min(0.30, signed))

    if abs(t) < 0.03:
        # Neutral zone -- soft amber/yellow
        return 'color: #92700c; font-weight: 500; background-color: rgba(245,166,35,0.10)'

    if t > 0:
        # Positive (good) -- interpolate from light green to deep green
        # t ranges 0.03 -> 0.30, map to intensity 0.0 -> 1.0
        intensity = min(1.0, (t - 0.03) / 0.27)
        # Text color: light green #15803d -> deep green #065f26
        r = int(21 - 11 * intensity)      # 21 -> 6
        g = int(128 - 33 * intensity)     # 128 -> 95
        b = int(61 - 23 * intensity)      # 61 -> 38
        # Background opacity: 0.06 -> 0.18
        bg_alpha = 0.06 + 0.12 * intensity
        return (
            f'color: rgb({r},{g},{b}); font-weight: 600; '
            f'background-color: rgba(16,185,80,{bg_alpha:.2f})'
        )
    else:
        # Negative (bad) -- interpolate from light red to deep red
        intensity = min(1.0, (abs(t) - 0.03) / 0.27)
        # Text color: light red #dc2626 -> deep red #991b1b
        r = int(220 - 67 * intensity)     # 220 -> 153
        g = int(38 - 11 * intensity)      # 38 -> 27
        b = int(38 - 11 * intensity)      # 38 -> 27
        # Background opacity: 0.05 -> 0.16
        bg_alpha = 0.05 + 0.11 * intensity
        return (
            f'color: rgb({r},{g},{b}); font-weight: 600; '
            f'background-color: rgba(220,38,38,{bg_alpha:.2f})'
        )


def render_perf_table_colored(df, period_col, max_height=420,
                               col_direction=None, render_plain=None,
                               freeze_cols=1):
    """Render a performance table with gradient period-over-period color coding.

    Args:
        df: DataFrame with at least a period column and numeric metric columns.
        period_col: Name of the column that identifies each period (e.g. 'Day',
            'Week', 'Month').
        max_height: Maximum container height in pixels (scrollable).
        col_direction: Optional dict mapping column name -> bool (True = higher
            is good). Falls back to DEFAULT_PERF_COL_DIRECTION.
        render_plain: Optional callable(df, max_height) used when the DataFrame
            has fewer than 2 rows and gradient styling is not possible.  When
            *None* a simple st.dataframe fallback is used.
        freeze_cols: Number of left columns to freeze when scrolling
            horizontally (default 1 = freeze the period column).
    """
    if df.empty or len(df) < 2:
        if render_plain:
            render_plain(df, max_height=max_height)
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)
        return

    direction = col_direction or DEFAULT_PERF_COL_DIRECTION

    # Sort descending for display (most recent first)
    display_df = df.sort_values(period_col, ascending=False).reset_index(drop=True)
    numeric_cols = [c for c in display_df.columns if c != period_col]

    def _apply_display_styles(row):
        idx = row.name
        styles = [''] * len(row)
        col_map = {c: i for i, c in enumerate(display_df.columns)}

        # Bold period column
        if period_col in col_map:
            styles[col_map[period_col]] = 'font-weight: 600; color: #0F3557'

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
            higher_is_good = direction.get(col, True)

            styles[col_map[col]] = gradient_perf_style(pct_change, higher_is_good)

        return styles

    styled = (
        display_df.style
        .set_properties(**{
            'font-size': '0.84rem',
            'font-family': 'Visby CF, DM Sans, -apple-system, sans-serif',
            'color': '#1e2d3d',
            'background-color': '#ffffff',
        })
        .apply(_apply_display_styles, axis=1)
        .set_table_styles([
            {'selector': 'th', 'props': [
                ('background-color', '#F0F4F8'),
                ('color', '#0F3557'),
                ('font-weight', '600'),
                ('font-size', '0.74rem'),
                ('text-transform', 'uppercase'),
                ('letter-spacing', '0.05em'),
                ('border-bottom', '2px solid #D6DEE8'),
                ('padding', '11px 14px'),
                ('position', 'sticky'),
                ('top', '0'),
                ('z-index', '1'),
            ]},
            {'selector': 'td', 'props': [
                ('border-bottom', '1px solid #F0F4F8'),
                ('padding', '9px 14px'),
                ('white-space', 'nowrap'),
            ]},
        ])
        .hide(axis='index')
    )
    html = styled.to_html()
    height_style = f'max-height:{max_height}px;overflow-y:auto;' if max_height else ''

    # --- Frozen left columns ---
    tid = f'pt{secrets.token_hex(3)}'
    freeze_css = ''
    if freeze_cols and freeze_cols > 0:
        _COL_W = 130
        for ci in range(freeze_cols):
            left_px = ci * _COL_W
            freeze_css += (
                f'#{tid} th.col{ci}'
                f'{{ position:sticky !important; left:{left_px}px;'
                f' z-index:3 !important; min-width:{_COL_W}px;'
                f' background-color:#F0F4F8 !important; }}\n'
                f'#{tid} td.col{ci}'
                f'{{ position:sticky !important; left:{left_px}px;'
                f' z-index:2 !important; min-width:{_COL_W}px; }}\n'
            )
        last = freeze_cols - 1
        freeze_css += (
            f'#{tid} th.col{last},'
            f'#{tid} td.col{last}'
            f'{{ border-right:2px solid #D6DEE8 !important; }}\n'
        )

    st.markdown(
        f'<div style="background:#ffffff;border-radius:12px;'
        f'box-shadow:0 2px 12px rgba(15,53,87,0.08);border:1px solid #E8EDF3;'
        f'width:100%;overflow:hidden;">'
        f'<div id="{tid}" class="perf-table" style="overflow-x:auto;{height_style}">'
        '<style>'
        f'#{tid} table {{ width:100%; border-collapse:collapse; }}'
        f'#{tid} th, #{tid} td {{ white-space:nowrap; }}'
        f'#{tid} th {{ position:sticky; top:0; z-index:1; background-color:#F0F4F8 !important; }}'
        f'#{tid} tr:hover td:not([style*="background-color"]) {{ background:#F7FAFC !important; }}'
        f'{freeze_css}'
        '</style>'
        f'{html}</div></div>',
        unsafe_allow_html=True,
    )
