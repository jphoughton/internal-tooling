"""DataFrame formatting and styling helpers for the Hydrant dashboard."""
import pandas as pd


# ---------------------------------------------------------------------------
# Column formatting helpers
# ---------------------------------------------------------------------------

def format_number_cols(df, cols):
    """Format specified columns as X,XXX."""
    for col in cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: f'{x:,.0f}' if pd.notna(x) and x != 0 else '\u2014')
    return df


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
