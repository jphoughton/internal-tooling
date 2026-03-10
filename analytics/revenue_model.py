"""
Revenue Model computation engine.

Replicates the Hydrant 2026 Monthly Revenue Model spreadsheet.
Takes editable input variables and computes all derived P&L rows.
"""
import logging

log = logging.getLogger(__name__)

# ── Variable keys (match DB storage) ─────────────────────────────────────
# Editable input variable keys
EDITABLE_VARS = [
    'dtc_spend', 'amazon_spend',
    'dtc_nc_aov', 'dtc_nc_roas', 'amazon_nc_aov', 'nc_multiplier',
    'dtc_repeat', 'amazon_repeat', 'wholesale_rev',
    'dtc_net_to_gross', 'dtc_processing_pct', 'dtc_fulfillment_pct',
    'amazon_fulfillment_pct', 'cogs_pct',
    'fixed_expenses',
    'funnel_cpms', 'funnel_cost_per_visitor', 'funnel_conv_rate', 'funnel_aov',
]

# ── Default values for Jan–Dec 2026 ─────────────────────────────────────
_MONTHS_2026 = [f'2026-{m:02d}' for m in range(1, 13)]

DEFAULTS = {
    'dtc_spend':             [60000, 75000, 75000, 95000, 130000, 150000, 190000, 180000, 140000, 130000, 140000, 130000],
    'amazon_spend':          [12000, 22000, 22000, 22000, 22000, 22000, 22000, 22000, 22000, 22000, 22000, 22000],
    'dtc_nc_aov':            [70] * 12,
    'dtc_nc_roas':           [0.7, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6, 0.6],
    'amazon_nc_aov':         [45] * 12,
    'nc_multiplier':         [0.5, 1.53, 1.53, 1.53, 1.53, 1.53, 1.53, 1.53, 1.53, 1.53, 1.53, 1.53],
    'dtc_repeat':            [127709.19, 105767.82, 120471.24, 135735.48, 151302.95, 171311.70,
                              192403.08, 203268.37, 202776.19, 198156.39, 188260.17, 184183.20],
    'amazon_repeat':         [108000, 82620, 82620, 104652, 143208, 165240,
                              209304, 198288, 154224, 143208, 154224, 143208],
    'wholesale_rev':         [1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 60, 1000],
    'dtc_net_to_gross':      [1.3859589838] * 12,
    'dtc_processing_pct':    [0.04] * 12,
    'dtc_fulfillment_pct':   [0.18] * 12,
    'amazon_fulfillment_pct': [0.305] * 12,
    'cogs_pct':              [0.165] * 12,
    'fixed_expenses':        [61782] * 12,
    'funnel_cpms':           [54] * 12,
    'funnel_cost_per_visitor': [1.70] * 12,
    'funnel_conv_rate':      [0.0175] * 12,
    'funnel_aov':            [68] * 12,
}


def get_default(variable, month_index):
    """Return default value for a variable at a given month index (0-based)."""
    vals = DEFAULTS.get(variable, [0] * 12)
    if month_index < len(vals):
        return vals[month_index]
    return vals[-1] if vals else 0


def get_defaults_for_months(months):
    """Return full defaults dict for a list of month keys.

    Maps by calendar month (1-12) so Jan defaults always apply to Jan months,
    regardless of which year or what horizon range is used.
    """
    result = {}
    for var, vals in DEFAULTS.items():
        result[var] = {}
        for m in months:
            try:
                month_num = int(m.split('-')[1])  # "2026-03" → 3
                idx = month_num - 1  # 0-based index into defaults array
                result[var][m] = vals[idx] if idx < len(vals) else (vals[-1] if vals else 0)
            except (IndexError, ValueError):
                result[var][m] = vals[0] if vals else 0
    return result


def merge_with_defaults(db_data, months):
    """Merge DB-stored values with defaults for any missing months/variables.

    db_data: {variable: {month: value}} from get_revenue_model()
    months: list of month keys
    Returns: {variable: {month: value}} with all gaps filled from defaults.
    """
    defaults = get_defaults_for_months(months)
    merged = {}
    for var in EDITABLE_VARS:
        merged[var] = {}
        for m in months:
            if var in db_data and m in db_data[var]:
                merged[var][m] = db_data[var][m]
            elif var in defaults and m in defaults[var]:
                merged[var][m] = defaults[var][m]
            else:
                merged[var][m] = 0
    return merged


def _v(inputs, var, month):
    """Safely get a value from the inputs dict."""
    return inputs.get(var, {}).get(month, 0)


def compute(inputs, months):
    """Compute the full revenue model P&L.

    Args:
        inputs: {variable: {month: value}} — all editable inputs (merged with defaults)
        months: list of month keys in order

    Returns:
        dict with keys for each calculated row, each containing {month: value}.
        Also includes 'ttm_sales', 'ttm_ebitda', 'ttm_ebitda_pct' scalars.
    """
    calc = {}

    def _set(name, month, value):
        if name not in calc:
            calc[name] = {}
        calc[name][month] = value

    for m in months:
        dtc_spend = _v(inputs, 'dtc_spend', m)
        amz_spend = _v(inputs, 'amazon_spend', m)
        dtc_nc_roas = _v(inputs, 'dtc_nc_roas', m)
        dtc_nc_aov = _v(inputs, 'dtc_nc_aov', m)
        amz_nc_aov = _v(inputs, 'amazon_nc_aov', m)
        nc_mult = _v(inputs, 'nc_multiplier', m)
        dtc_repeat = _v(inputs, 'dtc_repeat', m)
        amz_repeat = _v(inputs, 'amazon_repeat', m)
        wholesale = _v(inputs, 'wholesale_rev', m)
        net_to_gross = _v(inputs, 'dtc_net_to_gross', m)
        proc_pct = _v(inputs, 'dtc_processing_pct', m)
        dtc_fulfill_pct = _v(inputs, 'dtc_fulfillment_pct', m)
        amz_fulfill_pct = _v(inputs, 'amazon_fulfillment_pct', m)
        cogs_pct = _v(inputs, 'cogs_pct', m)
        fixed_exp = _v(inputs, 'fixed_expenses', m)

        # ── Media Spend ──
        total_media = dtc_spend + amz_spend
        _set('total_media_spend', m, total_media)

        # ── New Customer Revenue ──
        dtc_new = dtc_nc_roas * dtc_spend
        amz_new = nc_mult * dtc_new  # Amazon = multiplier × DTC New Revenue
        total_new = dtc_new + amz_new
        _set('dtc_new', m, dtc_new)
        _set('amazon_new', m, amz_new)
        _set('total_new', m, total_new)

        # ── New Customer Orders ──
        dtc_nc_orders = dtc_new / dtc_nc_aov if dtc_nc_aov else 0
        dtc_nc_cpa = dtc_spend / dtc_nc_orders if dtc_nc_orders else 0
        amz_nc_orders = amz_new / amz_nc_aov if amz_nc_aov else 0
        total_nc = dtc_nc_orders + amz_nc_orders
        _set('dtc_nc_orders', m, dtc_nc_orders)
        _set('dtc_nc_cpa', m, dtc_nc_cpa)
        _set('amazon_nc_orders', m, amz_nc_orders)
        _set('total_nc', m, total_nc)

        # ── Blended Metrics ──
        blended_nc_roas = total_new / total_media if total_media else 0
        blended_cpa = total_media / total_nc if total_nc else 0
        _set('blended_nc_roas', m, blended_nc_roas)
        _set('blended_cpa', m, blended_cpa)

        # ── Repeat Revenue ──
        _set('amazon_repeat', m, amz_repeat)
        total_repeat = dtc_repeat + amz_repeat
        _set('total_repeat', m, total_repeat)

        # ── Revenue Summary ──
        dtc_rev = dtc_new + dtc_repeat
        dtc_gross = dtc_rev * net_to_gross
        amz_rev = amz_new + amz_repeat
        net_sales = dtc_rev + amz_rev + wholesale
        biz_mer = net_sales / total_media if total_media else 0
        _set('dtc_gross_sales', m, dtc_gross)
        _set('dtc_rev', m, dtc_rev)
        _set('amazon_rev', m, amz_rev)
        _set('net_sales', m, net_sales)
        _set('business_mer', m, biz_mer)

        # ── Cost Breakdown ──
        dtc_proc_amt = proc_pct * dtc_rev
        dtc_fulfill_amt = dtc_fulfill_pct * dtc_rev
        amz_fulfill_amt = amz_fulfill_pct * amz_rev
        amz_cogs_amt = cogs_pct * amz_rev
        dtc_cogs_amt = cogs_pct * dtc_gross
        total_cos = dtc_proc_amt + dtc_fulfill_amt + amz_fulfill_amt + amz_cogs_amt + dtc_cogs_amt
        _set('dtc_processing_amt', m, dtc_proc_amt)
        _set('dtc_fulfillment_amt', m, dtc_fulfill_amt)
        _set('amazon_fulfillment_amt', m, amz_fulfill_amt)
        _set('amazon_cogs_amt', m, amz_cogs_amt)
        _set('dtc_cogs_amt', m, dtc_cogs_amt)
        _set('total_cos', m, total_cos)

        # ── Profit ──
        gross_profit = net_sales - total_cos
        gross_profit_pct = gross_profit / net_sales if net_sales else 0
        total_expenses = fixed_exp + total_cos + total_media
        net_profit = net_sales - total_expenses
        net_profit_pct = net_profit / net_sales if net_sales else 0
        _set('gross_profit', m, gross_profit)
        _set('gross_profit_pct', m, gross_profit_pct)
        _set('total_expenses', m, total_expenses)
        _set('net_profit', m, net_profit)
        _set('net_profit_pct', m, net_profit_pct)

        # ── Funnel Goals ──
        f_cpms = _v(inputs, 'funnel_cpms', m)
        f_cpv = _v(inputs, 'funnel_cost_per_visitor', m)
        f_conv = _v(inputs, 'funnel_conv_rate', m)
        f_aov = _v(inputs, 'funnel_aov', m)
        f_new_visitors = dtc_spend / f_cpv if f_cpv else 0
        f_new_customers = f_new_visitors * f_conv
        f_nc_rev = f_new_customers * f_aov
        f_nc_roas = f_nc_rev / dtc_spend if dtc_spend else 0
        _set('funnel_new_visitors', m, f_new_visitors)
        _set('funnel_new_customers', m, f_new_customers)
        _set('funnel_nc_rev', m, f_nc_rev)
        _set('funnel_nc_roas', m, f_nc_roas)

    # ── TTM Totals ──
    ttm_sales = sum(calc.get('net_sales', {}).get(m, 0) for m in months)
    ttm_gross = sum(calc.get('dtc_gross_sales', {}).get(m, 0)
                    + calc.get('amazon_rev', {}).get(m, 0)
                    + _v(inputs, 'wholesale_rev', m)
                    for m in months)
    ttm_ebitda = sum(calc.get('net_profit', {}).get(m, 0) for m in months)
    ttm_ebitda_pct = ttm_ebitda / ttm_sales if ttm_sales else 0
    calc['_ttm_gross'] = ttm_gross
    calc['_ttm_sales'] = ttm_sales
    calc['_ttm_ebitda'] = ttm_ebitda
    calc['_ttm_ebitda_pct'] = ttm_ebitda_pct

    return calc


# ── Row definitions for rendering ────────────────────────────────────────
# Each tuple: (key, label, format_type, is_editable, section)
# format_type: '$' = currency, '$k' = currency (thousands), 'x' = ratio,
#              '%' = percentage (0.04 displayed as 4.0%), '#' = integer/count,
#              '%raw' = already a percentage (0.54 displayed as 54.3%)

SECTIONS = [
    {
        'name': 'Media Spend',
        'rows': [
            ('dtc_spend',      'DTC Spend',          '$',  True),
            ('amazon_spend',   'Amazon Spend',       '$',  True),
            ('total_media_spend', 'Total Media Spend', '$', False),
        ],
    },
    {
        'name': 'Unit Economics',
        'rows': [
            ('dtc_nc_aov',     'DTC NC-AOV',         '$s', True),
            ('dtc_nc_roas',    'DTC NC-ROAS',        'x',  True),
            ('amazon_nc_aov',  'Amazon NC-AOV',      '$s', True),
            ('nc_multiplier',  'DTC / Amazon NC Multiplier', 'x', True),
        ],
    },
    {
        'name': 'Blended Metrics',
        'rows': [
            ('blended_nc_roas', 'Blended NC-ROAS',   'x',  False),
            ('blended_cpa',    'Blended CPA',        '$s', False),
        ],
    },
    {
        'name': 'New Customer Revenue',
        'rows': [
            ('dtc_new',        'DTC New',            '$',  False),
            ('amazon_new',     'Amazon New',         '$',  False),
            ('total_new',      'Total New',          '$',  False),
            ('dtc_nc_orders',  'DTC NC Orders',      '#',  False),
            ('dtc_nc_cpa',     'DTC NC-CPA',         '$s', False),
            ('amazon_nc_orders', 'Amazon NC Orders',  '#', False),
            ('total_nc',       'Total NC',           '#',  False),
        ],
    },
    {
        'name': 'Repeat Revenue',
        'rows': [
            ('dtc_repeat',     'DTC Repeat',         '$',  True),
            ('amazon_repeat',  'Amazon Repeat',      '$',  True),
            ('total_repeat',   'Total Repeat',       '$',  False),
        ],
    },
    {
        'name': 'Revenue Summary',
        'rows': [
            ('dtc_gross_sales', 'DTC Gross Sales',   '$',  False),
            ('dtc_rev',        'DTC Rev (Total Sales)', '$', False),
            ('amazon_rev',     'Amazon Rev',         '$',  False),
            ('wholesale_rev',  'Wholesale Rev',      '$',  True),
            ('net_sales',      'Net Sales',          '$',  False),
            ('business_mer',   'Business MER',       'x',  False),
        ],
    },
    {
        'name': 'Cost Assumptions',
        'rows': [
            ('dtc_net_to_gross',      'DTC Net → Gross',        'x4', True),
            ('dtc_processing_pct',    'DTC Processing Fee',     '%',  True),
            ('dtc_fulfillment_pct',   'DTC Fulfillment % Total', '%', True),
            ('amazon_fulfillment_pct', 'Amazon Fulfillment %',  '%',  True),
            ('cogs_pct',              'COGS % Gross',           '%',  True),
        ],
    },
    {
        'name': 'Cost Breakdown',
        'rows': [
            ('dtc_processing_amt',    'DTC Processing Fee',     '$',  False),
            ('dtc_fulfillment_amt',   'DTC Fulfillment $',      '$',  False),
            ('amazon_fulfillment_amt', 'Amazon Fulfillment $',  '$',  False),
            ('amazon_cogs_amt',       'Amazon COGS $',          '$',  False),
            ('dtc_cogs_amt',          'DTC COGS $',             '$',  False),
            ('total_cos',             'Total Cost Of Sale',     '$',  False),
            ('gross_profit',          'Gross Profit',           '$',  False),
            ('gross_profit_pct',      'Gross Profit %',         '%raw', False),
        ],
    },
    {
        'name': 'P&L Bottom Line',
        'rows': [
            ('fixed_expenses',  'Fixed Expenses',     '$',    True),
            ('total_expenses',  'Total Expenses',     '$',    False),
            ('net_profit',      'Net Profit',         '$',    False),
            ('net_profit_pct',  'Net Profit %',       '%raw', False),
        ],
    },
]

FUNNEL_SECTION = {
    'name': 'Funnel Goals',
    'rows': [
        ('dtc_spend',               'Spend',              '$',  False),  # mirror
        ('funnel_cpms',             'CPMs',               '$s', True),
        ('funnel_cost_per_visitor', 'Cost Per New Visitor', '$s', True),
        ('funnel_new_visitors',     'New Visitors',       '#',  False),
        ('funnel_conv_rate',        'Conversion Rate',    '%',  True),
        ('funnel_new_customers',    'New Customers',      '#',  False),
        ('funnel_aov',              'AOV',                '$s', True),
        ('funnel_nc_rev',           'NC-Rev',             '$',  False),
        ('funnel_nc_roas',          'NC-ROAS',            'x',  False),
    ],
}

# Total rows that should be bold
BOLD_ROWS = {
    'total_media_spend', 'total_new', 'total_nc', 'total_repeat',
    'net_sales', 'total_cos', 'gross_profit', 'total_expenses',
    'net_profit',
}


def format_value(value, fmt):
    """Format a value for display based on format type."""
    if value is None:
        return ''
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)

    if fmt == '$':
        return f'${v:,.0f}'
    elif fmt == '$s':
        return f'${v:.2f}'
    elif fmt == 'x':
        return f'{v:.2f}'
    elif fmt == 'x4':
        return f'{v:.4f}'
    elif fmt == '%':
        return f'{v * 100:.1f}%'
    elif fmt == '%raw':
        return f'{v * 100:.1f}%'
    elif fmt == '#':
        return f'{v:,.0f}'
    else:
        return f'{v:,.2f}'
