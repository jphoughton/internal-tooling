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
    _render_revenue_model(current)

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
def _render_revenue_model(current):
    st.subheader('Revenue Model')
    st.caption('Green sections are editable inputs. Calculated rows update live. '
               'Click **Save Revenue Model** at the bottom to persist changes.')

    horizon = current.get('forecast_horizon', 12)
    months = _month_list(horizon)
    ml = [_month_label(m) for m in months]  # month labels for columns

    # Load from DB + merge with defaults
    with get_db() as conn:
        db_data = get_revenue_model(conn)
    inputs = rm.merge_with_defaults(db_data, months)

    # Mutable copy for live edits
    live = {var: dict(inputs.get(var, {})) for var in rm.EDITABLE_VARS}

    # ── MEDIA SPEND (editable) ──────────────────────────────────────
    _section_header('MEDIA SPEND', editable=True)
    spend_df = _build_df({
        'DTC Spend': 'dtc_spend',
        'Amazon Spend': 'amazon_spend',
    }, live, months, ml)
    edited_spend = st.data_editor(
        spend_df, key='rm_spend', use_container_width=True, height=110,
        column_config={c: st.column_config.NumberColumn(c, format='$%,.0f', step=1000) for c in ml},
    )
    _apply_edits(live, edited_spend, months, ml, {
        'DTC Spend': 'dtc_spend', 'Amazon Spend': 'amazon_spend',
    })
    calc = rm.compute(live, months)
    _render_calc_rows(calc, months, ml, [
        ('total_media_spend', 'Total Media Spend', '$', True),
    ])

    # ── UNIT ECONOMICS (editable) ───────────────────────────────────
    _section_header('UNIT ECONOMICS', editable=True)
    econ_df = _build_df({
        'DTC NC-AOV': 'dtc_nc_aov',
        'DTC NC-ROAS': 'dtc_nc_roas',
        'Amazon NC-AOV': 'amazon_nc_aov',
        'DTC / Amazon NC Multiplier': 'nc_multiplier',
    }, live, months, ml)
    edited_econ = st.data_editor(
        econ_df, key='rm_econ', use_container_width=True, height=180,
        column_config={c: st.column_config.NumberColumn(c, format='%.2f', step=0.01) for c in ml},
    )
    _apply_edits(live, edited_econ, months, ml, {
        'DTC NC-AOV': 'dtc_nc_aov', 'DTC NC-ROAS': 'dtc_nc_roas',
        'Amazon NC-AOV': 'amazon_nc_aov', 'DTC / Amazon NC Multiplier': 'nc_multiplier',
    })
    calc = rm.compute(live, months)

    # ── BLENDED METRICS (calculated) ────────────────────────────────
    _section_header('BLENDED METRICS')
    _render_calc_rows(calc, months, ml, [
        ('blended_nc_roas', 'Blended NC-ROAS', 'x', False),
        ('blended_cpa', 'Blended CPA', '$s', False),
    ])

    # ── NEW CUSTOMER REVENUE (calculated) ───────────────────────────
    _section_header('NEW CUSTOMER REVENUE')
    _render_calc_rows(calc, months, ml, [
        ('dtc_new', 'DTC New', '$', False),
        ('amazon_new', 'Amazon New', '$', False),
        ('total_new', 'Total New', '$', True),
        ('dtc_nc_orders', 'DTC NC Orders', '#', False),
        ('dtc_nc_cpa', 'DTC NC-CPA', '$s', False),
        ('amazon_nc_orders', 'Amazon NC Orders', '#', False),
        ('total_nc', 'Total NC', '#', True),
    ])

    # ── REPEAT REVENUE (editable) ───────────────────────────────────
    _section_header('REPEAT REVENUE', editable=True)
    repeat_df = _build_df({
        'DTC Repeat': 'dtc_repeat',
        'Amazon Repeat': 'amazon_repeat',
    }, live, months, ml)
    edited_repeat = st.data_editor(
        repeat_df, key='rm_repeat', use_container_width=True, height=110,
        column_config={c: st.column_config.NumberColumn(c, format='$%,.0f', step=1000) for c in ml},
    )
    _apply_edits(live, edited_repeat, months, ml, {
        'DTC Repeat': 'dtc_repeat', 'Amazon Repeat': 'amazon_repeat',
    })
    calc = rm.compute(live, months)
    _render_calc_rows(calc, months, ml, [
        ('total_repeat', 'Total Repeat', '$', True),
    ])

    # ── REVENUE SUMMARY (calculated + Wholesale editable) ───────────
    _section_header('REVENUE SUMMARY')
    _render_calc_rows(calc, months, ml, [
        ('dtc_gross_sales', 'DTC Gross Sales', '$', False),
        ('dtc_rev', 'DTC Rev (Total Sales)', '$', False),
        ('amazon_rev', 'Amazon Rev', '$', False),
    ])
    # Wholesale Rev: small inline editor
    ws_df = _build_df({'Wholesale Rev': 'wholesale_rev'}, live, months, ml)
    edited_ws = st.data_editor(
        ws_df, key='rm_wholesale', use_container_width=True, height=75,
        column_config={c: st.column_config.NumberColumn(c, format='$%,.0f', step=100) for c in ml},
    )
    _apply_edits(live, edited_ws, months, ml, {'Wholesale Rev': 'wholesale_rev'})
    calc = rm.compute(live, months)
    _render_calc_rows(calc, months, ml, [
        ('net_sales', 'Net Sales', '$', True),
        ('business_mer', 'Business MER', 'x', False),
    ])

    # ── COST ASSUMPTIONS (editable) ─────────────────────────────────
    _section_header('COST ASSUMPTIONS', editable=True)
    # Display percentages as whole numbers (4.0 not 0.04) for readability
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
    # Convert display percentages back to decimals
    _apply_edits_pct(live, edited_cost, months, ml, {
        'DTC Net \u2192 Gross': ('dtc_net_to_gross', False),
        'DTC Processing Fee (%)': ('dtc_processing_pct', True),
        'DTC Fulfillment (%)': ('dtc_fulfillment_pct', True),
        'Amazon Fulfillment (%)': ('amazon_fulfillment_pct', True),
        'COGS % Gross': ('cogs_pct', True),
    })
    calc = rm.compute(live, months)

    # ── COST BREAKDOWN (calculated) ─────────────────────────────────
    _section_header('COST BREAKDOWN')
    _render_calc_rows(calc, months, ml, [
        ('dtc_processing_amt', 'DTC Processing Fee $', '$', False),
        ('dtc_fulfillment_amt', 'DTC Fulfillment $', '$', False),
        ('amazon_fulfillment_amt', 'Amazon Fulfillment $', '$', False),
        ('amazon_cogs_amt', 'Amazon COGS $', '$', False),
        ('dtc_cogs_amt', 'DTC COGS $', '$', False),
        ('total_cos', 'Total Cost Of Sale', '$', True),
        ('gross_profit', 'Gross Profit', '$', True),
        ('gross_profit_pct', 'Gross Profit %', '%raw', False),
    ])

    # ── P&L BOTTOM LINE (Fixed Expenses editable) ───────────────────
    _section_header('P&L BOTTOM LINE', editable=True)
    fixed_df = _build_df({'Fixed Expenses': 'fixed_expenses'}, live, months, ml)
    edited_fixed = st.data_editor(
        fixed_df, key='rm_fixed', use_container_width=True, height=75,
        column_config={c: st.column_config.NumberColumn(c, format='$%,.0f', step=1000) for c in ml},
    )
    _apply_edits(live, edited_fixed, months, ml, {'Fixed Expenses': 'fixed_expenses'})
    calc = rm.compute(live, months)
    _render_calc_rows(calc, months, ml, [
        ('total_expenses', 'Total Expenses', '$', True),
        ('net_profit', 'Net Profit', '$', True),
        ('net_profit_pct', 'Net Profit %', '%raw', False),
    ])

    # ── TTM Summary ──
    _render_ttm(calc)

    # ── FUNNEL GOALS (in expander) ──────────────────────────────────
    with st.expander('Funnel Goals', expanded=False):
        _section_header('FUNNEL GOALS', editable=True)
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
        calc = rm.compute(live, months)

        # Funnel spend mirrors DTC Spend
        _render_calc_rows(calc, months, ml, [
            ('funnel_new_visitors', 'New Visitors', '#', False),
            ('funnel_new_customers', 'New Customers', '#', False),
            ('funnel_nc_rev', 'NC-Rev', '$', False),
            ('funnel_nc_roas', 'NC-ROAS', 'x', False),
        ])

    # ── Save Button ──
    st.markdown('<div style="height:16px;"></div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        if st.button('\U0001f4be  Save Revenue Model', type='primary',
                      use_container_width=True, key='rm_save'):
            _save_revenue_model(live, months)
            st.session_state['_bv_revmodel_saved'] = True
            st.rerun()


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

_HDR_EDITABLE = (
    '<div style="background:rgba(76,175,80,0.12);padding:6px 14px;'
    'border-radius:6px;margin:20px 0 6px;border-left:4px solid #66bb6a;">'
    '<span style="color:#66bb6a;font-weight:700;font-size:0.78rem;'
    'text-transform:uppercase;letter-spacing:0.06em;">{}</span>'
    '<span style="color:rgba(102,187,106,0.5);font-size:0.65rem;'
    'margin-left:8px;font-weight:500;">EDITABLE</span></div>'
)

_HDR_CALC = (
    '<div style="background:rgba(255,255,255,0.03);padding:6px 14px;'
    'border-radius:6px;margin:20px 0 6px;border-left:4px solid rgba(255,255,255,0.15);">'
    '<span style="color:rgba(255,255,255,0.55);font-weight:700;font-size:0.78rem;'
    'text-transform:uppercase;letter-spacing:0.06em;">{}</span></div>'
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
    h.append('<th style="position:sticky;left:0;z-index:2;background:#0e1117;'
             'padding:5px 12px;text-align:left;font-size:0.7rem;color:rgba(255,255,255,0.4);'
             'min-width:200px;border-bottom:1px solid rgba(255,255,255,0.06);"></th>')
    for ml_val in month_labels:
        h.append(f'<th style="padding:5px 10px;text-align:right;font-size:0.7rem;'
                  f'color:rgba(255,255,255,0.4);min-width:85px;white-space:nowrap;'
                  f'border-bottom:1px solid rgba(255,255,255,0.06);">{ml_val}</th>')
    h.append('</tr>')

    for key, label, fmt, is_bold in rows:
        w = 'font-weight:700;' if is_bold else ''
        bt = 'border-top:1.5px solid rgba(255,255,255,0.12);' if is_bold else ''
        h.append(f'<tr style="{bt}">')
        h.append(f'<td style="position:sticky;left:0;z-index:2;background:#0e1117;'
                  f'padding:5px 12px;{w}color:rgba(255,255,255,0.82);white-space:nowrap;'
                  f'font-size:0.8rem;">{label}</td>')
        for m in months:
            v = calc.get(key, {}).get(m, 0)
            txt = rm.format_value(v, fmt)
            clr = '#ef5350' if isinstance(v, (int, float)) and v < 0 else 'rgba(255,255,255,0.82)'
            h.append(f'<td style="padding:5px 10px;text-align:right;{w}'
                      f'color:{clr};white-space:nowrap;font-size:0.8rem;">{txt}</td>')
        h.append('</tr>')

    h.append('</table></div>')
    st.markdown(''.join(h), unsafe_allow_html=True)


def _render_ttm(calc):
    """Render the TTM summary metrics bar."""
    ts = calc.get('_ttm_sales', 0)
    te = calc.get('_ttm_ebitda', 0)
    tp = calc.get('_ttm_ebitda_pct', 0)
    st.markdown(
        f'<div style="margin:14px 0 4px;padding:12px 18px;background:rgba(255,255,255,0.04);'
        f'border-radius:8px;border:1px solid rgba(255,255,255,0.08);">'
        f'<div style="font-size:0.7rem;color:rgba(255,255,255,0.4);text-transform:uppercase;'
        f'letter-spacing:0.06em;margin-bottom:8px;">TTM Summary</div>'
        f'<div style="display:flex;gap:40px;flex-wrap:wrap;">'
        f'<div><div style="font-size:0.65rem;color:rgba(255,255,255,0.35);">TTM Sales</div>'
        f'<div style="font-size:1.05rem;font-weight:700;color:#fff;">${ts:,.0f}</div></div>'
        f'<div><div style="font-size:0.65rem;color:rgba(255,255,255,0.35);">TTM EBITDA</div>'
        f'<div style="font-size:1.05rem;font-weight:700;color:#fff;">${te:,.0f}</div></div>'
        f'<div><div style="font-size:0.65rem;color:rgba(255,255,255,0.35);">EBITDA %</div>'
        f'<div style="font-size:1.05rem;font-weight:700;color:#fff;">{tp * 100:.1f}%</div></div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )
