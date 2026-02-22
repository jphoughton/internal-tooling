# Testing

## Framework

No formal test framework is currently in place. There is no `tests/` directory, no pytest or unittest configuration, and no CI pipeline running tests.

## Test Structure

No test files exist. The codebase relies on manual testing through the Streamlit UI and mock data generation.

## Mocking

### `mock_data.py`

The primary "testing" mechanism is synthetic data generation:

- Generates 12 months of realistic Shopify + Amazon orders
- Creates customer cohorts with retention patterns
- Populates all core tables: `customers`, `orders`, `order_items`, `daily_sku_sales`
- Controlled by `USE_MOCK_DATA=true` in `.env`

```bash
python mock_data.py  # Generates test dataset
```

### `USE_MOCK_DATA` flag

When `true`, the app uses generated data instead of calling live APIs. This is the default for local development.

## Coverage

- **Unit tests**: None
- **Integration tests**: None
- **E2E tests**: None (Playwright MCP available but no automated test scripts)
- **Manual testing**: Via Streamlit UI with mock data

### Critical untested areas:

- Prophet forecasting logic (fallback to moving average, seasonality)
- Waterfall demand split (retention curve math, contamination detection)
- Reorder schedule simulation (runway, MOQ rounding, urgency tiers)
- Retention cohort calculations (recency weighting, decay)
- ETL data transformations (ASIN mapping, order rebuilding)
- Database upsert operations (idempotency, conflict resolution)

## How to Run Tests

Currently no test suite to run. When tests are added:

```bash
# Recommended setup
pip install pytest
pytest tests/ -v

# With mock data for integration tests
USE_MOCK_DATA=true pytest tests/ -v
```

### Recommended test infrastructure to add:

1. `pytest` as test framework
2. `tests/` directory with `test_<module>.py` files
3. In-memory SQLite (`:memory:`) for database tests
4. Mocked API responses for ETL tests
5. Pre-push hook to run tests (`npm test` in `.husky/pre-push`)
