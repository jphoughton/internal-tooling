"""
Configuration for Inventory Demand Forecasting System.
Reads API credentials from .env file, sets DB path and defaults.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# --- Paths ---
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "inventory.db"

# --- Amazon SP-API ---
AMAZON_REFRESH_TOKEN = os.getenv("AMAZON_REFRESH_TOKEN", "")
AMAZON_LWA_CLIENT_ID = os.getenv("AMAZON_LWA_CLIENT_ID", "")
AMAZON_LWA_CLIENT_SECRET = os.getenv("AMAZON_LWA_CLIENT_SECRET", "")
AMAZON_AWS_ACCESS_KEY = os.getenv("AMAZON_AWS_ACCESS_KEY", "")
AMAZON_AWS_SECRET_KEY = os.getenv("AMAZON_AWS_SECRET_KEY", "")
AMAZON_MARKETPLACE_ID = os.getenv("AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER")  # US default
AMAZON_ROLE_ARN = os.getenv("AMAZON_ROLE_ARN", "")

# --- Shopify ---
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL", "")  # e.g. your-store.myshopify.com
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

# --- Packiyo 3PL ---
PACKIYO_API_URL = os.getenv("PACKIYO_API_URL", "https://aveshops.packiyo.com/api/v1")
PACKIYO_API_TOKEN = os.getenv("PACKIYO_API_TOKEN", "")
PACKIYO_CUSTOMER_ID = os.getenv("PACKIYO_CUSTOMER_ID", "12")

# --- Forecasting ---
FORECAST_HORIZON_DAYS = int(os.getenv("FORECAST_HORIZON_DAYS", "365"))
MIN_HISTORY_DAYS = int(os.getenv("MIN_HISTORY_DAYS", "60"))  # Minimum days for Prophet
LEAD_TIME_DAYS = int(os.getenv("LEAD_TIME_DAYS", "14"))  # Default reorder lead time
SAFETY_STOCK_MULTIPLIER = float(os.getenv("SAFETY_STOCK_MULTIPLIER", "1.5"))

# --- Scheduler ---
SYNC_HOUR = int(os.getenv("SYNC_HOUR", "6"))  # Daily sync at 6 AM
SYNC_MINUTE = int(os.getenv("SYNC_MINUTE", "0"))

# --- Data source mode ---
USE_MOCK_DATA = os.getenv("USE_MOCK_DATA", "true").lower() == "true"


# --- All configurable keys for the Settings UI ---
ENV_KEYS = {
    "AMAZON_REFRESH_TOKEN": "Amazon Refresh Token",
    "AMAZON_LWA_CLIENT_ID": "Amazon LWA Client ID",
    "AMAZON_LWA_CLIENT_SECRET": "Amazon LWA Client Secret",
    "AMAZON_MARKETPLACE_ID": "Amazon Marketplace ID",
    "SHOPIFY_STORE_URL": "Shopify Store URL",
    "SHOPIFY_CLIENT_ID": "Shopify Client ID",
    "SHOPIFY_CLIENT_SECRET": "Shopify Client Secret",
    "SHOPIFY_ACCESS_TOKEN": "Shopify Access Token (auto-filled after OAuth)",
    "SHOPIFY_API_VERSION": "Shopify API Version",
}

ENV_FILE = BASE_DIR / ".env"


def save_env(values: dict):
    """Write key=value pairs to the .env file, preserving keys not in `values`."""
    existing = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, v = stripped.split("=", 1)
                existing[k.strip()] = v.strip()

    existing.update(values)

    # Determine USE_MOCK_DATA based on whether any real credentials are set
    has_amazon = any(existing.get(k) for k in [
        "AMAZON_REFRESH_TOKEN", "AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET",
    ])
    has_shopify = any(existing.get(k) for k in [
        "SHOPIFY_ACCESS_TOKEN", "SHOPIFY_CLIENT_ID",
    ])
    if has_amazon or has_shopify:
        existing["USE_MOCK_DATA"] = "false"
    else:
        existing.setdefault("USE_MOCK_DATA", "true")

    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")

    # Reload into environment and module globals
    reload_config()


def reload_config():
    """Reload .env into os.environ and update module-level config vars."""
    load_dotenv(ENV_FILE, override=True)

    g = globals()
    g["AMAZON_REFRESH_TOKEN"] = os.getenv("AMAZON_REFRESH_TOKEN", "")
    g["AMAZON_LWA_CLIENT_ID"] = os.getenv("AMAZON_LWA_CLIENT_ID", "")
    g["AMAZON_LWA_CLIENT_SECRET"] = os.getenv("AMAZON_LWA_CLIENT_SECRET", "")
    g["AMAZON_AWS_ACCESS_KEY"] = os.getenv("AMAZON_AWS_ACCESS_KEY", "")
    g["AMAZON_AWS_SECRET_KEY"] = os.getenv("AMAZON_AWS_SECRET_KEY", "")
    g["AMAZON_MARKETPLACE_ID"] = os.getenv("AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER")
    g["AMAZON_ROLE_ARN"] = os.getenv("AMAZON_ROLE_ARN", "")
    g["SHOPIFY_STORE_URL"] = os.getenv("SHOPIFY_STORE_URL", "")
    g["SHOPIFY_ACCESS_TOKEN"] = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    g["SHOPIFY_CLIENT_ID"] = os.getenv("SHOPIFY_CLIENT_ID", "")
    g["SHOPIFY_CLIENT_SECRET"] = os.getenv("SHOPIFY_CLIENT_SECRET", "")
    g["SHOPIFY_API_VERSION"] = os.getenv("SHOPIFY_API_VERSION", "2024-01")
    g["PACKIYO_API_URL"] = os.getenv("PACKIYO_API_URL", "https://aveshops.packiyo.com/api/v1")
    g["PACKIYO_API_TOKEN"] = os.getenv("PACKIYO_API_TOKEN", "")
    g["PACKIYO_CUSTOMER_ID"] = os.getenv("PACKIYO_CUSTOMER_ID", "12")
    g["USE_MOCK_DATA"] = os.getenv("USE_MOCK_DATA", "true").lower() == "true"
