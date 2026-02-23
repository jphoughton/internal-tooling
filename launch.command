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

# Init DB
echo ""
echo "Initializing database..."
./venv/bin/python -c "from db import init_db; init_db()"

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
