#!/bin/bash
# Quick start script for Inventory Demand Forecast system

set -e

echo "=== Inventory Demand Forecast ==="
echo ""

# Check for .env
if [ ! -f .env ]; then
    echo "No .env file found. Copying from .env.example..."
    cp .env.example .env
    echo "Edit .env with your API credentials, then re-run this script."
    echo ""
fi

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt --break-system-packages -q 2>/dev/null || pip install -r requirements.txt -q

# Restore from seed if no local DB exists
if [ ! -f data/inventory.db ] && [ -f data/seed.sql.gz ]; then
    echo "No local database found. Restoring from seed data..."
    mkdir -p data
    gunzip -c data/seed.sql.gz | sqlite3 data/inventory.db
    echo "Seed data restored ($(sqlite3 data/inventory.db 'SELECT COUNT(*) FROM orders') orders)"
fi

# Initialize DB (creates tables if missing)
echo "Initializing database..."
python -c "from db import init_db; init_db()"

# Generate mock data if in mock mode, or sync if live
echo ""
python -c "
from config import USE_MOCK_DATA
from db import get_db, get_connection
print(f'Mode: {\"mock\" if USE_MOCK_DATA else \"live\"}')

if USE_MOCK_DATA:
    print('Generating mock data...')
    from mock_data import generate_mock_data
    generate_mock_data()
else:
    from analytics.waterfall import get_configured_sources
    configured = get_configured_sources()
    print(f'Configured sources: {configured or \"none\"}')

    if not configured:
        print('No API sources configured. Go to Settings in the dashboard to connect.')
    else:
        conn = get_connection()
        mock_count = conn.execute(
            \"SELECT COUNT(*) FROM orders WHERE order_id LIKE 'ama-ord-%' OR order_id LIKE 'sho-ord-%'\"
        ).fetchone()[0]
        has_data = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
        conn.close()

        if mock_count > 0:
            print('Clearing mock data...')
            with get_db() as c:
                for t in ['daily_sku_sales', 'order_items', 'orders', 'customers', 'sku_master', 'sync_log']:
                    c.execute(f'DELETE FROM {t}')

        if mock_count > 0 or has_data == 0:
            if 'shopify' in configured:
                from etl.shopify_client import test_connection
                ok, msg = test_connection()
                print(f'Shopify connection: {msg}')
                if not ok:
                    print('Skipping sync due to connection error.')
                    print('Fix your credentials in Settings, then click Refresh Data.')
                else:
                    print('Running initial sync (this may take a few minutes)...')
                    from etl.sync import run_daily_sync
                    results = run_daily_sync(full_refresh=True)
                    if results:
                        for k, v in results.items():
                            status = 'OK' if not str(v).startswith('ERROR') else 'FAILED'
                            print(f'  {k}: {status} -- {v}')
            else:
                print('Running initial sync...')
                from etl.sync import run_daily_sync
                results = run_daily_sync(full_refresh=True)
                if results:
                    for k, v in results.items():
                        status = 'OK' if not str(v).startswith('ERROR') else 'FAILED'
                        print(f'  {k}: {status} -- {v}')
        else:
            print(f'Data already present ({has_data} orders) — skipping initial sync.')
            print('Use Refresh Data in the dashboard to pull new orders.')
"

# Launch dashboard
echo ""
echo "Starting dashboard at http://localhost:8501"
echo "Press Ctrl+C to stop."
echo ""
streamlit run dashboard.py --server.port 8501 --server.headless true
