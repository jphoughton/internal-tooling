"""Business Variables page — planning variables, media spend, and inbound orders."""
import streamlit as st
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import (
    get_db, get_setting, get_seasonal_indices,
    get_media_spend, upsert_media_spend,
    get_amazon_revenue_forecast, upsert_amazon_revenue_forecast,
    get_planned_inbound_dict, upsert_planned_inbound,
)
from ui.business_vars import (
    BUSINESS_VARS, _GROUPS,
    get_business_vars, save_business_vars,
    _month_list, _month_label,
)
from analytics.sku_flavors import get_flavor
from analytics.waterfall import clear_waterfall_cache


def render(ctx):
    """Render the Business Variables page."""
    st.title('Business Variables')

    forecast_skus = ctx.get('forecast_skus', set())

    # Check for dialog save flags (set inside dialogs before rerun)
    if st.session_state.pop('_bv_page_media_saved', False):
        clear_waterfall_cache()
        st.cache_data.clear()
        st.rerun()
    if st.session_state.pop('_bv_page_orders_saved', False):
        clear_waterfall_cache()
        st.cache_data.clear()
        st.rerun()

    current = get_business_vars()

    # ── Scalar Variables ──
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
                st.caption(f'Peak: {_mn[pk-1]} ({indices[pk]:.2f}) · Low: {_mn[lo-1]} ({indices[lo]:.2f})')
        except Exception:
            pass

    if st.button('Save Variables', type='primary', key='bvpage_apply'):
        save_business_vars(edits)
        clear_waterfall_cache()
        st.cache_data.clear()
        st.success('Variables saved!')
        st.rerun()

    # ── Media Spend Plan ──
    st.divider()
    st.subheader('Media Spend Plan')
    st.caption('Edit monthly media spend across all channels. AMZ Revenue $0 = use velocity-based projection.')

    horizon = current.get('forecast_horizon', 12)
    months = _month_list(horizon)

    with get_db() as conn:
        dtc_rows = get_media_spend(conn, source='All Sources')
        amz_spend_rows = get_media_spend(conn, source='Amazon')
        amz_rev_rows = get_amazon_revenue_forecast(conn)

    dtc_lookup = {r['month']: r for r in dtc_rows}
    amz_spend_lookup = {r['month']: r['spend'] for r in amz_spend_rows}
    amz_rev_lookup = {r['month']: r['revenue'] for r in amz_rev_rows}

    # Header row
    hdr = st.columns([1.2, 1, 0.7, 1, 1])
    hdr[0].markdown('**Month**')
    hdr[1].markdown('**DTC Spend**')
    hdr[2].markdown('**ROAS**')
    hdr[3].markdown('**AMZ Spend**')
    hdr[4].markdown('**AMZ Revenue**')

    media_results = []
    with st.form('bvpage_media_spend_form'):
        for i, m in enumerate(months):
            existing = dtc_lookup.get(m, {'spend': 5000.0, 'new_customer_roas': 2.0})
            cols = st.columns([1.2, 1, 0.7, 1, 1])

            cols[0].markdown(
                f'<div style="padding:8px 0;font-weight:600;font-size:0.85rem;">'
                f'{_month_label(m)}</div>',
                unsafe_allow_html=True,
            )
            dtc = cols[1].number_input(
                'DTC $', value=float(existing.get('spend', 5000.0)),
                min_value=0.0, step=500.0, format='%.0f',
                key=f'bvpg_ms_dtc_{i}', label_visibility='collapsed',
            )
            roas = cols[2].number_input(
                'ROAS', value=float(existing.get('new_customer_roas', 2.0)),
                min_value=0.1, step=0.1, format='%.1f',
                key=f'bvpg_ms_roas_{i}', label_visibility='collapsed',
            )
            amz_s = cols[3].number_input(
                'AMZ $', value=float(amz_spend_lookup.get(m, 0.0)),
                min_value=0.0, step=500.0, format='%.0f',
                key=f'bvpg_ms_amzs_{i}', label_visibility='collapsed',
            )
            amz_r = cols[4].number_input(
                'AMZ Rev', value=float(amz_rev_lookup.get(m, 0.0)),
                min_value=0.0, step=5000.0, format='%.0f',
                key=f'bvpg_ms_amzr_{i}', label_visibility='collapsed',
            )
            media_results.append((m, dtc, roas, amz_s, amz_r))

        if st.form_submit_button('Save Media Spend', type='primary',
                                  use_container_width=True):
            with get_db() as conn:
                for m, dtc, roas, amz_s, amz_r in media_results:
                    upsert_media_spend(conn, m, dtc, roas, source='All Sources')
                    upsert_media_spend(conn, m, amz_s, 0.0, source='Amazon')
                    upsert_amazon_revenue_forecast(conn, m, amz_r)
            st.session_state['_bv_page_media_saved'] = True
            st.rerun()

    # ── Planned Inbound Orders ──
    if forecast_skus:
        st.divider()
        st.subheader('Planned Inbound Orders')
        st.caption('Units arriving per SKU per month. Edit one quarter at a time.')

        with get_db() as conn:
            existing_inbound = get_planned_inbound_dict(conn)

        # Split months into quarters of 3
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

                # Header
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
