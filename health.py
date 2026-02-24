"""Minimal Flask health-check server.

Runs on port 8080 alongside the Streamlit dashboard so load balancers and
Railway health checks have a fast, lightweight endpoint to probe.

Endpoints:
  GET /health  →  {"status": "ok", "timestamp": "<UTC ISO-8601>"}
"""

from datetime import datetime, timezone

from flask import Flask, jsonify

app = Flask(__name__)


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
