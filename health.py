"""Lightweight Flask health check server.

Runs as a separate process (via supervisord) alongside the Streamlit dashboard.
Exposes GET /health → {"status": "ok", "timestamp": "<UTC ISO-8601>"}
Port is configured via HEALTH_PORT env var (default: 8080).
"""

import os
from datetime import datetime, timezone

from flask import Flask, jsonify

app = Flask(__name__)


@app.get('/health')
def health():
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    })


if __name__ == '__main__':
    port = int(os.environ.get('HEALTH_PORT', 8080))
    app.run(host='0.0.0.0', port=port)
