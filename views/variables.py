"""Business Variables page — planning variables, revenue model, and inbound orders."""
import logging

import pandas as pd
import streamlit as st
from db import (
    get_db, get_setting, get_seasonal_indices,
    get_media_spend, upsert_media_spend,
    get_amazon_revenue_forecast, upsert_amazon_revenue_forecast,
    get_planned_inbound_dict, upsert_planned_inbound,
    get_revenue_model, save_revenue_model_bulk,
)
from ui.business_vars import (
    BUSINESS_VARS, _GROUPS,
    get_business_vars, save_business_vars,
    _month_list, _month_label,
)
from analytics.sku_flavors import get_flavor
from analytics.waterfall import clear_waterfall_cache
from analytics import revenue_model as rm

log = logging.getLogger(__name__)


def render(ctx):
    """Render the Business Variables page."""
    st.title('Business Variables')

    forecast_skus = ctx.get('forecast_skus', set())

    # Check for save flags
    for flag in ('_bv_page_media_saved', '_bv_page_orders_saved', '_bv_revmodel_saved'):
        if st.session_state.pop(flag, False):
            clear_waterfall_cache()
            st.cache_data.clear()
            st.rerun()

    current = get_business_vars()

    # ── Scalar Variables ──
    _render_scalar_variables(current)

    # ── Revenue Model (replaces old Media Spend Plan) ──
    st.divider()
    _render_revenue_model(current, ctx)

    # ── Planned Inbound Orders ──
    if forecast_skus:
        st.divider()
        _render_planned_inbound(current, forecast_skus)


# =====================================================================
# Scalar Variables
# =====================================================================
def _render_scalar_variables(current):
    st.subheader('Planning Variables')
    edits = {}
    for group in _GROUPS:
        st.markdown(f'**{group}**')
        group_vars = [(k, s) for k, s in BUSINESS_VARS.items() if s['group'] == group]
        cols = st.columns(len(group_vars))
        for i, (full_key, spec) in enumerate(group_vars):
            short_key = full_key.replace('bv.', '')
            cur_val = current[short_key]
            label = f"{spec['label']} ({spec['unit']})"
            wkey = f'bvpage_{short_key}'
            with cols[i]:
                if spec['type'] == 'select':
                    options = spec['options']
                    idx = options.index(cur_val) if cur_val in options else 0
                    val = st.selectbox(
                        label, options, index=idx, key=wkey,
                        format_func=lambda x, u=spec['unit']: f'{x} {u}',
                        help=spec.get('help', ''),
                    )
                elif spec['type'] == 'int':
                    val = st.number_input(
                        label, min_value=spec['min'], max_value=spec['max'],
                        value=cur_val, step=spec['step'], key=wkey,
                        help=spec.get('help', ''),
                    )
                elif spec['type'] == 'float':
                    val = st.number_input(
                        label, min_value=spec['min'], max_value=spec['max'],
                        value=float(cur_val), step=spec['step'],
                        format='%.2f', key=wkey,
                        help=spec.get('help', ''),
                    )
                edits[short_key] = val

    # Seasonality toggle
    st.markdown('**Seasonality**')
    _seas_col, _seas_info_col = st.columns([1, 3])
    with _seas_col:
        seas_val = st.toggle(
            'Enable Seasonality', value=current['seasonality_enabled'],
            key='bvpage_seasonality_enabled',
        )
        edits['seasonality_enabled'] = seas_val
    with _seas_info_col:
        try:
            with get_db() as conn:
                indices = get_seasonal_indices(conn)
            if indices:
                _mn = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
                pk = max(indices, key=indices.get)
                lo = min(indices, key=indices.get)
                st.caption(f'Peak: {_mn[pk-1]} ({indices[pk]:.2f}) '
                           f'\u00b7 Low: {_mn[lo-1]} ({indices[lo]:.2f})')
        except Exception:
            pass

    if st.button('Save Variables', type='primary', key='bvpage_apply'):
        save_business_vars(edits)
        clear_waterfall_cache()
        st.cache_data.clear()
        st.success('Variables saved!')
        st.rerun()


# =====================================================================
# Revenue Model
# =====================================================================
def _inject_waterfall_repeat(live, months, ctx):
    """Replace repeat revenue with waterfall-computed cohort projections."""
    import json as _json_wf
    try:
        cached_waterfall = ctx.get('cached_waterfall')
        load_seasonal_json = ctx.get('load_seasonal_json')
        if not cached_waterfall or not load_seasonal_json:
            return

        with get_db() as conn:
            media_spend = get_media_spend(conn, source='All Sources')
        if not media_spend:
            return

        spend_json = _json_wf.dumps(media_spend, sort_keys=True)
        seasonal_json = load_seasonal_json()

        # DTC repeat from shopify waterfall
        wf_dtc = cached_waterfall(spend_json, 'shopify', 12, seasonal_json)
        if wf_dtc is not None and not wf_dtc.empty:
            for _, row in wf_dtc.iterrows():
                m = str(row.get('month', ''))
                if m in months:
                    live['dtc_repeat'][m] = float(row.get('repeat_revenue', 0))

        # Amazon repeat from amazon waterfall
        wf_amz = cached_waterfall(spend_json, 'amazon', 12, seasonal_json)
        if wf_amz is not None and not wf_amz.empty:
            for _, row in wf_amz.iterrows():
                m = str(row.get('month', ''))
                if m in months:
                    live['amazon_repeat'][m] = float(row.get('repeat_revenue', 0))
    except Exception as e:
        log.warning('_inject_waterfall_repeat failed: %s', e)


def _render_revenue_model(current, ctx):
    st.subheader('Revenue Model')
    st.caption('One unified P&L view. Green rows are editable inputs — '
               'expand **Edit Inputs** below the table to change them.')

    horizon = current.get('forecast_horizon', 12)
    months = _month_list(horizon)
    ml = [_month_label(m) for m in months]

    # Load from DB + merge with defaults
    with get_db() as conn:
        db_data = get_revenue_model(conn)
    inputs = rm.merge_with_defaults(db_data, months)
    live = {var: dict(inputs.get(var, {})) for var in rm.EDITABLE_VARS}

    # ── Edit Inputs (expander, processed first so edits apply before display) ──
    with st.expander('Edit Inputs', expanded=False):
        st.caption('Edit values below and click **Save Revenue Model** to persist.')

        st.markdown('**Media Spend**')
        spend_df = _build_df({
            'DTC Spend': 'dtc_spend', 'Amazon Spend': 'amazon_spend',
        }, live, months, ml)
        edited_spend = st.data_editor(
            spend_df, key='rm_spend', use_container_width=True, height=110,
            column_config={c: st.column_config.NumberColumn(c, format='$%,.0f', step=1000) for c in ml},
        )
        _apply_edits(live, edited_spend, months, ml, {
            'DTC Spend': 'dtc_spend', 'Amazon Spend': 'amazon_spend',
        })

        st.markdown('**Unit Economics**')
        econ_df = _build_df({
            'DTC NC-AOV': 'dtc_nc_aov', 'DTC NC-ROAS': 'dtc_nc_roas',
            'Amazon NC-AOV': 'amazon_nc_aov', 'NC Multiplier': 'nc_multiplier',
        }, live, months, ml)
        edited_econ = st.data_editor(
            econ_df, key='rm_econ', use_container_width=True, height=180,
            column_config={c: st.column_config.NumberColumn(c, format='%.2f', step=0.01) for c in ml},
        )
        _apply_edits(live, edited_econ, months, ml, {
            'DTC NC-AOV': 'dtc_nc_aov', 'DTC NC-ROAS': 'dtc_nc_roas',
            'Amazon NC-AOV': 'amazon_nc_aov', 'NC Multiplier': 'nc_multiplier',
        })

        st.markdown('**Cost Assumptions**')
        cost_data = {}
        cost_data['DTC Net \u2192 Gross'] = [live['dtc_net_to_gross'][m] for m in months]
        cost_data['DTC Processing Fee (%)'] = [live['dtc_processing_pct'][m] * 100 for m in months]
        cost_data['DTC Fulfillment (%)'] = [live['dtc_fulfillment_pct'][m] * 100 for m in months]
        cost_data['Amazon Fulfillment (%)'] = [live['amazon_fulfillment_pct'][m] * 100 for m in months]
        cost_data['COGS % Gross'] = [live['cogs_pct'][m] * 100 for m in months]
        cost_df = pd.DataFrame(cost_data, index=ml).T
        edited_cost = st.data_editor(
            cost_df, key='rm_cost', use_container_width=True, height=215,
            column_config={c: st.column_config.NumberColumn(c, format='%.2f', step=0.1) for c in ml},
        )
        _apply_edits_pct(live, edited_cost, months, ml, {
            'DTC Net \u2192 Gross': ('dtc_net_to_gross', False),
            'DTC Processing Fee (%)': ('dtc_processing_pct', True),
            'DTC Fulfillment (%)': ('dtc_fulfillment_pct', True),
            'Amazon Fulfillment (%)': ('amazon_fulfillment_pct', True),
            'COGS % Gross': ('cogs_pct', True),
        })

        st.markdown('**Other Inputs**')
        other_df = _build_df({
            'Wholesale Rev': 'wholesale_rev', 'Fixed Expenses': 'fixed_expenses',
        }, live, months, ml)
        edited_other = st.data_editor(
            other_df, key='rm_other', use_container_width=True, height=110,
            column_config={c: st.column_config.NumberColumn(c, format='$%,.0f', step=1000) for c in ml},
        )
        _apply_edits(live, edited_other, months, ml, {
            'Wholesale Rev': 'wholesale_rev', 'Fixed Expenses': 'fixed_expenses',
        })

        # Save button inside expander
        if st.button('Save Revenue Model', type='primary',
                      use_container_width=True, key='rm_save'):
            _save_revenue_model(live, months)
            st.session_state['_bv_revmodel_saved'] = True
            st.rerun()

    # ── Inject waterfall repeat revenue (overrides manual values) ──
    _inject_waterfall_repeat(live, months, ctx)

    # ── Compute full model ──
    calc = rm.compute(live, months)

    # ── Render unified P&L table ──
    _render_full_pnl(live, calc, months, ml)

    # ── TTM Summary ──
    _render_ttm(calc)

    # ── Funnel Goals (separate expander) ──
    with st.expander('Funnel Goals', expanded=False):
        funnel_data = {}
        funnel_data['CPMs ($)'] = [live['funnel_cpms'][m] for m in months]
        funnel_data['Cost Per New Visitor ($)'] = [live['funnel_cost_per_visitor'][m] for m in months]
        funnel_data['Conversion Rate (%)'] = [live['funnel_conv_rate'][m] * 100 for m in months]
        funnel_data['AOV ($)'] = [live['funnel_aov'][m] for m in months]
        funnel_df = pd.DataFrame(funnel_data, index=ml).T
        edited_funnel = st.data_editor(
            funnel_df, key='rm_funnel', use_container_width=True, height=180,
            column_config={c: st.column_config.NumberColumn(c, format='%.2f', step=0.1) for c in ml},
        )
        _apply_edits_pct(live, edited_funnel, months, ml, {
            'CPMs ($)': ('funnel_cpms', False),
            'Cost Per New Visitor ($)': ('funnel_cost_per_visitor', False),
            'Conversion Rate (%)': ('funnel_conv_rate', True),
            'AOV ($)': ('funnel_aov', False),
        })
        funnel_calc = rm.compute(live, months)
        _render_calc_rows(funnel_calc, months, ml, [
            ('funnel_new_visitors', 'New Visitors', '#', False),
            ('funnel_new_customers', 'New Customers', '#', False),
            ('funnel_nc_rev', 'NC-Rev', '$', False),
            ('funnel_nc_roas', 'NC-ROAS', 'x', False),
        ])


# =====================================================================
# Planned Inbound Orders (unchanged)
# =====================================================================
def _render_planned_inbound(current, forecast_skus):
    st.subheader('Planned Inbound Orders')
    st.caption('Units arriving per SKU per month. Edit one quarter at a time.')

    horizon = current.get('forecast_horizon', 12)
    months = _month_list(horizon)

    with get_db() as conn:
        existing_inbound = get_planned_inbound_dict(conn)

    quarters = []
    for qi in range(0, len(months), 3):
        chunk = months[qi:qi + 3]
        q_label = f"{_month_label(chunk[0])} \u2013 {_month_label(chunk[-1])}"
        quarters.append((q_label, chunk))

    q_labels = [q[0] for q in quarters]
    q_tabs = st.tabs(q_labels)
    sorted_skus = sorted(forecast_skus)

    for q_idx, (q_label, q_months) in enumerate(quarters):
        with q_tabs[q_idx]:
            n_cols = len(q_months)
            widths = [2.0] + [1.0] * n_cols

            hdr = st.columns(widths)
            hdr[0].markdown('**Flavor**')
            for j, m in enumerate(q_months):
                hdr[j + 1].markdown(f'**{_month_label(m)}**')

            with st.form(f'bvpage_inbound_q{q_idx}_form'):
                q_results = {}
                for i, sku in enumerate(sorted_skus):
                    flavor = get_flavor(sku)
                    sku_data = existing_inbound.get(sku, {})
                    cols = st.columns(widths)
                    cols[0].markdown(
                        f'<div style="padding:6px 0;font-size:0.8rem;font-weight:600;">'
                        f'{flavor}</div>',
                        unsafe_allow_html=True,
                    )
                    sku_vals = {}
                    for j, m in enumerate(q_months):
                        val = cols[j + 1].number_input(
                            f'{sku}_{m}', value=int(sku_data.get(m, 0)),
                            min_value=0, step=100,
                            key=f'bvpg_io_{q_idx}_{i}_{j}', label_visibility='collapsed',
                        )
                        sku_vals[m] = val
                    q_results[sku] = sku_vals

                if st.form_submit_button(f'Save {q_label}', type='primary',
                                          use_container_width=True):
                    with get_db() as conn:
                        for sku, month_vals in q_results.items():
                            for month_str, units in month_vals.items():
                                upsert_planned_inbound(conn, sku, month_str, int(units))
                    st.session_state['_bv_page_orders_saved'] = True
                    st.rerun()


# =====================================================================
# Revenue Model — helper functions
# =====================================================================

def _build_df(label_to_var, live, months, month_labels):
    """Build a DataFrame for st.data_editor. Rows = labels, columns = months."""
    data = {}
    for label, var_key in label_to_var.items():
        data[label] = [live[var_key].get(m, 0) for m in months]
    return pd.DataFrame(data, index=month_labels).T


def _apply_edits(live, edited_df, months, month_labels, label_to_var):
    """Apply edits from a data_editor return value back to the live inputs dict."""
    for label, var_key in label_to_var.items():
        if label not in edited_df.index:
            continue
        for i, ml_val in enumerate(month_labels):
            if ml_val in edited_df.columns and i < len(months):
                val = edited_df.at[label, ml_val]
                if pd.notna(val):
                    live[var_key][months[i]] = float(val)


def _apply_edits_pct(live, edited_df, months, month_labels, label_to_var_pct):
    """Apply edits, converting display-percentages (4.0) back to decimals (0.04)."""
    for label, (var_key, is_pct) in label_to_var_pct.items():
        if label not in edited_df.index:
            continue
        for i, ml_val in enumerate(month_labels):
            if ml_val in edited_df.columns and i < len(months):
                val = edited_df.at[label, ml_val]
                if pd.notna(val):
                    if is_pct:
                        live[var_key][months[i]] = float(val) / 100.0
                    else:
                        live[var_key][months[i]] = float(val)


def _save_revenue_model(live, months):
    """Persist inputs to DB + sync legacy tables for backward compatibility."""
    with get_db() as conn:
        save_revenue_model_bulk(conn, live)

        # Sync to media_spend + amazon_revenue_forecast for waterfall/cashflow
        for m in months:
            dtc_spend = live.get('dtc_spend', {}).get(m, 0)
            dtc_roas = live.get('dtc_nc_roas', {}).get(m, 0)
            amz_spend = live.get('amazon_spend', {}).get(m, 0)
            # Amazon total revenue = Amazon New (multiplier * DTC spend) + Amazon Repeat
            nc_mult = live.get('nc_multiplier', {}).get(m, 0)
            amz_new = nc_mult * dtc_spend
            amz_repeat = live.get('amazon_repeat', {}).get(m, 0)
            amz_total_rev = amz_new + amz_repeat

            upsert_media_spend(conn, m, dtc_spend, dtc_roas, source='All Sources')
            upsert_media_spend(conn, m, amz_spend, 0.0, source='Amazon')
            upsert_amazon_revenue_forecast(conn, m, amz_total_rev)

    st.success('Revenue model saved!')


# =====================================================================
# Rendering helpers
# =====================================================================

def _render_full_pnl(live, calc, months, ml):
    """Render the full P&L as one unified HTML table."""
    n_cols = len(ml)

    # Define all sections and their rows
    # Row format: (type, label, key, format)
    #   type: 'input' = from live (green), 'input_pct' = from live as pct,
    #         'calc' = from calc, 'total' = from calc (bold)
    sections = [
        ('MEDIA SPEND', True, [
            ('input', 'DTC Spend', 'dtc_spend', '$'),
            ('input', 'Amazon Spend', 'amazon_spend', '$'),
            ('total', 'Total Media Spend', 'total_media_spend', '$'),
        ]),
        ('UNIT ECONOMICS', True, [
            ('input', 'DTC NC-ROAS', 'dtc_nc_roas', 'x'),
            ('input', 'DTC NC-AOV', 'dtc_nc_aov', '$s'),
            ('input', 'Amazon NC-AOV', 'amazon_nc_aov', '$s'),
            ('input', 'NC Multiplier', 'nc_multiplier', 'x'),
        ]),
        ('BLENDED METRICS', False, [
            ('calc', 'Blended NC-ROAS', 'blended_nc_roas', 'x'),
            ('calc', 'Blended CPA', 'blended_cpa', '$s'),
        ]),
        ('NEW CUSTOMER REVENUE', False, [
            ('calc', 'DTC New', 'dtc_new', '$'),
            ('calc', 'Amazon New', 'amazon_new', '$'),
            ('total', 'Total New', 'total_new', '$'),
            ('calc', 'DTC NC Orders', 'dtc_nc_orders', '#'),
            ('calc', 'DTC NC-CPA', 'dtc_nc_cpa', '$s'),
            ('calc', 'Amazon NC Orders', 'amazon_nc_orders', '#'),
            ('total', 'Total NC', 'total_nc', '#'),
        ]),
        ('REPEAT REVENUE', False, [
            ('input_ro', 'DTC Repeat', 'dtc_repeat', '$'),
            ('input_ro', 'Amazon Repeat', 'amazon_repeat', '$'),
            ('total', 'Total Repeat', 'total_repeat', '$'),
        ]),
        ('REVENUE SUMMARY', False, [
            ('calc', 'DTC Gross Sales', 'dtc_gross_sales', '$'),
            ('calc', 'DTC Rev (Net Sales)', 'dtc_rev', '$'),
            ('calc', 'Amazon Rev', 'amazon_rev', '$'),
            ('input', 'Wholesale Rev', 'wholesale_rev', '$'),
            ('total', 'Net Sales', 'net_sales', '$'),
            ('calc', 'Business MER', 'business_mer', 'x'),
        ]),
        ('COST ASSUMPTIONS', True, [
            ('input_raw', 'DTC Net \u2192 Gross', 'dtc_net_to_gross', 'x4'),
            ('input_pct', 'DTC Processing Fee', 'dtc_processing_pct', '%'),
            ('input_pct', 'DTC Fulfillment', 'dtc_fulfillment_pct', '%'),
            ('input_pct', 'Amazon Fulfillment', 'amazon_fulfillment_pct', '%'),
            ('input_pct', 'COGS % Gross', 'cogs_pct', '%'),
        ]),
        ('COST BREAKDOWN', False, [
            ('calc', 'DTC Processing $', 'dtc_processing_amt', '$'),
            ('calc', 'DTC Fulfillment $', 'dtc_fulfillment_amt', '$'),
            ('calc', 'Amazon Fulfillment $', 'amazon_fulfillment_amt', '$'),
            ('calc', 'Amazon COGS $', 'amazon_cogs_amt', '$'),
            ('calc', 'DTC COGS $', 'dtc_cogs_amt', '$'),
            ('total', 'Total Cost Of Sale', 'total_cos', '$'),
            ('total', 'Gross Profit', 'gross_profit', '$'),
            ('calc', 'Gross Profit %', 'gross_profit_pct', '%raw'),
        ]),
        ('P&L BOTTOM LINE', True, [
            ('input', 'Fixed Expenses', 'fixed_expenses', '$'),
            ('total', 'Total Expenses', 'total_expenses', '$'),
            ('total', 'Net Profit', 'net_profit', '$'),
            ('calc', 'Net Profit %', 'net_profit_pct', '%raw'),
        ]),
    ]

    h = []
    h.append('<div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:8px;'
             'margin:8px 0 16px;">')
    h.append('<table style="width:100%;border-collapse:collapse;font-size:0.82rem;'
             'font-family:\'DM Sans\',-apple-system,sans-serif;">')

    # Header row
    h.append('<thead><tr>')
    h.append('<th style="position:sticky;left:0;z-index:4;background:#f8fafc;'
             'padding:8px 12px;text-align:left;font-size:0.7rem;color:#94a3b8;'
             'min-width:190px;border-bottom:2px solid #e2e8f0;"></th>')
    for ml_val in ml:
        h.append(f'<th style="padding:8px 8px;text-align:right;font-size:0.7rem;'
                  f'color:#94a3b8;min-width:88px;white-space:nowrap;'
                  f'border-bottom:2px solid #e2e8f0;background:#f8fafc;">'
                  f'{ml_val}</th>')
    h.append('</tr></thead><tbody>')

    for section_name, is_editable, rows in sections:
        # Section header row
        if is_editable:
            badge = ('<span style="background:#dcfce7;color:#16a34a;font-size:0.55rem;'
                     'padding:1px 6px;border-radius:3px;margin-left:8px;font-weight:600;'
                     'letter-spacing:0.04em;">EDITABLE</span>')
        else:
            badge = ('<span style="color:#94a3b8;font-size:0.55rem;margin-left:8px;'
                     'font-weight:500;letter-spacing:0.04em;">CALCULATED</span>')

        h.append(f'<tr><td colspan="{n_cols + 1}" style="background:#f1f5f9;'
                  f'padding:8px 12px;font-size:0.7rem;font-weight:700;'
                  f'color:#475569;text-transform:uppercase;letter-spacing:0.06em;'
                  f'border-top:2px solid #e2e8f0;">'
                  f'{section_name} {badge}</td></tr>')

        for row_type, label, key, fmt in rows:
            is_input = row_type.startswith('input')
            is_total = row_type == 'total'

            # Get values: inputs from live, calculated from calc
            if is_input:
                values = {m: live.get(key, {}).get(m, 0) for m in months}
            else:
                values = {m: calc.get(key, {}).get(m, 0) for m in months}

            # Styling
            if is_total:
                row_bg = '#f8fafc'
                cell_bg = '#f8fafc'
                weight = 'font-weight:700;'
                label_color = '#0f172a'
                top_border = 'border-top:1.5px solid #cbd5e1;'
            elif is_input:
                row_bg = '#f0fdf4'
                cell_bg = '#f0fdf4'
                weight = 'font-weight:500;'
                label_color = '#15803d'
                top_border = ''
            else:
                row_bg = '#ffffff'
                cell_bg = '#ffffff'
                weight = 'font-weight:400;'
                label_color = '#334155'
                top_border = ''

            h.append(f'<tr style="{top_border}">')

            # Label
            from_wf = ''
            if row_type == 'input_ro':
                from_wf = (' <span style="color:#94a3b8;font-size:0.6rem;'
                           'font-weight:400;font-style:italic;">cohort</span>')
            elif is_input and row_type != 'input_ro':
                from_wf = (' <span style="color:#22c55e;font-size:0.55rem;'
                           'font-weight:600;">\u270e</span>')

            h.append(f'<td style="position:sticky;left:0;z-index:2;background:{row_bg};'
                      f'padding:6px 12px;{weight}color:{label_color};white-space:nowrap;'
                      f'font-size:0.8rem;border-bottom:1px solid #f1f5f9;">'
                      f'{label}{from_wf}</td>')

            # Value cells
            for m in months:
                v = values.get(m, 0)
                if row_type == 'input_pct':
                    txt = f'{v * 100:.1f}%'
                elif row_type == 'input_raw':
                    txt = rm.format_value(v, fmt)
                else:
                    txt = rm.format_value(v, fmt)

                val_color = '#dc2626' if isinstance(v, (int, float)) and v < 0 else (
                    '#0f172a' if is_total else '#334155')

                h.append(f'<td style="padding:6px 8px;text-align:right;{weight}'
                          f'color:{val_color};white-space:nowrap;font-size:0.8rem;'
                          f'background:{cell_bg};border-bottom:1px solid #f1f5f9;">'
                          f'{txt}</td>')
            h.append('</tr>')

    h.append('</tbody></table></div>')
    st.markdown(''.join(h), unsafe_allow_html=True)


_HDR_EDITABLE = (
    '<div style="background:#f0fdf4;padding:8px 14px;'
    'border-radius:6px;margin:24px 0 8px;border-left:4px solid #22c55e;">'
    '<span style="color:#16a34a;font-weight:700;font-size:0.78rem;'
    'text-transform:uppercase;letter-spacing:0.06em;">{}</span>'
    '<span style="background:#dcfce7;color:#16a34a;font-size:0.6rem;'
    'margin-left:10px;font-weight:600;padding:2px 8px;border-radius:3px;'
    'letter-spacing:0.04em;">EDITABLE</span></div>'
)

_HDR_CALC = (
    '<div style="background:#f8fafc;padding:8px 14px;'
    'border-radius:6px;margin:24px 0 8px;border-left:4px solid #94a3b8;">'
    '<span style="color:#475569;font-weight:700;font-size:0.78rem;'
    'text-transform:uppercase;letter-spacing:0.06em;">{}</span>'
    '<span style="color:#94a3b8;font-size:0.6rem;'
    'margin-left:10px;font-weight:500;letter-spacing:0.04em;">CALCULATED</span></div>'
)


def _section_header(title, editable=False):
    tpl = _HDR_EDITABLE if editable else _HDR_CALC
    st.markdown(tpl.format(title), unsafe_allow_html=True)


def _render_calc_rows(calc, months, month_labels, rows):
    """Render calculated rows as a styled HTML table fragment."""
    h = ['<div style="overflow-x:auto;margin-bottom:4px;">']
    h.append('<table style="width:100%;border-collapse:collapse;font-size:0.82rem;'
             'font-family:\'DM Sans\',-apple-system,sans-serif;">')

    # Column header
    h.append('<tr>')
    h.append('<th style="position:sticky;left:0;z-index:2;background:#f8fafc;'
             'padding:6px 12px;text-align:left;font-size:0.7rem;color:#94a3b8;'
             'min-width:200px;border-bottom:1px solid #e2e8f0;"></th>')
    for ml_val in month_labels:
        h.append(f'<th style="padding:6px 10px;text-align:right;font-size:0.7rem;'
                  f'color:#94a3b8;min-width:85px;white-space:nowrap;'
                  f'border-bottom:1px solid #e2e8f0;">{ml_val}</th>')
    h.append('</tr>')

    for key, label, fmt, is_bold in rows:
        w = 'font-weight:700;' if is_bold else 'font-weight:500;'
        bg = 'background:#f1f5f9;' if is_bold else ''
        bt = 'border-top:1.5px solid #cbd5e1;' if is_bold else ''
        h.append(f'<tr style="{bt}{bg}">')
        h.append(f'<td style="position:sticky;left:0;z-index:2;background:{("#f1f5f9" if is_bold else "#f8fafc")};'
                  f'padding:6px 12px;{w}color:#1e293b;white-space:nowrap;'
                  f'font-size:0.8rem;border-bottom:1px solid #f1f5f9;">{label}</td>')
        for m in months:
            v = calc.get(key, {}).get(m, 0)
            txt = rm.format_value(v, fmt)
            clr = '#dc2626' if isinstance(v, (int, float)) and v < 0 else '#1e293b'
            h.append(f'<td style="padding:6px 10px;text-align:right;{w}'
                      f'color:{clr};white-space:nowrap;font-size:0.8rem;'
                      f'border-bottom:1px solid #f1f5f9;">{txt}</td>')
        h.append('</tr>')

    h.append('</table></div>')
    st.markdown(''.join(h), unsafe_allow_html=True)


def _render_ttm(calc):
    """Render the TTM summary metrics bar."""
    ts = calc.get('_ttm_sales', 0)
    te = calc.get('_ttm_ebitda', 0)
    tp = calc.get('_ttm_ebitda_pct', 0)
    st.markdown(
        f'<div style="margin:14px 0 4px;padding:12px 18px;background:#f8fafc;'
        f'border-radius:8px;border:1px solid #e2e8f0;">'
        f'<div style="font-size:0.7rem;color:#94a3b8;text-transform:uppercase;'
        f'letter-spacing:0.06em;margin-bottom:8px;">TTM Summary</div>'
        f'<div style="display:flex;gap:40px;flex-wrap:wrap;">'
        f'<div><div style="font-size:0.65rem;color:#94a3b8;">TTM Sales</div>'
        f'<div style="font-size:1.05rem;font-weight:700;color:#0f172a;">${ts:,.0f}</div></div>'
        f'<div><div style="font-size:0.65rem;color:#94a3b8;">TTM EBITDA</div>'
        f'<div style="font-size:1.05rem;font-weight:700;color:#0f172a;">${te:,.0f}</div></div>'
        f'<div><div style="font-size:0.65rem;color:#94a3b8;">EBITDA %</div>'
        f'<div style="font-size:1.05rem;font-weight:700;color:#0f172a;">{tp * 100:.1f}%</div></div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )
