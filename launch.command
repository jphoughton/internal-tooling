#!/bin/bash
# ============================================
# Inventory Demand Forecast — Double-click to run
# ============================================
# Just double-click this file. It handles everything:
# installs dependencies, sets up data, opens the dashboard.

cd "$(dirname "$0")"

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    echo "Python 3 is required. Install it from https://python.org"
    echo "Press any key to exit."
    read -n 1
    exit 1
fi

# Create virtual environment if needed
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Always install/update packages
echo "Installing dependencies..."
./venv/bin/pip install -r requirements.txt -q

# Copy .env if needed
if [ ! -f ".env" ]; then
    cp .env.example .env
fi

# Init DB + set up data
echo ""
echo "Setting up data..."
./venv/bin/python -c "
from db import init_db, get_db, get_connection
init_db()
from config import USE_MOCK_DATA
print(f'  Mode: {\"mock\" if USE_MOCK_DATA else \"live\"}')

if USE_MOCK_DATA:
    print('  Generating mock data...')
    from mock_data import generate_mock_data
    generate_mock_data()
    print('  Mock data ready.')
else:
    # Check what sources are configured
    from analytics.waterfall import get_configured_sources
    configured = get_configured_sources()
    print(f'  Configured sources: {configured or \"none\"}')

    if not configured:
        print('  No API sources configured. Go to Settings in the dashboard to connect.')
    else:
        # Clear any leftover mock data
        conn = get_connection()
        mock_count = conn.execute(
            \"SELECT COUNT(*) FROM orders WHERE order_id LIKE 'ama-ord-%' OR order_id LIKE 'sho-ord-%'\"
        ).fetchone()[0]
        has_data = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
        conn.close()

        if mock_count > 0:
            print('  Clearing mock data...')
            with get_db() as c:
                for t in ['daily_sku_sales', 'order_items', 'orders', 'customers', 'sku_master', 'sync_log']:
                    c.execute(f'DELETE FROM {t}')

        if mock_count > 0 or has_data == 0:
            # Test connection first
            if 'shopify' in configured:
                from etl.shopify_client import test_connection
                ok, msg = test_connection()
                print(f'  Shopify connection: {msg}')
                if not ok:
                    print('  Skipping sync due to connection error.')
                    print('  Fix your credentials in Settings, then click Refresh Data.')
                else:
                    print('  Running initial sync (this may take a few minutes)...')
                    from etl.sync import run_daily_sync
                    results = run_daily_sync(full_refresh=True)
                    if results:
                        for k, v in results.items():
                            status = 'OK' if not str(v).startswith('ERROR') else 'FAILED'
                            print(f'  {k}: {status} — {v}')
            else:
                print('  Running initial sync...')
                from etl.sync import run_daily_sync
                results = run_daily_sync(full_refresh=True)
                if results:
                    for k, v in results.items():
                        status = 'OK' if not str(v).startswith('ERROR') else 'FAILED'
                        print(f'  {k}: {status} — {v}')
        else:
            print(f'  Data already present ({has_data} orders) — skipping initial sync.')
            print('  Use Refresh Data in the dashboard to pull new orders.')
"

# --- Launch ---
echo ""
echo "Starting dashboard at http://localhost:8501"
echo "Close this window to stop the dashboard."
echo ""

# Open browser after a short delay
(sleep 2 && open "http://localhost:8501") &

# Run dashboard
./venv/bin/streamlit run dashboard.py \
    --server.port 8501 \
    --server.headless true \
    --browser.gatherUsageStats false
