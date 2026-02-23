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

# Initialize DB (creates tables if missing)
echo "Initializing database..."
python -c "from db import init_db; init_db()"

# Launch dashboard
echo ""
echo "Starting dashboard at http://localhost:8501"
echo "Press Ctrl+C to stop."
echo ""
streamlit run dashboard.py --server.port 8501 --server.headless true
