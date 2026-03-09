"""
Daily health check: inspects sync_log, model_runs, and data quality.
Prints a summary of issues found. Exit code 0 = healthy, 1 = issues found.

Usage:
    python healthcheck.py          # Print report
    python healthcheck.py --fix    # Attempt auto-fixes (re-run failed syncs/models)
"""
import sys
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

EXPECTED_SYNC_SOURCES = ['amazon', 'shopify', 'google_sheet']
EXPECTED_MODELS = [
    'retention_cohorts', 'repeat_forecast', 'seasonality', 'sku_sales_mix',
    'waterfall', 'cashflow_forecast', 'global_alerts', 'retention_page_data',
    'page_stats', 'ops_page_data', 'pacing_precompute', 'overview_data_precompute',
]
STALE_THRESHOLD_HOURS = 48


def check_sync_health(conn):
    """Check sync_log for failed or stale syncs."""
    from db import read_sql
    issues = []

    # Get latest sync per source
    df = read_sql("""
        SELECT source, MAX(created_at) as last_sync, status, error_message
        FROM sync_log
        WHERE (source, created_at) IN (
            SELECT source, MAX(created_at) FROM sync_log GROUP BY source
        )
        GROUP BY source, status, error_message
    """, conn)

    if df.empty:
        issues.append('CRITICAL: No sync_log entries found at all')
        return issues, []

    synced_sources = set(df['source'].tolist())
    failed_sources = []

    for source in EXPECTED_SYNC_SOURCES:
        if source not in synced_sources:
            issues.append(f'WARNING: No sync records for source "{source}"')
            continue

        row = df[df['source'] == source].iloc[0]

        # Check for errors
        if row['status'] == 'error':
            msg = row.get('error_message', 'unknown')
            issues.append(f'ERROR: Last sync for "{source}" failed: {msg}')
            failed_sources.append(source)

        # Check staleness
        try:
            last_sync = datetime.fromisoformat(str(row['last_sync']).replace('Z', '+00:00').split('+')[0])
            age_hours = (datetime.now() - last_sync).total_seconds() / 3600
            if age_hours > STALE_THRESHOLD_HOURS:
                issues.append(f'WARNING: "{source}" last synced {age_hours:.0f}h ago (>{STALE_THRESHOLD_HOURS}h)')
        except (ValueError, TypeError):
            issues.append(f'WARNING: Could not parse last sync time for "{source}": {row["last_sync"]}')

    return issues, failed_sources


def check_model_health(conn):
    """Check model_runs for failed or stale models."""
    from db import get_model_runs
    issues = []
    failed_models = []

    model_runs = get_model_runs(conn)

    for model_name in EXPECTED_MODELS:
        if model_name not in model_runs:
            issues.append(f'WARNING: Model "{model_name}" has never run')
            failed_models.append(model_name)
            continue

        run = model_runs[model_name]

        if run['status'] == 'error':
            msg = run.get('error_message', 'unknown')
            issues.append(f'ERROR: Model "{model_name}" failed: {msg}')
            failed_models.append(model_name)

        # Check staleness
        try:
            last_run = datetime.fromisoformat(str(run['last_run_at']).replace('Z', '+00:00').split('+')[0])
            age_hours = (datetime.now() - last_run).total_seconds() / 3600
            if age_hours > STALE_THRESHOLD_HOURS:
                issues.append(f'WARNING: Model "{model_name}" last ran {age_hours:.0f}h ago')
        except (ValueError, TypeError):
            pass

    return issues, failed_models


def check_data_quality(conn):
    """Quick data quality checks on core tables."""
    from db import read_sql
    issues = []

    # Check for negative revenue
    df = read_sql("""
        SELECT COUNT(*) as cnt FROM daily_sku_sales WHERE revenue < 0
    """, conn)
    if not df.empty and df.iloc[0]['cnt'] > 0:
        issues.append(f'ERROR: {df.iloc[0]["cnt"]} rows with negative revenue in daily_sku_sales')

    # Check for future dates
    today = datetime.now().strftime('%Y-%m-%d')
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    df = read_sql("""
        SELECT COUNT(*) as cnt FROM daily_sku_sales WHERE sale_date > %s
    """, conn, params=(tomorrow,))
    if not df.empty and df.iloc[0]['cnt'] > 0:
        issues.append(f'ERROR: {df.iloc[0]["cnt"]} rows with future dates in daily_sku_sales')

    # Check orphaned order_items
    df = read_sql("""
        SELECT COUNT(*) as cnt FROM order_items oi
        LEFT JOIN orders o ON oi.order_id = o.order_id
        WHERE o.order_id IS NULL
    """, conn)
    if not df.empty and df.iloc[0]['cnt'] > 0:
        issues.append(f'WARNING: {df.iloc[0]["cnt"]} orphaned order_items (no matching order)')

    # Check row counts are non-zero
    for table in ['daily_sku_sales', 'orders', 'customers']:
        df = read_sql(f'SELECT COUNT(*) as cnt FROM {table}', conn)
        if df.empty or df.iloc[0]['cnt'] == 0:
            issues.append(f'CRITICAL: Table "{table}" is empty')

    return issues


def attempt_fixes(failed_sources, failed_models):
    """Re-run failed ETL syncs and models."""
    fixed = []

    if failed_sources:
        log.info('Re-running ETL sync to fix failed sources: %s', failed_sources)
        try:
            from etl.sync import run_daily_sync
            results = run_daily_sync()
            log.info('ETL sync results: %s', results)
            fixed.append(f'Re-ran ETL sync: {results}')
        except Exception as e:
            log.error('ETL re-sync failed: %s', e)
            fixed.append(f'ETL re-sync failed: {e}')

    if failed_models:
        log.info('Re-running failed models: %s', failed_models)
        try:
            from analytics.orchestrator import run_all_daily_models
            results = run_all_daily_models(triggered_by='healthcheck')
            log.info('Model re-run results: %s', results)
            fixed.append(f'Re-ran models: {results}')
        except Exception as e:
            log.error('Model re-run failed: %s', e)
            fixed.append(f'Model re-run failed: {e}')

    return fixed


def run_healthcheck(auto_fix=False):
    """Run all health checks and optionally attempt fixes."""
    from db import get_db

    print(f'\n{"="*60}')
    print(f'  HEALTH CHECK — {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'{"="*60}\n')

    all_issues = []
    failed_sources = []
    failed_models = []

    with get_db() as conn:
        # 1. Sync health
        print('Checking ETL syncs...')
        issues, fs = check_sync_health(conn)
        all_issues.extend(issues)
        failed_sources.extend(fs)

        # 2. Model health
        print('Checking model runs...')
        issues, fm = check_model_health(conn)
        all_issues.extend(issues)
        failed_models.extend(fm)

        # 3. Data quality
        print('Checking data quality...')
        issues = check_data_quality(conn)
        all_issues.extend(issues)

    # Print results
    print(f'\n{"—"*60}')
    if not all_issues:
        print('ALL CLEAR — no issues found')
        print(f'{"—"*60}\n')
        return 0

    print(f'FOUND {len(all_issues)} ISSUE(S):\n')
    for issue in all_issues:
        print(f'  • {issue}')
    print(f'\n{"—"*60}')

    # Auto-fix if requested
    if auto_fix and (failed_sources or failed_models):
        print('\nAttempting auto-fixes...\n')
        fixes = attempt_fixes(failed_sources, failed_models)
        for fix in fixes:
            print(f'  → {fix}')

        # Re-check after fixes
        print('\nRe-checking after fixes...')
        remaining = []
        with get_db() as conn:
            issues, _ = check_sync_health(conn)
            remaining.extend(issues)
            issues, _ = check_model_health(conn)
            remaining.extend(issues)

        if not remaining:
            print('ALL ISSUES RESOLVED')
            return 0
        else:
            print(f'{len(remaining)} issue(s) remain after fixes')
            return 1

    return 1


if __name__ == '__main__':
    auto_fix = '--fix' in sys.argv
    exit_code = run_healthcheck(auto_fix=auto_fix)
    sys.exit(exit_code)
