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
HEALTH_STATUS_OK = 'ok'
HEALTH_STATUS_DB_ERROR = 'db_error'
HEALTH_CONTENT_TYPE = 'application/json'
HEALTH_DEFAULT_HOST = '0.0.0.0'
HEALTH_DEFAULT_PORT = 8502
HEALTH_STARTUP_MESSAGE = 'Health check server listening on'

# Input validation
MAX_INPUT_LENGTH = 500

# Items list endpoint
ITEMS_ENDPOINT_PATH = '/items'
ITEMS_DEFAULT_PAGE = 1
ITEMS_DEFAULT_PER_PAGE = 20
ITEMS_MAX_PER_PAGE = 100

# Data export endpoint
EXPORT_CSV_ENDPOINT_PATH = '/export/csv'
EXPORT_CSV_CONTENT_TYPE = 'text/csv'
