"""
Configuration for Inventory Demand Forecasting System.
Reads API credentials from .env file, then overlays any values
persisted in the app_settings DB table (which survives Railway redeploys).
"""
BUILD_VERSION = "1.0.0"
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# --- Paths ---
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Allow override for Railway volume mount (e.g., DATABASE_PATH=/data/inventory.db)
_db_override = os.getenv("DATABASE_PATH")
if _db_override:
    DB_PATH = Path(_db_override)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
else:
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

# --- General Application ---
DATABASE_URL = os.getenv("DATABASE_URL", "")  # PostgreSQL URL; empty = use local SQLite
API_KEY = os.getenv("API_KEY", "")  # Optional bearer token for health endpoint auth
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

# --- Scheduler ---
SYNC_HOUR = int(os.getenv("SYNC_HOUR", "5"))  # Daily sync at 5 AM PST
SYNC_MINUTE = int(os.getenv("SYNC_MINUTE", "0"))
SYNC_TIMEZONE = os.getenv("SYNC_TIMEZONE", "America/Los_Angeles")  # PST/PDT


# --- All configurable keys for the Settings UI ---
# These are the credential keys that can be edited via the dashboard and
# persisted in the app_settings DB table (survives Railway redeploys).
CREDENTIAL_KEYS = {
    "AMAZON_REFRESH_TOKEN": "Amazon Refresh Token",
    "AMAZON_LWA_CLIENT_ID": "Amazon LWA Client ID",
    "AMAZON_LWA_CLIENT_SECRET": "Amazon LWA Client Secret",
    "AMAZON_MARKETPLACE_ID": "Amazon Marketplace ID",
    "SHOPIFY_STORE_URL": "Shopify Store URL",
    "SHOPIFY_CLIENT_ID": "Shopify Client ID",
    "SHOPIFY_CLIENT_SECRET": "Shopify Client Secret",
    "SHOPIFY_ACCESS_TOKEN": "Shopify Access Token (auto-filled after OAuth)",
    "SHOPIFY_API_VERSION": "Shopify API Version",
    "PACKIYO_API_TOKEN": "Packiyo API Token",
    "PACKIYO_API_URL": "Packiyo API URL",
    "PACKIYO_CUSTOMER_ID": "Packiyo Customer ID",
}

ENV_FILE = BASE_DIR / ".env"


def _load_credentials_from_db():
    """Overlay credential values from the persistent DB onto os.environ.

    This ensures credentials survive Railway container redeploys since the
    PostgreSQL DB persists independently of the app container.
    """
    try:
        from db import get_db
        with get_db() as conn:
            placeholders = ','.join('%s' for _ in CREDENTIAL_KEYS)
            rows = conn.execute(
                f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
                list(CREDENTIAL_KEYS.keys()),
            ).fetchall()
            for row in rows:
                if row["value"]:  # only override if DB has a non-empty value
                    os.environ[row["key"]] = row["value"]
    except Exception:
        pass  # DB may not exist yet on first run


def save_credentials(values: dict):
    """Persist credentials to both the DB (durable) and .env (local convenience).

    Only saves non-empty values to prevent accidental overwrites when
    Streamlit form fields render with empty defaults.
    """
    # Filter out empty/whitespace-only values to avoid wiping existing keys
    non_empty = {k: v for k, v in values.items() if v and str(v).strip()}
    if not non_empty:
        return

    # --- Write to DB (primary, persistent on Railway) ---
    try:
        from db import get_db, set_setting
        with get_db() as conn:
            for k, v in non_empty.items():
                set_setting(conn, k, v)
    except Exception:
        pass

    # --- Also write to .env for local dev convenience ---
    _save_env_file(non_empty)

    reload_config()


def save_env(values: dict):
    """Legacy wrapper — now saves to both DB and .env."""
    save_credentials(values)


def _save_env_file(values: dict):
    """Write key=value pairs to the .env file, preserving keys not in `values`."""
    existing = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, v = stripped.split("=", 1)
                existing[k.strip()] = v.strip()

    existing.update(values)

    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def reload_config():
    """Reload .env, then overlay DB credentials, and update module globals."""
    load_dotenv(ENV_FILE, override=True)
    _load_credentials_from_db()

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


# Load DB credentials on startup (after .env, so DB values take priority),
# then refresh module-level vars to pick up the overlaid values.
_load_credentials_from_db()
reload_config()
