"""
keep_alive.py
-------------
Production-grade Flask + waitress web server that keeps the Replit
container awake 24/7.

Routes:
  /        → "Bot is Active"  (human-readable)
  /health  → "OK"             (stable target for external pingers)

Point any external uptime pinger (UptimeRobot, Freshping, etc.) at:
  https://1ac8f5dd-3866-488c-838c-c20935a95850-00-mz6k8j2gyym.riker.replit.dev/health

The pinger hits /health every few minutes; Replit sees traffic and never
spins the container down, even when your browser is closed.

Why waitress (not Flask's dev server)?
  • Multi-threaded — handles concurrent pings without blocking
  • No reloader / debugger overhead
  • Production-tested, suppresses the "do not use in production" warning
"""

from __future__ import annotations

import logging
import threading

from flask import Flask
from waitress import serve

logger = logging.getLogger(__name__)
app    = Flask(__name__)


@app.route("/")
def home() -> tuple[str, int]:
    return "Bot is Active", 200


@app.route("/health")
def health() -> tuple[str, int]:
    """Plain text 'OK' — stable, predictable, parser-friendly."""
    return "OK", 200


def _run() -> None:
    """Block on the waitress WSGI server (called from a daemon thread)."""
    logger.info("[KeepAlive] Web server starting on port 8080 (waitress)")
    # threads=4 → handles bursts of pinger traffic without blocking
    serve(app, host="0.0.0.0", port=8080, threads=4, _quiet=True)


def keep_alive() -> threading.Thread:
    """Launch the web server in a background daemon thread; return it."""
    t = threading.Thread(target=_run, name="keep-alive", daemon=True)
    t.start()
    logger.info(
        "[KeepAlive] Running at http://0.0.0.0:8080/  "
        "(/health → 'OK' for pingers)"
    )
    return t
