"""
main.py
-------
DeFi High-Frequency Multi-Chain Arbitrage Hunter

Each cycle:
  1. Checks native-token wallet balances on Polygon, Arbitrum, Base.
  2. Scans Uniswap V3 (Polygon), SushiSwap (Arbitrum), PancakeSwap (Base)
     for every token in the watchlist.
  3. Calculates net profit after gas fees and flash-loan fee (0.05%).
  4. Sends a Telegram alert for any opportunity with net profit > $10 USD,
     including direct DEX swap links.
  5. Stores the best route in flash_loan.optimal_route for the executor.

Required Replit Secrets:
  WALLET_ADDRESS      — EVM wallet to monitor
  TELEGRAM_BOT_TOKEN  — from Telegram @BotFather
  TELEGRAM_CHAT_ID    — target chat for alerts

Optional env vars:
  RUN_ONCE            — "true" → single pass then exit (default: loop)
  POLL_INTERVAL       — seconds between scans (default: 60)
  PRICE_SNAPSHOT      — "true" → send full price table even with no arb found
"""

from __future__ import annotations

import os
import sys
import time
import logging

from monitor import (
    check_all_chains,
    send_telegram_report,
    send_arb_alerts,
    send_price_snapshot,
    scan_all_dexes,
    store_optimal_route,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = {
        "wallet_address":     os.environ.get("WALLET_ADDRESS", "").strip(),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id":   os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "run_once":           os.environ.get("RUN_ONCE", "false").lower() == "true",
        "poll_interval":      int(os.environ.get("POLL_INTERVAL", "60")),
        "price_snapshot":     os.environ.get("PRICE_SNAPSHOT", "false").lower() == "true",
    }

    missing = [k for k, v in {
        "WALLET_ADDRESS":     cfg["wallet_address"],
        "TELEGRAM_BOT_TOKEN": cfg["telegram_bot_token"],
        "TELEGRAM_CHAT_ID":   cfg["telegram_chat_id"],
    }.items() if not v]

    if missing:
        print(
            f"[ERROR] Missing secret(s): {', '.join(missing)}\n"
            "Add them in Replit Secrets and restart.",
            file=sys.stderr,
        )
        sys.exit(1)

    return cfg


# ---------------------------------------------------------------------------
# One monitoring cycle
# ---------------------------------------------------------------------------

def run_cycle(cfg: dict) -> None:
    bot   = cfg["telegram_bot_token"]
    chat  = cfg["telegram_chat_id"]

    # ── 1. Wallet balance check ────────────────────────────────────────────
    logger.info("=== Wallet balance check ===")
    balance_results = check_all_chains(cfg["wallet_address"])
    for r in balance_results:
        if r["error"]:
            logger.warning(f"  [{r['name']}] ERROR: {r['error']}")
        else:
            flag = " *** LOW ***" if r["is_low"] else ""
            logger.info(f"  [{r['name']}] {r['balance']:.6f} {r['native_token']}{flag}")

    send_telegram_report(bot, chat, balance_results)

    # ── 2. Price scan across all DEXes ────────────────────────────────────
    logger.info("=== Price scan across DEXes ===")
    all_prices, opportunities = scan_all_dexes()

    # ── 3. Store optimal route for flash-loan executor ────────────────────
    best = opportunities[0] if opportunities else None
    store_optimal_route(best)

    # ── 4. Send Telegram alerts ───────────────────────────────────────────
    if opportunities:
        n = send_arb_alerts(bot, chat, opportunities)
        logger.info(f"[Arb] {n} opportunity alert(s) sent.")
    elif cfg["price_snapshot"]:
        send_price_snapshot(bot, chat, all_prices)
        logger.info("[Arb] No opportunities — price snapshot sent.")
    else:
        logger.info("[Arb] No profitable opportunities this cycle.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()

    logger.info("DeFi Arbitrage Hunter starting...")
    logger.info(f"Wallet : {cfg['wallet_address']}")
    logger.info(f"DEXes  : Uniswap V3 (Polygon) | SushiSwap (Arbitrum) | PancakeSwap (Base)")
    logger.info(f"Tokens : WETH, WBTC, LINK, GHO, USDe, MATIC")
    logger.info(f"Min net profit: $10 USD  |  Trade size: $10,000  |  Flash-loan fee: 0.05%")
    logger.info(f"Poll   : every {cfg['poll_interval']}s  (RUN_ONCE={cfg['run_once']})")

    if cfg["run_once"]:
        run_cycle(cfg)
        return

    while True:
        try:
            run_cycle(cfg)
        except Exception as exc:
            logger.error(f"Cycle error: {exc}", exc_info=True)

        logger.info(f"Sleeping {cfg['poll_interval']}s...")
        time.sleep(cfg["poll_interval"])


if __name__ == "__main__":
    main()
