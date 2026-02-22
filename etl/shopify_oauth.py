"""
Shopify authentication for Dev Dashboard apps.
Uses Client Credentials Grant to obtain access tokens.
Tokens expire after 24 hours and are refreshed automatically.
"""
import requests
import config as cfg
from config import save_env


def get_access_token():
    """
    Obtain an access token using the Client Credentials Grant.
    This is the recommended approach for Shopify Dev Dashboard apps.

    Returns (access_token, error_message).
    """
    store = cfg.SHOPIFY_STORE_URL.rstrip("/")
    if not store.startswith("http"):
        store = f"https://{store}"

    client_id = cfg.SHOPIFY_CLIENT_ID
    client_secret = cfg.SHOPIFY_CLIENT_SECRET

    if not client_id:
        return None, "Shopify Client ID is required. Enter it in Settings."
    if not client_secret:
        return None, "Shopify Client Secret is required. Enter it in Settings."
    if not store:
        return None, "Shopify Store URL is required. Enter it in Settings."

    token_url = f"{store}/admin/oauth/access_token"

    try:
        resp = requests.post(token_url, json={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            token = data.get("access_token", "")
            if token:
                save_env({"SHOPIFY_ACCESS_TOKEN": token})
                return token, None
            else:
                return None, "Shopify returned 200 but no access token in response."
        elif resp.status_code == 401:
            return None, "Invalid Client ID or Client Secret. Check your credentials in the Shopify Dev Dashboard."
        elif resp.status_code == 403:
            return None, "Access forbidden. Make sure your app has the required API scopes configured."
        elif resp.status_code == 400:
            body = resp.text
            if "app_not_installed" in body:
                return None, (
                    "App is not installed on this store. "
                    "Go to Shopify Admin > Settings > Apps and sales channels > Develop apps, "
                    "select your app, and click 'Install app'. Then try again."
                )
            return None, f"Bad request ({resp.status_code}): {body[:200]}"
        else:
            return None, f"Token request failed ({resp.status_code}): {resp.text[:200]}"

    except requests.exceptions.ConnectionError:
        return None, f"Could not connect to {cfg.SHOPIFY_STORE_URL}. Check the store URL."
    except Exception as e:
        return None, f"Token request failed: {e}"
