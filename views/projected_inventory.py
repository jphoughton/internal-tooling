"""Projected Inventory page."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
from dateutil.relativedelta import relativedelta
from db import (
    get_db, read_sql,
    get_media_spend, get_amazon_revenue_forecast,
    get_planned_inbound_dict,
    get_last_sync_timestamp, get_new_rows_since_yesterday, get_synced_sources,
)
from analytics.sku_flavors import get_flavor, get_sku_sales_rank
from analytics.dtc_demand import (
    build_master_dtc_forecast,
    get_current_month_progress,
)
from ui.components import render_html_table, render_freshness_badge
import config


def render(ctx):
    """Render the Projected Inventory page."""
    FORECAST_SKUS = ctx['forecast_skus']
    _cached_waterfall = ctx['cached_waterfall']
    _cached_sku_forecast = ctx['cached_sku_forecast']
    _load_seasonal_json = ctx['load_seasonal_json']

    _title_col, _badge_col = st.columns([7, 3])
    with _title_col:
        st.title('Projected Inventory')
    with _badge_col:
        with get_db() as conn:
            _ts = get_last_sync_timestamp(conn, ['shopify', 'amazon'])
            _new = get_new_rows_since_yesterday(conn, ['shopify', 'amazon'])
            _srcs = get_synced_sources(conn, ['shopify', 'amazon'])
        _src_label = ' + '.join(s.title() for s in sorted(_srcs)) if _srcs else None
        render_freshness_badge(last_refreshed_str=_ts, new_rows=_new, source=_src_label)
    st.caption('Combines current inventory across all channels (3PL + Amazon FBA) with the full DTC demand forecast (Shopify + Amazon) to project inventory levels per SKU per month.')

    # --- Fetch live inventory from all sources ---
    pi_inv_3pl = []
    pi_inv_amz = []
    pi_has_3pl = False
    pi_has_amz = False

    with st.spinner('Fetching live inventory...'):
        if config.PACKIYO_API_TOKEN:
            try:
                from etl.packiyo_client import get_inventory as pi_get_3pl
                pi_inv_3pl = pi_get_3pl()
                pi_has_3pl = True
            except Exception as e:
                st.warning(f'Could not fetch 3PL inventory: {e}')

        pi_has_amz_creds = all(getattr(config, k, '') for k in [
            'AMAZON_REFRESH_TOKEN', 'AMAZON_LWA_CLIENT_ID', 'AMAZON_LWA_CLIENT_SECRET',
        ])
        if pi_has_amz_creds:
            try:
                from etl.amazon_inventory import get_inventory as pi_get_fba
                pi_inv_amz = pi_get_fba()
                pi_has_amz = True
            except Exception as e:
                st.warning(f'Could not fetch Amazon FBA inventory: {e}')

    pi_has_inv = pi_has_3pl or pi_has_amz

    # Merge inventory across sources
    pi_combined = {}
    for item in pi_inv_3pl:
        sku = item['sku']
        pi_combined[sku] = {
            'sku': sku,
            'name': item.get('name', ''),
            '3pl_available': item.get('quantity_available', 0) or 0,
            'fba_fulfillable': 0,
            'total_available': item.get('quantity_available', 0) or 0,
            'inbound': item.get('quantity_inbound', 0) or 0,
        }
    for item in pi_inv_amz:
        sku = item['sku']
        # Use total_quantity (fulfillable + reserved + inbound + unfulfillable)
        # for inventory planning, since inbound stock will become available.
        fba_total = item.get('total_quantity', 0) or 0
        fba_inbound = (item.get('inbound_shipped', 0) or 0) + (item.get('inbound_receiving', 0) or 0)
        if sku in pi_combined:
            pi_combined[sku]['fba_fulfillable'] = fba_total
            pi_combined[sku]['total_available'] += fba_total
            pi_combined[sku]['inbound'] += fba_inbound
        else:
            pi_combined[sku] = {
                'sku': sku,
                'name': item.get('product_name', ''),
                '3pl_available': 0,
                'fba_fulfillable': fba_total,
                'total_available': fba_total,
                'inbound': fba_inbound,
            }

    if not pi_has_inv:
        st.warning('No inventory sources connected. Connect Packiyo (3PL) or Amazon (FBA) in **Settings** to see projected inventory.')
    else:
        # --- Get the demand forecast ---
        pi_forecast_ok = False
        pi_rollup = pd.DataFrame()
        pi_revenue = {}
        try:
            import json as _json_pi
            with get_db() as conn:
                pi_spend = get_media_spend(conn, source='All Sources')
                pi_amz_rev = get_amazon_revenue_forecast(conn)
            if not pi_spend:
                pi_spend = [{'month': (datetime.utcnow() + relativedelta(months=i)).strftime('%Y-%m'),
                             'spend': 0, 'new_customer_roas': 0.7} for i in range(12)]
            pi_amz_dict = {r['month']: r['revenue'] for r in pi_amz_rev if r.get('revenue', 0) > 0} if pi_amz_rev else None

            pi_media_json = _json_pi.dumps(pi_spend, sort_keys=True)
            pi_wf = _cached_waterfall(pi_media_json, None, 12, _load_seasonal_json())
            pi_sku_fc = _cached_sku_forecast(pi_wf.to_json(), None) if not pi_wf.empty else pd.DataFrame()

            _bv_pi = ctx.get('biz_vars', {})
            pi_dtc = build_master_dtc_forecast(
                shopify_waterfall_df=pi_wf,
                shopify_sku_forecast_df=pi_sku_fc,
                amazon_growth_rate=_bv_pi.get('amazon_growth_pct', 0.0),
                horizon_months=_bv_pi.get('forecast_horizon', 12),
                forecast_skus=FORECAST_SKUS,
                media_plan=pi_spend,
                amazon_revenue_forecast=pi_amz_dict,
            )
            pi_rollup = pi_dtc['rollup_table']
            pi_revenue = pi_dtc.get('revenue', {})
            pi_forecast_ok = not pi_rollup.empty
        except Exception as e:
            st.warning(f'Could not load demand forecast: {e}')

        if not pi_forecast_ok:
            st.warning('No demand forecast available. Set up your media spend plan in **Demand Forecast** first.')
        else:
            # --- Month progress ---
            mp = get_current_month_progress()
            current_month = mp['current_month']

            month_cols = [c for c in pi_rollup.columns if c.startswith('20')]
            month_labels = {}
            for m in month_cols:
                try:
                    dt = datetime.strptime(m, '%Y-%m')
                    month_labels[m] = dt.strftime("%b '%y")
                except ValueError:
                    month_labels[m] = m

            # --- Planned Inbound (from sidebar Business Variables > Orders tab) ---
            st.caption("Planned inbound — edit in sidebar **Business Variables > Orders** tab.")
            with get_db() as conn:
                inbound_for_projection = get_planned_inbound_dict(conn)

            st.divider()

            # --- Build projected inventory table ---
            # For each SKU: start with current inventory (3PL + FBA), subtract monthly demand, add planned inbound
            proj_rows = []
            _pi_rank = get_sku_sales_rank()
            _pi_max_rank = len(_pi_rank)
            for sku in sorted(FORECAST_SKUS, key=lambda s: _pi_rank.get(s, _pi_max_rank)):
                inv = pi_combined.get(sku, {'3pl_available': 0, 'fba_fulfillable': 0, 'total_available': 0, 'inbound': 0, 'name': ''})
                fc_row = pi_rollup[pi_rollup['SKU'] == sku]
                sku_planned = inbound_for_projection.get(sku, {})

                flavor = get_flavor(sku)
                starting_inv = inv['total_available'] + inv['inbound']
                running_inv = starting_inv

                row = {
                    'SKU': sku,
                    'Flavor': flavor,
                    '3PL': inv['3pl_available'],
                    'FBA': inv['fba_fulfillable'],
                    'In Transit': inv['inbound'],
                    'Total Stock': inv['total_available'] + inv['inbound'],
                }

                for m in month_cols:
                    demand = 0
                    if not fc_row.empty and m in fc_row.columns:
                        demand = int(fc_row.iloc[0][m] or 0)

                    # For current month, only subtract remaining demand
                    if m == current_month:
                        demand = round(demand * mp['pct_remaining'])

                    # Add planned inbound for this month
                    landing = sku_planned.get(m, 0)
                    running_inv = running_inv - demand + landing
                    row[m] = round(running_inv)

                proj_rows.append(row)

            proj_df = pd.DataFrame(proj_rows)

            # --- KPI summary ---
            total_3pl = sum(pi_combined.get(s, {}).get('3pl_available', 0) for s in FORECAST_SKUS)
            total_fba = sum(pi_combined.get(s, {}).get('fba_fulfillable', 0) for s in FORECAST_SKUS)
            total_inv = total_3pl + total_fba
            total_in_transit = sum(pi_combined.get(s, {}).get('inbound', 0) for s in FORECAST_SKUS)
            total_planned = sum(
                sum(inbound_for_projection.get(s, {}).get(m, 0) for m in month_cols)
                for s in FORECAST_SKUS
            )

            # Find SKUs that go negative (stockout) and when
            stockout_skus = []
            for _, row in proj_df.iterrows():
                for m in month_cols:
                    if row[m] < 0:
                        stockout_skus.append({
                            'sku': row['SKU'],
                            'flavor': row['Flavor'],
                            'month': m,
                            'deficit': abs(row[m]),
                        })
                        break  # first stockout month only

            kc1, kc2, kc3, kc4, kc5, kc6 = st.columns(6)
            kc1.metric('3PL Stock', f'{total_3pl:,}' if pi_has_3pl else '\u2014')
            kc2.metric('FBA Stock', f'{total_fba:,}' if pi_has_amz else '\u2014')
            kc3.metric('Combined Stock', f'{total_inv:,}')
            kc4.metric('In Transit', f'{total_in_transit:,}')
            kc5.metric('Planned Inbound', f'{total_planned:,}', help="Total units you've entered in the planned inbound table")
            kc6.metric('Projected Stockouts', f'{len(stockout_skus)} SKUs', help='SKUs projected to go negative within forecast window')

            st.caption(
                f"Current month: {datetime.strptime(current_month, '%Y-%m').strftime('%B %Y')} "
                f"(day {mp['day_of_month']}/{mp['days_in_month']} \u2014 only remaining {mp['days_remaining']} days of demand deducted for this month)"
            )

            st.divider()

            # --- Projected Inventory Table ---
            st.subheader('Projected Inventory by SKU')
            st.caption(
                'Starting from combined 3PL + FBA stock + in transit, subtract total forecasted demand (Shopify + Amazon) '
                'and add planned inbound each month. Negative (red) = projected stockout. Low stock (yellow) = under 500 units.'
            )

            # Add Days of Supply column (total stock / daily demand rate)
            _dos_map = {}
            for _, _r in pi_rollup.iterrows():
                _fc_3mo = sum(_r.get(m, 0) or 0 for m in month_cols[:3])
                if _fc_3mo > 0:
                    _dos_map[_r['SKU']] = _fc_3mo / 91.32
            for i, _row in proj_df.iterrows():
                _daily = _dos_map.get(_row['SKU'], 0)
                proj_df.at[i, 'DoS'] = round(_row['Total Stock'] / _daily) if _daily > 0 else None

            # Format for display
            disp_proj = proj_df.copy()
            disp_proj = disp_proj.rename(columns=month_labels)
            disp_month_labels = [month_labels[m] for m in month_cols]

            # Format DoS as "Xd"
            disp_proj['DoS'] = disp_proj['DoS'].apply(
                lambda x: f'{int(x)}d' if pd.notna(x) else '\u2014'
            )

            # Reorder: put DoS right after Total Stock
            _col_order = ['SKU', 'Flavor', '3PL', 'FBA', 'In Transit', 'Total Stock', 'DoS'] + disp_month_labels
            disp_proj = disp_proj[[c for c in _col_order if c in disp_proj.columns]]

            # Color function: red for negative, yellow for low stock
            def _color_inv(val):
                if isinstance(val, (int, float)):
                    if val < 0:
                        return 'background-color: #fee2e2; color: #991b1b; font-weight: bold'
                    elif val < 500:
                        return 'background-color: #fef3c7; color: #92400e'
                return ''

            # Color function for DoS strings
            def _color_dos(val):
                if isinstance(val, str) and val.endswith('d'):
                    try:
                        days = int(val[:-1])
                        if days < 30:
                            return 'background-color: #fee2e2; color: #991b1b; font-weight: bold'
                        elif days < 60:
                            return 'background-color: #fef3c7; color: #92400e'
                    except ValueError:
                        pass
                return ''

            # Apply number formatting before coloring
            _num_cols = ['3PL', 'FBA', 'In Transit', 'Total Stock'] + disp_month_labels
            for _nc in _num_cols:
                if _nc in disp_proj.columns:
                    disp_proj[_nc] = disp_proj[_nc].apply(lambda x: f'{x:,.0f}' if isinstance(x, (int, float)) else x)

            # Combined style function
            _inv_style_cols = _num_cols + ['DoS']

            def _color_cell(val):
                # Parse formatted number strings back
                if isinstance(val, str):
                    if val.endswith('d'):
                        return _color_dos(val)
                    cleaned = val.replace(',', '').strip()
                    try:
                        return _color_inv(float(cleaned))
                    except ValueError:
                        return ''
                return _color_inv(val)

            render_html_table(
                disp_proj,
                max_height=min(len(disp_proj) * 35 + 38, 700),
                style_fn=_color_cell,
                style_cols=_inv_style_cols,
                column_groups=[
                    ('', ['SKU', 'Flavor']),
                    ('Current Stock', ['3PL', 'FBA', 'In Transit', 'Total Stock', 'DoS']),
                    ('Monthly Projections', disp_month_labels),
                ],
            )

            # --- Stockout Alerts ---
            if stockout_skus:
                st.divider()
                st.subheader('Projected Stockouts')
                st.caption('SKUs projected to run out of inventory if no reorder is placed.')

                for so in sorted(stockout_skus, key=lambda x: x['month']):
                    mo_label = datetime.strptime(so['month'], '%Y-%m').strftime('%B %Y')
                    st.warning(
                        f"**{so['sku']}** ({so['flavor']}) \u2014 projected stockout in **{mo_label}** "
                        f"(deficit: {so['deficit']:,} units)"
                    )

            # --- Inventory Runway Chart ---
            st.divider()
            st.subheader('Inventory Runway')
            st.caption('Visual projection of inventory levels over time. Lines that dip below zero indicate stockouts.')

            fig_pi = go.Figure()
            for _, row in proj_df.iterrows():
                sku = row['SKU']
                flavor = row['Flavor'] or sku
                inv_vals = [row[m] for m in month_cols]

                fig_pi.add_trace(go.Scatter(
                    x=[month_labels[m] for m in month_cols],
                    y=inv_vals,
                    mode='lines+markers',
                    name=flavor,
                    marker=dict(size=4),
                ))

            fig_pi.add_shape(
                type='line', x0=0, x1=1, xref='paper', y0=0, y1=0,
                line=dict(dash='solid', color='#E05252', width=1.5),
            )
            fig_pi.add_annotation(
                x=0, xref='paper', y=0, text='Stockout Line',
                showarrow=False, yanchor='bottom', xanchor='left',
                font=dict(size=11, color='#E05252'),
            )
            fig_pi.update_layout(
                height=500,
                margin=dict(l=0, r=0, t=10, b=0),
                xaxis_title='Month',
                yaxis_title='Projected Inventory (units)',
                legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0),
            )
            st.plotly_chart(fig_pi, use_container_width=True)
