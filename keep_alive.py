"""
keep_alive.py
-------------
Lightweight Flask web server that keeps the Replit container awake 24/7.

Point any external uptime pinger (UptimeRobot, Freshping, etc.) at:
  https://1ac8f5dd-3866-488c-838c-c20935a95850-00-mz6k8j2gyym.riker.replit.dev/

The pinger hits / every few minutes; Replit sees traffic and never spins
the container down, even when your browser is closed.
"""

import threading
import logging
from flask import Flask, jsonify

logger = logging.getLogger(__name__)
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is Active", 200


@app.route("/health")
def health():
    return jsonify({"status": "Bot is Active"}), 200


def run():
    """Start Flask on port 8080 (blocking — call from a daemon thread)."""
    logger.info("[KeepAlive] Web server starting on port 8080")
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)


def keep_alive():
    """Launch the web server in a background daemon thread and return immediately."""
    t = threading.Thread(target=run, name="keep-alive", daemon=True)
    t.start()
    logger.info("[KeepAlive] Running at http://0.0.0.0:8080/ — bot will stay online")
