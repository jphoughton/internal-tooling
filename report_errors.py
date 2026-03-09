"""
Checks page_errors table for recent dashboard errors and creates GitHub issues.
Skips duplicates — won't create an issue if one already exists for the same error.

Requires GITHUB_TOKEN env var (fine-grained PAT with Issues write permission).

Usage:
    python report_errors.py              # Check and create issues
    python report_errors.py --dry-run    # Print what would be created
"""
import os
import sys
import logging
from datetime import datetime

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

REPO = 'jphoughton/internal-tooling'
LABEL = 'auto-detected'
API_BASE = f'https://api.github.com/repos/{REPO}'


def _gh_headers():
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        log.warning('GITHUB_TOKEN not set — cannot create GitHub issues')
        return None
    return {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
    }


def _ensure_label(headers):
    """Create the 'auto-detected' label if it doesn't exist."""
    try:
        resp = requests.get(f'{API_BASE}/labels/{LABEL}', headers=headers, timeout=10)
        if resp.status_code == 404:
            requests.post(f'{API_BASE}/labels', headers=headers, timeout=10, json={
                'name': LABEL, 'color': 'e11d48', 'description': 'Auto-detected dashboard errors'
            })
    except Exception:
        pass


def get_recent_errors(hours=24):
    """Fetch recent page errors from DB."""
    from db import get_db, get_recent_page_errors
    with get_db() as conn:
        return get_recent_page_errors(conn, hours=hours)


def get_open_issues(headers):
    """Get open GitHub issues with our label."""
    try:
        resp = requests.get(
            f'{API_BASE}/issues',
            headers=headers,
            params={'labels': LABEL, 'state': 'open', 'per_page': 50},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception as e:
        log.error('Failed to list GitHub issues: %s', e)
        return []


def create_issue(title, body, headers):
    """Create a GitHub issue."""
    try:
        resp = requests.post(
            f'{API_BASE}/issues',
            headers=headers,
            json={'title': title, 'body': body, 'labels': [LABEL]},
            timeout=15,
        )
        if resp.status_code == 201:
            url = resp.json().get('html_url', '')
            log.info('Created issue: %s', url)
            return url
        else:
            log.error('Failed to create issue (%s): %s', resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        log.error('Failed to create issue: %s', e)
        return None


def error_fingerprint(error):
    """Create a unique key for deduplication based on page + error type + location."""
    tb = error.get('traceback', '')
    # Use the last "File ..." line from traceback as the location fingerprint
    file_lines = [l.strip() for l in tb.split('\n') if l.strip().startswith('File ')]
    location = file_lines[-1] if file_lines else ''
    return f"{error['page']}|{error['error_type']}|{location}"


def run_report(dry_run=False):
    """Check for errors and create GitHub issues."""
    print(f'\n{"="*60}')
    print(f'  ERROR REPORT — {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'{"="*60}\n')

    errors = get_recent_errors(hours=24)
    if not errors:
        print('No page errors in the last 24 hours.')
        return 0

    print(f'Found {len(errors)} error(s) in the last 24 hours.\n')

    # Deduplicate errors — group by fingerprint, keep first occurrence
    seen = {}
    for err in errors:
        fp = error_fingerprint(err)
        if fp not in seen:
            seen[fp] = err

    print(f'{len(seen)} unique error(s) after deduplication.\n')

    headers = _gh_headers()
    if not headers and not dry_run:
        print('GITHUB_TOKEN not set — skipping issue creation.')
        print('Set GITHUB_TOKEN env var to enable auto-issue creation.')
        return 1

    if headers and not dry_run:
        _ensure_label(headers)

    # Check existing open issues to avoid duplicates
    open_issues = get_open_issues(headers) if headers else []
    existing_titles = {i['title'] for i in open_issues}

    created = 0
    for fp, err in seen.items():
        title = f"[auto] {err['page']}: {err['error_type']} — {err['error_message'][:80]}"

        if title in existing_titles:
            print(f'  SKIP (issue exists): {title}')
            continue

        body = f"""## Auto-detected page error

**Page:** {err['page']}
**Error:** `{err['error_type']}: {err['error_message']}`
**First seen:** {err['created_at']}

### Traceback
```
{err.get('traceback', 'No traceback available')}
```

### How to fix
Open Claude Code and say: `fix the open auto-detected issues`

---
*Auto-created by `report_errors.py`*
"""

        if dry_run:
            print(f'  WOULD CREATE: {title}')
        else:
            url = create_issue(title, body, headers)
            if url:
                created += 1

    print(f'\n{"—"*60}')
    if dry_run:
        print(f'Dry run — {len(seen)} issue(s) would be created.')
    else:
        print(f'Created {created} new issue(s).')
    print(f'{"—"*60}\n')

    return 0


if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    sys.exit(run_report(dry_run=dry_run))
