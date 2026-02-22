# Integrations

## External APIs

### Amazon SP-API (Selling Partner API)

**Data pulled**: Daily sales by ASIN, FBA inventory levels, fulfillment data
**Auth**: OAuth 2.0 (Login with Amazon) — refresh token auto-managed by library
**Key files**:

- `etl/amazon.py` — Sales reports, flat-file orders, fulfillment data
- `etl/amazon_inventory.py` — FBA inventory real-time query
- `etl/amazon_sku_map.py` — ASIN ↔ Master SKU mapping (23 entries)

**Data destination**: `daily_sku_sales` (source='amazon') — skips `orders` table entirely
**Sync frequency**: Daily (scheduler daemon) or on-demand

### Shopify Admin API

**Data pulled**: Orders, customers, line items
**Auth**: OAuth 2.0 Client Credentials — 24-hour token expiry, auto-refreshed
**Key files**:

- `etl/shopify_client.py` — Order/customer sync + token refresh
- `etl/shopify_oauth.py` — Token acquisition flow
- `etl/shopify_bulk_import.py` — Bulk operations handler

**Data destination**: `customers` → `orders` → `order_items` → rebuilt into `daily_sku_sales` (source='shopify')
**Sync frequency**: Daily or on-demand

### Packiyo 3PL API

**Data pulled**: Real-time warehouse inventory (on hand, allocated, available, inbound)
**Auth**: Bearer token (static, manual rotation)
**Key files**: `etl/packiyo_client.py` — Inventory query + connection test

**Data destination**: In-memory only (displayed on Reorder Alerts page, not persisted)
**Sync frequency**: On-demand (queried on page load)

### Google Sheets

**Data pulled**: Custom metrics via public CSV export (daily data, Amazon rollup)
**Auth**: None (public sharing link)
**Key files**: `etl/google_sheets.py` — CSV fetch + sync

**Data destination**: `google_sheet_data` table, `amazon_daily_rollup` table

### Klaviyo (Configured, Not Active)

**Status**: Skeleton only in `etl/klaviyo_client.py` — no active sync

## Database

### SQLite (Primary Store)

**Location**: `data/inventory.db` (gitignored, persistent on Railway /data volume)
**Mode**: WAL (Write-Ahead Logging) for concurrent reads

**Schema (12+ tables)**:

| Table                     | Purpose                    | Primary Key                        |
| ------------------------- | -------------------------- | ---------------------------------- |
| `customers`               | Customer records           | `customer_id`                      |
| `orders`                  | Shopify order headers      | `order_id`                         |
| `order_items`             | Line items per order       | `id` (auto), UNIQUE(order_id, sku) |
| `sku_master`              | Product catalog            | `sku`                              |
| `daily_sku_sales`         | Aggregated daily sales     | (sale_date, sku, source)           |
| `media_spend`             | Monthly ad spend + ROAS    | (month, source)                    |
| `amazon_revenue_forecast` | Revenue targets            | `month`                            |
| `planned_inbound`         | User-entered POs           | (sku, month)                       |
| `seasonal_indices`        | Monthly demand multipliers | `month_num`                        |
| `app_settings`            | Key-value config store     | `key`                              |
| `sync_log`                | ETL sync history           | `id` (auto)                        |

**Connection pattern** (`db.py`):

```python
with get_db() as conn:
    # WAL mode, foreign keys enabled, row factory
    # Auto-commits on success, rolls back on exception
```

**Seed data**: `data/seed.sql.gz` (Git LFS, 12+ months real data, auto-restored on first deploy)

## Auth Providers

| Provider   | Flow                         | Token Lifetime                  | Refresh                | Storage           |
| ---------- | ---------------------------- | ------------------------------- | ---------------------- | ----------------- |
| Amazon LWA | Authorization Code → Refresh | Access: 1hr, Refresh: perpetual | Automatic (library)    | `app_settings` DB |
| Shopify    | Client Credentials           | Access: 24hr                    | Auto on 401 or sync    | `app_settings` DB |
| Packiyo    | Bearer Token                 | Static                          | Manual (Settings page) | `app_settings` DB |

**Why SQLite for credentials**: Railway redeploys delete the container (including .env), but `/data` volume persists. Credentials survive because `config.py` loads from DB at startup.

## Data Flow

```
EXTERNAL APIs              ETL                     DATABASE              ANALYTICS          DASHBOARD
──────────────────────────────────────────────────────────────────────────────────────────────────────

Amazon SP-API ──────► amazon.py ──────────────► daily_sku_sales ─┐
  (flat-file, 30d)    map ASIN→Master SKU       (source='amazon') │
                                                                    ├──► forecast.py (Prophet)
Shopify Admin ──────► shopify_client.py ──────► customers     ────┤
  (/orders.json)      fetch_orders()          ► orders            ├──► waterfall.py (demand split)
                                              ► order_items       │
                                              ► daily_sku_sales   ├──► retention.py (cohort)
                                               (source='shopify') │
Packiyo 3PL ────────► packiyo_client.py ──────► [real-time only]──┤
  (inventory)          get_inventory()                             ├──► reorder.py (runway sim)
                                                                    │
FBA Inventory ──────► amazon_inventory.py ────► [real-time only]──┘    ──► dashboard.py
                                                                            (10 pages)
Google Sheets ──────► google_sheets.py ───────► google_sheet_data
  (public CSV)

Manual Input ───────► Dashboard Settings ─────► media_spend
                                               ► seasonal_indices
                                               ► planned_inbound
                                               ► app_settings
```

**Key rules**:

1. Amazon data → directly to `daily_sku_sales` (no customer-level detail)
2. Shopify data → `orders` → `order_items` → rebuilt into `daily_sku_sales` via `rebuild_daily_sales()`
3. Packiyo/FBA inventory → real-time only, never persisted
4. Auto-sync triggers if last sync > 24 hours ago on page load
