"""
CLI entry point for the Hydrant Command Center.

Usage:
    python -m inventory serve          # Launch the Streamlit dashboard
    python -m inventory etl            # Run ETL sync immediately
    python -m inventory etl --full     # Full historical ETL refresh
    python -m inventory migrate        # Run database schema setup
"""
import argparse
import sys


def cmd_serve(args):
    """Launch the Streamlit dashboard."""
    import os
    import subprocess

    dashboard = os.path.join(os.path.dirname(__file__), 'dashboard.py')
    cmd = [sys.executable, '-m', 'streamlit', 'run', dashboard, '--server.port', str(args.port)]
    print(f'Starting dashboard on http://localhost:{args.port}')
    sys.exit(subprocess.call(cmd))


def cmd_etl(args):
    """Run ETL sync."""
    from etl.sync import run_daily_sync

    print('Running full historical refresh...' if args.full else 'Running incremental sync...')
    results = run_daily_sync(full_refresh=args.full)
    print(f'ETL complete: {results}')


def cmd_migrate(args):
    """Run database schema setup (create tables and seed defaults)."""
    from db import init_db

    print('Running database migration...')
    init_db()
    print('Migration complete.')


def main():
    parser = argparse.ArgumentParser(
        prog='inventory',
        description='Hydrant Command Center CLI',
    )
    subparsers = parser.add_subparsers(dest='command', metavar='COMMAND')
    subparsers.required = True

    # serve
    serve_parser = subparsers.add_parser('serve', help='Launch the Streamlit dashboard')
    serve_parser.add_argument(
        '--port', type=int, default=8501,
        help='Port to listen on (default: 8501)',
    )
    serve_parser.set_defaults(func=cmd_serve)

    # etl
    etl_parser = subparsers.add_parser('etl', help='Run ETL data sync')
    etl_parser.add_argument(
        '--full', action='store_true',
        help='Run full historical refresh instead of incremental sync',
    )
    etl_parser.set_defaults(func=cmd_etl)

    # migrate
    migrate_parser = subparsers.add_parser('migrate', help='Run database schema setup')
    migrate_parser.set_defaults(func=cmd_migrate)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
