"""Centralized constants and magic numbers."""

# Core SKUs to include in forecast tables (17 Hydrant products)
FORECAST_SKUS = {
    "ENBP-BP0030-LEB0",
    "HYBP-BP0030-CHLB0",
    "HYBP-BP0030-GFB0",
    "HYBP-BP0030-ICLEB0",
    "HYBP-BP0050-NAB0",
    "SLBP-BP0030-CHB0",
    "ENPO-ST0030-RSLEB0",
    "HYPO-ST0030-BOB0",
    "HYPO-ST0030-FRPUB0",
    "HYPO-ST0030-LELMB0",
    "HYPO-ST0030-VPBFLB0",
    "IMPO-ST0030-ELB0",
    "NSPO-ST0030-BEB0",
    "NSPO-ST0030-LEB0",
    "NSPO-ST0030-VPWLBB0",
    "NSPO-ST0030-WALEB0",
    "SLPO-ST0030-ELB0",
}

# Triple Whale correction factor (TW overstates attribution ~2x)
TW_ADJUSTMENT = 0.5

# Default seasonal indices (hydration product seasonality)
DEFAULT_SEASONAL_INDICES = {
    1: 0.95, 2: 0.92, 3: 0.98, 4: 1.02, 5: 1.05, 6: 1.10,
    7: 1.12, 8: 1.08, 9: 1.02, 10: 0.98, 11: 0.92, 12: 0.88,
}

# Pacing tolerance bounds
PACING_ON_TRACK_LOW = 0.95
PACING_ON_TRACK_HIGH = 1.05

# FBA transfer default lead time
FBA_TRANSFER_LEAD_TIME_WEEKS = 4

# Health check server
HEALTH_ENDPOINT_PATH = '/health'
EXPORT_CSV_PATH = '/export/csv'
HEALTH_STATUS_OK = 'ok'
HEALTH_STATUS_DB_ERROR = 'db_error'
HEALTH_CONTENT_TYPE = 'application/json'
HEALTH_DEFAULT_HOST = '0.0.0.0'
HEALTH_DEFAULT_PORT = 8502
HEALTH_STARTUP_MESSAGE = 'Health check server listening on'

# Input validation
MAX_INPUT_LENGTH = 500

# Inventory items list endpoint
INVENTORY_ITEMS_ENDPOINT_PATH = '/items'
INVENTORY_ITEMS_DEFAULT_PAGE = 1
INVENTORY_ITEMS_DEFAULT_PER_PAGE = 20
INVENTORY_ITEMS_MAX_PER_PAGE = 100

# Rate limiting
RATE_LIMIT_REQUESTS = 60        # max requests per IP per window
RATE_LIMIT_WINDOW_SECONDS = 60  # window duration in seconds

# Startup validation
STARTUP_REQUIRED_ENV_VARS = ['DATABASE_URL']
STARTUP_OPTIONAL_INTEGRATIONS = {
    'Amazon': 'AMAZON_REFRESH_TOKEN',
    'Shopify': 'SHOPIFY_ACCESS_TOKEN',
    'Packiyo': 'PACKIYO_API_TOKEN',
    'Klaviyo': 'KLAVIYO_API_KEY',
}

# ---------------------------------------------------------------------------
# Cash Flow categories and projection defaults
# ---------------------------------------------------------------------------
CASHFLOW_CATEGORIES = {
    # Revenue (from P&L revenue model)
    'dtc_revenue': {'label': 'DTC Revenue', 'group': 'revenue', 'method': 'waterfall'},
    'amazon_revenue': {'label': 'Amazon Revenue', 'group': 'revenue', 'method': 'forecast_table'},
    'wholesale_revenue': {'label': 'Wholesale Revenue', 'group': 'revenue', 'method': 'monthly_spread_rev'},
    # Expenses (operating) — fixed from settings, variable from P&L
    'media': {'label': 'Media / Ads', 'group': 'expense', 'method': 'media_split'},
    'payroll': {'label': 'Payroll', 'group': 'expense', 'method': 'biweekly_alternating'},
    'fulfillment': {'label': 'Fulfillment / 3PL', 'group': 'expense', 'method': 'monthly_spread'},
    'sales_tax': {'label': 'Sales Tax', 'group': 'expense', 'method': 'sales_tax_pct'},
    'software': {'label': 'Software / SaaS', 'group': 'expense', 'method': 'monthly_week2'},
    'shipping': {'label': 'Shipping', 'group': 'expense', 'method': 'shipping_pct'},
    'agency': {'label': 'Marketing OpEx', 'group': 'expense', 'method': 'mktg_opex_split'},
    'accounting': {'label': 'Accounting / CPA', 'group': 'expense', 'method': 'monthly_lump'},
    'insurance': {'label': 'Insurance', 'group': 'expense', 'method': 'monthly_lump'},
    'consulting': {'label': 'Consulting', 'group': 'expense', 'method': 'monthly_lump'},
    'other_expense': {'label': 'Other Expense', 'group': 'expense', 'method': 'monthly_lump'},
    # COGS & Debt — outflows tracked separately from operating expenses
    'production': {'label': 'Production Runs', 'group': 'cogs_debt', 'method': 'po_based'},
    'loan': {'label': 'Loan Principal', 'group': 'cogs_debt', 'method': 'loan_schedule'},
    'loan_interest': {'label': 'Loan Interest', 'group': 'cogs_debt', 'method': 'loc_interest_eom'},
}

# Seed defaults for expense projections when no actuals exist.
# monthly_lump categories: monthly amount. Others: weekly amount.
CASHFLOW_SEED_DEFAULTS = {
    'media': 12500,        # ~$50K/mo weekly avg (used only as total fallback)
    'payroll': 13000,      # payroll_a default
    'loan': 30000,         # monthly principal default
    'loan_interest': 0,    # calculated from LOC balance
    'fulfillment': 7500,   # weekly fallback (~$32K/mo)
    'production': 0,       # PO-based only, no default
    'sales_tax': 13000,    # monthly lump from actuals avg
    'software': 10541,     # $10,540.56/mo
    'shipping': 2000,
    'agency': 13300,       # total marketing opex ($7K + $6.3K)
    'accounting': 2500,
    'insurance': 1900,
    'consulting': 0,       # consulting ended after Feb 2026
    'other_expense': 500,
}

# DTC payout day-of-week weights (0=Mon, 6=Sun)
DTC_DOW_WEIGHTS = {
    0: 0.157,  # Monday
    1: 0.309,  # Tuesday (weekend batch)
    2: 0.252,  # Wednesday
    3: 0.144,  # Thursday
    4: 0.137,  # Friday
    5: 0.0,    # Saturday (no payouts)
    6: 0.0,    # Sunday (no payouts)
}

# Confidence interval growth per week (simple fan-out)
CASHFLOW_CONFIDENCE_WEEKLY_GROWTH = 0.02  # 2% per week
CASHFLOW_CONFIDENCE_MAX = 0.30            # cap at 30%
