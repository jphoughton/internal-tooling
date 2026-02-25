"""Settings page — API credentials, data management, imports."""
import streamlit as st
import pandas as pd
from datetime import datetime
from db import (
    get_db, read_sql,
    get_setting, set_setting,
    upsert_customer, upsert_order, upsert_order_item, upsert_sku,
    get_model_runs,
)
from config import save_env, reload_config
import config
from analytics.waterfall import clear_waterfall_cache
from ui.components import render_html_table


def render(ctx):
    """Render the Settings page."""
    st.title("Settings")

    # --- Amazon SP-API ---
    st.subheader("Amazon SP-API")
    amazon_vals = {}
    amazon_vals["AMAZON_REFRESH_TOKEN"] = st.text_input(
        "Refresh Token",
        value=getattr(config, "AMAZON_REFRESH_TOKEN", ""),
        type="password",
        key="settings_AMAZON_REFRESH_TOKEN",
        help="From Seller Central -> Develop Apps -> Your App -> Sandbox/Authorize page",
    )
    amazon_vals["AMAZON_LWA_CLIENT_ID"] = st.text_input(
        "LWA Client ID",
        value=getattr(config, "AMAZON_LWA_CLIENT_ID", ""),
        key="settings_AMAZON_LWA_CLIENT_ID",
        help="From Seller Central -> Develop Apps -> Your App -> LWA Credentials (starts with amzn1.application-oa2-client...)",
    )
    amazon_vals["AMAZON_LWA_CLIENT_SECRET"] = st.text_input(
        "LWA Client Secret",
        value=getattr(config, "AMAZON_LWA_CLIENT_SECRET", ""),
        type="password",
        key="settings_AMAZON_LWA_CLIENT_SECRET",
        help="From the same LWA Credentials section",
    )
    amazon_vals["AMAZON_MARKETPLACE_ID"] = st.text_input(
        "Marketplace ID",
        value=getattr(config, "AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER"),
        key="settings_AMAZON_MARKETPLACE_ID",
        help="US marketplace is ATVPDKIKX0DER (default). Only change if selling in a different country.",
    )

    st.divider()

    # --- Shopify ---
    st.subheader("Shopify Admin API")
    shopify_vals = {}
    shopify_vals["SHOPIFY_STORE_URL"] = st.text_input(
        "Shopify Store URL",
        value=getattr(config, "SHOPIFY_STORE_URL", ""),
        placeholder="your-store.myshopify.com",
        key="settings_SHOPIFY_STORE_URL",
    )
    shopify_vals["SHOPIFY_CLIENT_ID"] = st.text_input(
        "Shopify Client ID",
        value=getattr(config, "SHOPIFY_CLIENT_ID", ""),
        key="settings_SHOPIFY_CLIENT_ID",
    )
    shopify_vals["SHOPIFY_CLIENT_SECRET"] = st.text_input(
        "Shopify Client Secret",
        value=getattr(config, "SHOPIFY_CLIENT_SECRET", ""),
        type="password",
        key="settings_SHOPIFY_CLIENT_SECRET",
    )
    shopify_vals["SHOPIFY_API_VERSION"] = st.text_input(
        "Shopify API Version",
        value=getattr(config, "SHOPIFY_API_VERSION", "2024-01"),
        key="settings_SHOPIFY_API_VERSION",
    )

    if getattr(config, "SHOPIFY_ACCESS_TOKEN", ""):
        st.success("Access token is saved.")
        if st.button("Test Shopify Connection"):
            from etl.shopify_client import test_connection
            ok, msg = test_connection()
            if ok:
                st.success(msg)
            else:
                st.error(msg)
    else:
        st.caption("Enter your Client ID and Secret from the Shopify Dev Dashboard, then click **Connect Shopify**.")

    st.divider()

    # --- Packiyo 3PL ---
    st.subheader("Packiyo 3PL")
    packiyo_vals = {}
    packiyo_vals["PACKIYO_API_TOKEN"] = st.text_input(
        "API Token",
        value=getattr(config, "PACKIYO_API_TOKEN", ""),
        type="password",
        key="settings_PACKIYO_API_TOKEN",
        help="Bearer token from Packiyo (e.g. 256|jKuI...)",
    )
    packiyo_vals["PACKIYO_API_URL"] = st.text_input(
        "API URL",
        value=getattr(config, "PACKIYO_API_URL", "https://aveshops.packiyo.com/api/v1"),
        key="settings_PACKIYO_API_URL",
        help="Packiyo API base URL",
    )
    packiyo_vals["PACKIYO_CUSTOMER_ID"] = st.text_input(
        "Customer ID",
        value=getattr(config, "PACKIYO_CUSTOMER_ID", "12"),
        key="settings_PACKIYO_CUSTOMER_ID",
        help="Your Packiyo customer ID",
    )

    if getattr(config, "PACKIYO_API_TOKEN", ""):
        if st.button("Test Packiyo Connection"):
            from etl.packiyo_client import test_connection as test_packiyo
            ok, msg = test_packiyo()
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    st.divider()

    # --- Save ---
    col_save, col_oauth = st.columns(2)
    with col_save:
        if st.button("Save Credentials", type="primary"):
            all_vals = {**amazon_vals, **shopify_vals, **packiyo_vals}
            # save_credentials already filters out empty values
            save_env(all_vals)
            st.success("Credentials saved! (only non-empty fields were updated)")
            st.rerun()

    with col_oauth:
        if st.button("Connect Shopify", type="secondary"):
            # save_credentials already filters out empty values
            save_env(shopify_vals)
            from etl.shopify_oauth import get_access_token
            with st.spinner("Requesting access token from Shopify..."):
                token, error = get_access_token()
            if token:
                st.success("Shopify connected! Access token saved.")
                st.rerun()
            else:
                st.error(f"Connection failed: {error}")

    # --- Status ---
    st.divider()
    st.subheader("Connection Status")
    col1, col2, col3 = st.columns(3)
    with col1:
        has_amazon = all(getattr(config, k, "") for k in [
            "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
        ])
        if has_amazon:
            st.success("Amazon SP-API: Configured")
        else:
            missing = [k.replace("AMAZON_", "") for k in [
                "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
            ] if not getattr(config, k, "")]
            st.warning(f"Amazon SP-API: Missing {', '.join(missing)}")
    with col2:
        has_shopify = bool(getattr(config, "SHOPIFY_ACCESS_TOKEN", ""))
        if has_shopify:
            st.success("Shopify Admin API: Connected")
        else:
            has_creds = all(getattr(config, k, "") for k in [
                "SHOPIFY_STORE_URL", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
            ])
            if has_creds:
                st.warning("Shopify: Credentials saved, click **Authorize Shopify** to connect")
            else:
                st.warning("Shopify Admin API: Not configured")
    with col3:
        if getattr(config, "PACKIYO_API_TOKEN", ""):
            st.success("Packiyo 3PL: Configured")
        else:
            st.warning("Packiyo 3PL: Not configured")

    st.info("Currently using **live API data**.")

    # --- Model Run Status ---
    st.divider()
    st.subheader("Analytics Model Status")
    st.caption("Daily analytics models run automatically after each ETL sync. You can also trigger them manually.")

    with get_db() as _mr_conn:
        _model_runs = get_model_runs(_mr_conn)

    if _model_runs:
        _mr_rows = []
        _status_icons = {'success': '\u2705', 'error': '\u274C'}
        for name, run in sorted(_model_runs.items()):
            icon = _status_icons.get(run.get('status', ''), '\u2753')
            last_at = run.get('last_run_at', '')
            if last_at:
                try:
                    _dt = datetime.strptime(last_at[:19], '%Y-%m-%d %H:%M:%S')
                    _delta = datetime.utcnow() - _dt
                    _secs = _delta.total_seconds()
                    if _secs < 3600:
                        ago = f'{max(1, int(_secs // 60))}m ago'
                    elif _secs < 86400:
                        ago = f'{int(_secs // 3600)}h ago'
                    else:
                        ago = f'{_delta.days}d ago'
                    last_at = f'{last_at[:16]} ({ago})'
                except (ValueError, TypeError):
                    pass
            dur = run.get('duration_seconds')
            dur_str = f'{dur:.1f}s' if dur else '-'
            _mr_rows.append({
                'Status': icon,
                'Model': name.replace('_', ' ').title(),
                'Last Run': last_at,
                'Duration': dur_str,
                'Triggered By': (run.get('triggered_by') or '-').replace('_', ' ').title(),
                'Error': run.get('error_message') or '',
            })
        _mr_df = pd.DataFrame(_mr_rows)
        render_html_table(_mr_df)
    else:
        st.caption("No model runs recorded yet. Models will run automatically after the next data sync.")

    _mr_col1, _mr_col2 = st.columns(2)
    with _mr_col1:
        if st.button("Run All Models Now", key="run_all_models"):
            with st.spinner("Running analytics models..."):
                from analytics.orchestrator import run_all_daily_models
                _results = run_all_daily_models(triggered_by='manual')
            _ok = sum(1 for s in _results.values() if s == 'success')
            if _ok == len(_results):
                st.success(f"All {_ok} models completed successfully.")
            else:
                _errs = [k for k, v in _results.items() if v != 'success']
                st.warning(f"{_ok}/{len(_results)} models succeeded. Failed: {', '.join(_errs)}")
            st.rerun()

    # --- Backfill Full History ---
    st.divider()
    st.subheader("Data Management")

    with get_db() as conn:
        order_range = conn.execute(
            "SELECT MIN(order_date) as min_d, MAX(order_date) as max_d, COUNT(*) as cnt FROM orders"
        ).fetchone()
        cust_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]

    if order_range and order_range["cnt"] > 0:
        st.markdown(
            f"**Current data:** {order_range['cnt']:,} orders from "
            f"**{order_range['min_d'][:10]}** to **{order_range['max_d'][:10]}** "
            f"| {cust_count:,} customers"
        )
    else:
        st.markdown("**No order data loaded yet.**")

    st.caption(
        "Backfill pulls your full order history (up to 5 years) from Shopify using bulk export. "
        "This improves forecast accuracy by correctly identifying each customer's true first order date. "
        "Existing data is preserved — duplicates are handled automatically."
    )

    if st.button("Backfill Full Order History", type="secondary"):
        from etl.shopify_bulk_import import run_bulk_backfill

        backfill_progress = st.progress(0, text="Starting bulk export from Shopify...")
        backfill_status = st.empty()

        def _on_backfill_progress(imported, total):
            pct = min(imported / max(total, 1), 1.0)
            backfill_progress.progress(pct, text=f"Importing orders: {imported:,} / {total:,}")

        try:
            count = run_bulk_backfill(on_progress=_on_backfill_progress)
            backfill_progress.empty()
            backfill_status.success(f"Backfill complete! {count:,} orders imported.")
            clear_waterfall_cache()
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            backfill_progress.empty()
            backfill_status.error(f"Backfill failed: {e}")

    # --- Amazon Revenue CSV Upload ---
    st.divider()
    st.subheader("Amazon Order Import")
    st.caption(
        "Upload Amazon order-level data (from Seller Central or cohort exports). "
        "Supports both order-level CSVs (with Order ID, SKU, Item Price) and daily summary CSVs. "
        "Order data will populate sales forecasts, customer retention, and revenue tracking."
    )

    amazon_upload = st.file_uploader(
        "Upload Amazon Sales CSV/Excel", type=["csv", "xlsx", "xls"], key="amazon_rev_upload",
    )
    if amazon_upload is not None:
        try:
            if amazon_upload.name.endswith((".xlsx", ".xls")):
                amz_df = pd.read_excel(amazon_upload)
            else:
                amz_df = pd.read_csv(amazon_upload, low_memory=False)

            # Detect format: order-level (has Order Id) vs daily summary
            is_order_level = any("order id" in c.lower() for c in amz_df.columns)

            if is_order_level:
                # --- ORDER-LEVEL FORMAT (e.g. Amazon Cohort MASTER file) ---
                st.info(f"Detected **order-level** format: {len(amz_df):,} line items.")

                # Auto-detect columns
                _order_col = next((c for c in amz_df.columns if "amazon order id" in c.lower()), None)
                _sku_col = next((c for c in amz_df.columns if "merchant sku" in c.lower() or c.lower() == "sku"), None)
                _date_col = next((c for c in amz_df.columns if "purchase date" in c.lower()), None)
                _price_col = next((c for c in amz_df.columns if "item price" in c.lower()), None)
                _qty_col = next((c for c in amz_df.columns if "shipped quantity" in c.lower() or "quantity" in c.lower()), None)
                _title_col = next((c for c in amz_df.columns if "title" in c.lower()), None)
                _email_col = next((c for c in amz_df.columns if "buyer email" in c.lower()), None)

                if not all([_order_col, _sku_col, _date_col, _price_col, _qty_col]):
                    st.error("Could not detect required columns (Order ID, SKU, Date, Price, Quantity).")
                else:
                    # Preview
                    preview_cols = [c for c in [_date_col, _order_col, _sku_col, _qty_col, _price_col, _title_col] if c]
                    render_html_table(amz_df[preview_cols].head(10))

                    _amz_unique_orders = amz_df[_order_col].nunique()
                    _amz_unique_skus = amz_df[_sku_col].nunique()
                    _amz_total_rev = amz_df[_price_col].sum()
                    ac1, ac2, ac3 = st.columns(3)
                    ac1.metric("Orders", f"{_amz_unique_orders:,}")
                    ac2.metric("Unique SKUs", f"{_amz_unique_skus}")
                    ac3.metric("Total Revenue", f"${_amz_total_rev:,.0f}")

                    if st.button("Import", key="import_amz_rev", type="primary"):
                        import hashlib
                        _amz_progress = st.progress(0, text="Importing Amazon orders...")
                        with get_db() as conn:
                            imported_orders = 0
                            imported_items = 0
                            imported_customers = 0
                            total_rows = len(amz_df)

                            for idx, (_, arow) in enumerate(amz_df.iterrows()):
                                try:
                                    _order_id = f"amz-{arow[_order_col]}"
                                    _sku = str(arow[_sku_col]).strip()
                                    _raw_date = str(arow[_date_col])
                                    _price = float(arow[_price_col]) if pd.notna(arow[_price_col]) else 0
                                    _qty = int(float(arow[_qty_col])) if pd.notna(arow[_qty_col]) else 1
                                    _title = str(arow[_title_col]) if _title_col and pd.notna(arow.get(_title_col)) else ""
                                    _email = str(arow[_email_col]) if _email_col and pd.notna(arow.get(_email_col)) else ""

                                    if not _sku or _qty == 0:
                                        continue

                                    # Parse date (handles ISO format and M/D/YYYY)
                                    _date_str = pd.Timestamp(_raw_date).strftime("%Y-%m-%d")

                                    # Customer (hash email for privacy)
                                    if _email:
                                        _cust_id = f"amz-{hashlib.md5(_email.encode()).hexdigest()[:12]}"
                                        upsert_customer(conn, _cust_id, _email, "amazon", _date_str)
                                        imported_customers += 1
                                    else:
                                        _cust_id = None

                                    # Order
                                    upsert_order(conn, _order_id, "amazon", str(arow[_order_col]),
                                                 _cust_id, _date_str, _price)
                                    imported_orders += 1

                                    # Order item
                                    _unit_price = _price / _qty if _qty > 0 else _price
                                    upsert_order_item(conn, _order_id, _sku, _title, _qty, _unit_price)
                                    imported_items += 1

                                    # SKU master
                                    upsert_sku(conn, _sku, _title, None, _date_str, "amazon")

                                except (ValueError, KeyError, TypeError):
                                    continue

                                if idx % 1000 == 0:
                                    _amz_progress.progress(
                                        min(idx / total_rows, 1.0),
                                        text=f"Importing: {idx:,} / {total_rows:,} rows..."
                                    )

                            # Aggregate into daily_sku_sales
                            _amz_progress.progress(0.95, text="Building daily sales aggregates...")

                            # Build Amazon daily sales from the imported order_items
                            conn.execute("DELETE FROM daily_sku_sales WHERE source = 'amazon'")
                            conn.execute("""
                                INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
                                SELECT
                                    DATE(o.order_date) as sale_date,
                                    oi.sku,
                                    'amazon' as source,
                                    SUM(oi.quantity) as units_sold,
                                    SUM(oi.total_price) as revenue,
                                    COUNT(DISTINCT o.order_id) as order_count
                                FROM order_items oi
                                JOIN orders o ON oi.order_id = o.order_id
                                WHERE o.source = 'amazon' AND o.status = 'completed'
                                GROUP BY DATE(o.order_date), oi.sku
                            """)

                        _amz_progress.progress(1.0, text="Done!")
                        st.success(
                            f"Imported **{imported_orders:,}** Amazon orders, "
                            f"**{imported_items:,}** line items, "
                            f"**{imported_customers:,}** customers."
                        )
                        clear_waterfall_cache()
                        st.cache_data.clear()
                        st.rerun()

            else:
                # --- DAILY SUMMARY FORMAT ---
                st.info(f"Detected **daily summary** format: {len(amz_df):,} rows.")
                render_html_table(amz_df.head(10))

                _date_cols = [c for c in amz_df.columns if any(k in c.lower() for k in ["date", "day"])]
                _sku_cols = [c for c in amz_df.columns if any(k in c.lower() for k in ["sku", "asin", "child"])]
                _units_cols = [c for c in amz_df.columns if any(k in c.lower() for k in ["unit", "quantity", "qty"])]
                _rev_cols = [c for c in amz_df.columns if any(k in c.lower() for k in ["revenue", "sales", "amount", "ordered product sales"])]

                ac1, ac2, ac3, ac4 = st.columns(4)
                with ac1:
                    amz_date_col = st.selectbox("Date column", _date_cols if _date_cols else amz_df.columns.tolist(), key="amz_date_col")
                with ac2:
                    amz_sku_col = st.selectbox("SKU/ASIN column", _sku_cols if _sku_cols else amz_df.columns.tolist(), key="amz_sku_col")
                with ac3:
                    amz_units_col = st.selectbox("Units column", _units_cols if _units_cols else amz_df.columns.tolist(), key="amz_units_col")
                with ac4:
                    amz_rev_col = st.selectbox("Revenue column", _rev_cols if _rev_cols else amz_df.columns.tolist(), key="amz_rev_col")

                if st.button("Import", key="import_amz_rev", type="primary"):
                    with get_db() as conn:
                        imported_amz = 0
                        for _, arow in amz_df.iterrows():
                            try:
                                _d = str(arow[amz_date_col]).strip()[:10]
                                _sku = str(arow[amz_sku_col]).strip()
                                _units = int(float(str(arow[amz_units_col]).replace(",", "").replace("$", "")))
                                _rev = float(str(arow[amz_rev_col]).replace(",", "").replace("$", ""))
                                if not _sku or _units == 0:
                                    continue
                                conn.execute("""
                                    INSERT INTO daily_sku_sales (sale_date, sku, source, units_sold, revenue, order_count)
                                    VALUES (?, ?, 'amazon', ?, ?, 1)
                                    ON CONFLICT(sale_date, sku, source) DO UPDATE SET
                                        units_sold = excluded.units_sold,
                                        revenue = excluded.revenue
                                """, (_d, _sku, _units, _rev))
                                imported_amz += 1
                            except (ValueError, KeyError):
                                continue
                    st.success(f"Imported {imported_amz} Amazon daily sales records.")
                    clear_waterfall_cache()
                    st.cache_data.clear()
                    st.rerun()
        except Exception as e:
            st.error(f"Failed to parse file: {e}")

    # --- Google Sheets Integration ---
    st.divider()
    st.subheader("Google Sheets")
    st.caption("Import data from a Google Sheet's 'Daily Data' tab. The sheet must be shared as 'Anyone with the link can view'.")

    with get_db() as conn:
        _gs_sheet_id = get_setting(conn, "google_sheet_id", "1GE_upVue9Fh4okDs7s3qE1ae54DStY-H0K7MovgCcxk")
        _gs_gid = get_setting(conn, "google_sheet_gid", "1661449750")
        _gs_last_sync = get_setting(conn, "google_sheet_last_sync", "Never")

    gs_sheet_id = st.text_input("Sheet ID", value=_gs_sheet_id, key="gs_sheet_id",
                                 help="The long ID in the Google Sheets URL between /d/ and /edit")
    gs_gid = st.text_input("Tab GID", value=_gs_gid, key="gs_gid",
                            help="The gid= parameter in the URL for the specific tab")
    st.caption(f"Last sync: {_gs_last_sync}")

    gs_c1, gs_c2 = st.columns(2)
    with gs_c1:
        if st.button("Test Connection", key="gs_test"):
            from etl.google_sheets import fetch_daily_data_tab
            df = fetch_daily_data_tab(gs_sheet_id, gs_gid)
            if df.empty:
                st.error("Could not fetch data. Make sure the sheet is shared as 'Anyone with the link can view'.")
            else:
                st.success(f"Connected! Found {len(df)} rows, {len(df.columns)} columns.")
                render_html_table(df.head(5))
    with gs_c2:
        if st.button("Sync Now", key="gs_sync", type="primary"):
            from etl.google_sheets import sync_google_sheet, sync_amazon_rollup
            with get_db() as conn:
                set_setting(conn, "google_sheet_id", gs_sheet_id)
                set_setting(conn, "google_sheet_gid", gs_gid)
                result = sync_google_sheet(conn, gs_sheet_id, gs_gid)
                if result > 0:
                    set_setting(conn, "google_sheet_last_sync",
                                datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
                    st.success(f"Synced {result} rows from Google Sheet.")
                else:
                    st.error("Sync failed. Check that the sheet is publicly accessible.")
                # Also sync Amazon Roll Up Date tab
                amz_result = sync_amazon_rollup(conn)
                if amz_result > 0:
                    st.success(f"Synced {amz_result} rows from Amazon Roll Up Date.")
                else:
                    st.warning("Amazon Roll Up Date sync returned no data.")

    # --- Klaviyo ---
    st.divider()
    st.subheader("Klaviyo API")
    st.caption("Pull email campaign metrics and revenue attribution from Klaviyo.")

    with get_db() as conn:
        _klaviyo_key = get_setting(conn, "klaviyo_api_key", "")

    klaviyo_key = st.text_input("Klaviyo Private API Key", value=_klaviyo_key,
                                 type="password", key="klaviyo_key",
                                 help="From Klaviyo -> Settings -> API Keys -> Create Private API Key")
    if st.button("Save", key="save_klaviyo"):
        with get_db() as conn:
            set_setting(conn, "klaviyo_api_key", klaviyo_key)
        st.success("Klaviyo API key saved.")
        st.rerun()

    if _klaviyo_key:
        with get_db() as conn:
            _kl_last_sync = conn.execute(
                "SELECT created_at FROM sync_log WHERE source = 'klaviyo' "
                "AND status = 'success' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            _kl_metric_id = get_setting(conn, "klaviyo_conversion_metric_id", "")
        if _kl_last_sync:
            st.success(f"Klaviyo connected. Last sync: {_kl_last_sync['created_at']}")
        else:
            st.info("Klaviyo connected. Data will sync on next scheduled run, or refresh from the Marketing page.")
        st.text_input(
            "Conversion Metric ID (Placed Order)",
            value=_kl_metric_id,
            key="klaviyo_metric_id",
            help="Required for revenue metrics. Auto-detected on first sync, or paste from Klaviyo.",
        )
        if st.button("Save Metric ID", key="save_klaviyo_metric"):
            with get_db() as conn:
                set_setting(conn, "klaviyo_conversion_metric_id",
                            st.session_state.get("klaviyo_metric_id", ""))
            st.success("Conversion metric ID saved.")
            st.rerun()
    else:
        st.caption("Enter your API key above to enable Klaviyo integration.")

    # --- Postscript ---
    st.divider()
    st.subheader("Postscript (SMS)")
    st.warning(
        "**Postscript's API does not expose campaign analytics or revenue attribution.** "
        "It only supports subscriber management. For SMS campaign performance data, "
        "export directly from the Postscript dashboard or track via Google Analytics UTM parameters."
    )

    # --- Bank / Ramp Transactions ---
    st.divider()
    st.subheader("Bank & Card Transactions")
    st.caption(
        "Import bank or credit card transaction data via CSV. "
        "Supports Ramp and Highbeam export formats. Internal transfers, CC statement payments, "
        "and Ramp-paid vendor bills are automatically flagged to prevent double-counting."
    )

    uploaded_csv = st.file_uploader("Upload Transaction CSV", type=["csv"], key="highbeam_csv")
    if uploaded_csv is not None:
        try:
            bank_df = pd.read_csv(uploaded_csv)
            st.success(f"Loaded {len(bank_df)} rows from CSV.")

            # Detect format
            is_ramp = "Merchant Name" in bank_df.columns or "Ramp Category" in bank_df.columns
            is_highbeam = "Direction" in bank_df.columns and "Summary" in bank_df.columns

            if is_ramp:
                st.info("Detected **Ramp** CSV format.")
                if "Type" in bank_df.columns:
                    transfers = bank_df["Type"].str.contains("Transfer", case=False, na=False)
                    st.caption(f"Filtering out {transfers.sum()} internal transfers.")
                    bank_df = bank_df[~transfers].copy()
                preview_cols = [c for c in ["Transaction Date", "Amount", "Merchant Name", "Ramp Category", "Ramp Department"] if c in bank_df.columns]
                render_html_table(bank_df[preview_cols].head(10))

            elif is_highbeam:
                st.info("Detected **Highbeam** CSV format.")

                def _classify_hb(row):
                    s = str(row.get("Summary", "")).lower()
                    d = row.get("Direction", "")
                    if "internal transfer" in s:
                        return "Internal Transfer", True
                    if "highbeam" in s and "transfer" in s:
                        return "Internal Transfer", True
                    if "ramp statement" in s:
                        return "CC Payment (Ramp)", True  # Already itemized in Ramp data
                    if "via ramp" in s:
                        return "Vendor (via Ramp)", True  # Already itemized in Ramp data
                    if d == "Credit":
                        if any(k in s for k in ["shopify", "stripe", "paypal", "braintree"]):
                            return "Revenue Payout", False
                        if "amazon" in s:
                            return "Amazon Payout", False
                        return "Other Income", False
                    # Debits
                    if any(k in s for k in ["tax", "revenue", "dept", "treasury", "dor ", "dor|"]):
                        return "Sales Tax", False
                    if any(k in s for k in ["stikpak", "avenue shops"]):
                        return "Manufacturing / COGS", False
                    if any(k in s for k in ["unishippers", "ups", "fedex", "usps", "dhl"]):
                        return "Shipping", False
                    return "Operating Expense", False

                bank_df[["_hb_cat", "_hb_exclude"]] = bank_df.apply(
                    lambda r: pd.Series(_classify_hb(r)), axis=1
                )
                excluded = bank_df["_hb_exclude"].sum()
                st.caption(
                    f"Auto-classified: {excluded} rows flagged as non-cash-flow "
                    f"(internal transfers, Ramp CC payments, Ramp-paid vendor bills)."
                )
                render_html_table(
                    bank_df[["Date", "Direction", "Amount", "Summary", "_hb_cat"]].head(15)
                )
            else:
                render_html_table(bank_df.head(10))

            if st.button("Import", key="import_bank", type="primary"):
                with get_db() as conn:
                    conn.execute("DROP TABLE IF EXISTS bank_transactions")
                    conn.execute("""
                        CREATE TABLE bank_transactions (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            date TEXT NOT NULL,
                            description TEXT,
                            merchant TEXT,
                            amount REAL NOT NULL,
                            direction TEXT DEFAULT 'debit',
                            category TEXT DEFAULT 'Uncategorized',
                            department TEXT,
                            user_name TEXT,
                            is_transfer INTEGER DEFAULT 0,
                            is_ramp_duplicate INTEGER DEFAULT 0,
                            source TEXT DEFAULT 'unknown',
                            created_at TEXT DEFAULT CURRENT_TIMESTAMP
                        )
                    """)

                    imported = 0
                    for _, row in bank_df.iterrows():
                        try:
                            if is_ramp:
                                _date = str(row.get("Transaction Date", row.get("Clearing Date", "")))
                                _desc = str(row.get("Merchant Description", ""))
                                _merchant = str(row.get("Merchant Name", ""))
                                _amt = float(str(row.get("Amount", 0)).replace(",", "").replace("$", ""))
                                _cat = str(row.get("Ramp Category", row.get("Accounting Category", "Uncategorized")))
                                _dept = str(row.get("Ramp Department", ""))
                                _user = str(row.get("User", ""))
                                _is_transfer = 1 if "transfer" in str(row.get("Type", "")).lower() else 0
                                _direction = "debit"
                                _is_ramp_dup = 0
                                _source = "ramp"
                            elif is_highbeam:
                                _raw_date = str(row.get("Date", ""))
                                _date = pd.Timestamp(_raw_date).strftime("%Y-%m-%d") if _raw_date else ""
                                _desc = str(row.get("Summary", ""))
                                _merchant = _desc.split("|")[0].strip() if "|" in _desc else _desc[:40]
                                _amt = float(str(row.get("Amount", 0)).replace(",", "").replace("$", ""))
                                _direction = str(row.get("Direction", "")).lower()
                                _cat = row.get("_hb_cat", "Uncategorized")
                                _dept = ""
                                _user = ""
                                _is_transfer = 1 if _cat == "Internal Transfer" else 0
                                _is_ramp_dup = 1 if _cat in ("CC Payment (Ramp)", "Vendor (via Ramp)") else 0
                                _source = "highbeam"
                            else:
                                _date_col = next((c for c in bank_df.columns if "date" in c.lower()), bank_df.columns[0])
                                _desc_col = next((c for c in bank_df.columns if "desc" in c.lower() or "memo" in c.lower() or "summary" in c.lower()), bank_df.columns[min(1, len(bank_df.columns)-1)])
                                _amt_col = next((c for c in bank_df.columns if "amount" in c.lower()), bank_df.columns[min(2, len(bank_df.columns)-1)])
                                _date = str(row[_date_col])
                                _desc = str(row[_desc_col])
                                _merchant = _desc
                                _amt = float(str(row[_amt_col]).replace(",", "").replace("$", ""))
                                _direction = "debit"
                                _cat = "Uncategorized"
                                _dept = ""
                                _user = ""
                                _is_transfer = 0
                                _is_ramp_dup = 0
                                _source = "other"

                            conn.execute("""
                                INSERT INTO bank_transactions (date, description, merchant, amount, direction, category, department, user_name, is_transfer, is_ramp_duplicate, source)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (_date, _desc, _merchant, _amt, _direction, _cat, _dept, _user, _is_transfer, _is_ramp_dup, _source))
                            imported += 1
                        except (ValueError, KeyError):
                            continue

                st.success(f"Imported {imported} transactions.")
                st.rerun()
        except Exception as e:
            st.error(f"Failed to parse CSV: {e}")
