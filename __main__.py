"""
CLI entry point for the Hydrant Command Center dashboard.

Usage:
    python -m inventory serve              # Launch Streamlit dashboard
    python -m inventory etl                # Run ETL sync once and exit
    python -m inventory etl --full         # Full historical refresh
    python -m inventory etl --daemon       # Run in daemon mode (daily at configured time)
    python -m inventory migrate            # Create/update database schema
"""
import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parent


def cmd_serve(args):
    """Launch the Streamlit dashboard."""
    dashboard = _ROOT / 'dashboard.py'
    cmd = [sys.executable, '-m', 'streamlit', 'run', str(dashboard)]
    if args.port:
        cmd += ['--server.port', str(args.port)]
    sys.exit(subprocess.call(cmd))


def cmd_etl(args):
    """Run the ETL pipeline."""
    if args.daemon:
        from scheduler import run_daemon
        run_daemon()
    else:
        from etl.sync import run_daily_sync
        print('Running ETL sync...')
        results = run_daily_sync(full_refresh=args.full)
        print(f'ETL complete: {results}')


def cmd_migrate(args):
    """Initialise or migrate the database schema."""
    from db import init_db
    print('Running database migrations...')
    init_db()
    print('Migrations complete.')


def main():
    parser = argparse.ArgumentParser(
        prog='inventory',
        description='Hydrant Command Center — CLI entry point',
    )
    subparsers = parser.add_subparsers(dest='command', metavar='<command>')
    subparsers.required = True

    # --- serve ---
    serve_parser = subparsers.add_parser('serve', help='Launch the Streamlit dashboard')
    serve_parser.add_argument(
        '--port', type=int, default=None, metavar='PORT',
        help='Port to run on (default: 8501)',
    )
    serve_parser.set_defaults(func=cmd_serve)

    # --- etl ---
    etl_parser = subparsers.add_parser('etl', help='Run the ETL pipeline')
    etl_group = etl_parser.add_mutually_exclusive_group()
    etl_group.add_argument(
        '--full', action='store_true',
        help='Full historical refresh instead of incremental',
    )
    etl_group.add_argument(
        '--daemon', action='store_true',
        help='Run in daemon mode — stays alive and syncs daily',
    )
    etl_parser.set_defaults(func=cmd_etl)

    # --- migrate ---
    migrate_parser = subparsers.add_parser('migrate', help='Create/update database schema')
    migrate_parser.set_defaults(func=cmd_migrate)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
