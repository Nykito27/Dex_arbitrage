"""
keepalive.py
------------
Lightweight Flask web server on port 8080.

Serves /health (and /) so external uptime monitors (UptimeRobot,
Freshping, BetterUptime, etc.) can ping the container every few minutes
and prevent Replit from sleeping the repl while you are offline.

Runs in a daemon thread — the arbitrage loop is never blocked.
"""

from __future__ import annotations

import logging
import threading
import time
from flask import Flask, jsonify

logger = logging.getLogger(__name__)

app = Flask(__name__)
_start_time = time.time()

# Shared reference to stats so the /status endpoint can read them
_stats_ref: dict = {}


def _uptime_str() -> str:
    secs  = int(time.time() - _start_time)
    h, r  = divmod(secs, 3600)
    m, s  = divmod(r, 60)
    return f"{h}h {m}m {s}s"


@app.route("/")
@app.route("/health")
def health():
    return jsonify({
        "status":         "running",
        "service":        "DeFi Arbitrage Hunter",
        "uptime":         _uptime_str(),
        "uptime_seconds": int(time.time() - _start_time),
    })


@app.route("/status")
def status():
    return jsonify({
        "status":  "running",
        "uptime":  _uptime_str(),
        "stats":   _stats_ref,
    })


def start_keepalive_server(port: int = 8080,
                           stats: dict | None = None) -> None:
    """
    Launch the Flask keep-alive server in a background daemon thread.

    Parameters
    ----------
    port  : TCP port to listen on (default 8080)
    stats : mutable dict from main.py — exposed at /status
    """
    if stats is not None:
        _stats_ref.update(stats)
        _stats_ref["_live_ref"] = stats   # keep a pointer for live updates

    def _run():
        import os
        import logging as _log
        _log.getLogger("werkzeug").setLevel(_log.WARNING)   # quiet Flask logs
        logger.info(f"[KeepAlive] Flask server listening on 0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, name="keepalive", daemon=True)
    t.start()
    logger.info(f"[KeepAlive] Server started — ping http://localhost:{port}/health")
