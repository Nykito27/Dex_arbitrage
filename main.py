"""
main.py
-------
DeFi Multi-Chain Wallet Monitor

Reads credentials from environment variables (Replit Secrets), checks the
native-token balance of a single wallet across Polygon, Arbitrum, and Base,
then sends a single Telegram report.

Environment variables required (set via Replit Secrets):
  WALLET_ADDRESS      -- EVM wallet address to monitor (0x...)
  TELEGRAM_BOT_TOKEN  -- Token from Telegram @BotFather
  TELEGRAM_CHAT_ID    -- Telegram chat ID to receive alerts

Optional:
  RUN_ONCE            -- Set to "true" to run once and exit (default: loop every 60s)
"""

import os
import sys
import time

from monitor import check_all_chains, send_telegram_report

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 60  # How often to re-check balances when looping


def load_config() -> dict:
    """Load and validate required environment variables."""
    config = {
        "wallet_address": os.environ.get("WALLET_ADDRESS", "").strip(),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "run_once": os.environ.get("RUN_ONCE", "false").lower() == "true",
    }

    missing = [
        key
        for key, value in {
            "WALLET_ADDRESS": config["wallet_address"],
            "TELEGRAM_BOT_TOKEN": config["telegram_bot_token"],
            "TELEGRAM_CHAT_ID": config["telegram_chat_id"],
        }.items()
        if not value
    ]

    if missing:
        print(
            f"[ERROR] Missing required environment variable(s): {', '.join(missing)}\n"
            "Set them in Replit Secrets and restart.",
            file=sys.stderr,
        )
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# Core monitoring logic
# ---------------------------------------------------------------------------

def run_check(config: dict) -> None:
    """Perform one full monitoring cycle: check balances → send report."""
    print(f"[Monitor] Checking balances for wallet: {config['wallet_address']}")

    results = check_all_chains(config["wallet_address"])

    for r in results:
        if r["error"]:
            print(f"  [{r['name']}] ERROR: {r['error']}")
        else:
            flag = " *** LOW BALANCE ***" if r["is_low"] else ""
            print(
                f"  [{r['name']}] {r['balance']:.6f} {r['native_token']}{flag}"
            )

    send_telegram_report(
        bot_token=config["telegram_bot_token"],
        chat_id=config["telegram_chat_id"],
        results=results,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()

    print("[Monitor] DeFi Multi-Chain Monitor starting...")
    print("[Monitor] Chains: Polygon, Arbitrum, Base")
    print(f"[Monitor] Poll interval: {POLL_INTERVAL_SECONDS}s (set RUN_ONCE=true to run once)")

    if config["run_once"]:
        run_check(config)
    else:
        while True:
            try:
                run_check(config)
            except Exception as exc:
                print(f"[Monitor] Unexpected error: {exc}", file=sys.stderr)
            print(f"[Monitor] Sleeping for {POLL_INTERVAL_SECONDS}s...")
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
