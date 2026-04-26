"""
github_action_runner.py
-----------------------
ONE-SHOT runner for GitHub Actions (or any cron-based scheduler).

Lifecycle (~30-90 seconds end-to-end):
  1. Wake up.
  2. Read all secrets from os.environ.
  3. Snapshot wallet balances on every chain.
  4. Scan all DEXes across the watchlist for same-chain arbitrage gaps.
  5. If the best opportunity's net profit > MIN_PROFIT_USD ($10 default):
       - send Telegram alert
       - fire the flash-loan trade through FlashLoanExecutor.sol
       - send Telegram trade-executed receipt
  6. Otherwise: optionally send a "no-op" ping if SEND_PING=true.
  7. Exit. (Container is destroyed by GitHub Actions — no daemon, no keep-alive.)

Why a separate runner (vs. main.py with RUN_ONCE=true):
  • main.py also boots the waitress keep-alive server and the Telegram
    long-poll command listener — both pointless inside a 4-minute CI job.
  • This runner imports only what it needs, exits cleanly, and uses a static
    profit floor (the dynamic "5 + 2.5×gas" is overkill for 5-min cron cadence).

All sensitive values come from os.environ — never hard-code anything here.
"""

from __future__ import annotations

import logging
import os
import sys
import time

# Make the project root importable when this script lives in scripts/
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from monitor import (                           # noqa: E402
    bot_state,
    check_all_chains,
    get_private_rpc_status,
    is_on_cooldown,
    cooldown_remaining,
    log_attempt,
    log_failure,
    log_success,
    scan_all_dexes,
    send_arb_alerts,
    send_telegram_report,
    send_trade_executed,
    store_optimal_route,
)
from monitor.flash_loan import FlashLoanExecutor, get_optimal_route   # noqa: E402
from monitor.telegram_notifier import _send as _telegram_send         # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gh-action")

REQUIRED_SECRETS = (
    "WALLET_ADDRESS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "PRIVATE_KEY",
    "EXECUTOR_CONTRACT_ADDRESS",
)


def _check_secrets() -> dict[str, str]:
    missing = [k for k in REQUIRED_SECRETS if not os.getenv(k, "").strip()]
    if missing:
        log.error("Missing required GitHub secret(s): %s", ", ".join(missing))
        log.error("Configure them at: Settings → Secrets and variables → Actions")
        sys.exit(1)
    return {k: os.environ[k].strip() for k in REQUIRED_SECRETS}


# ─────────────────────────────────────────────────────────────────────────────
# Main one-shot flow
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    t0  = time.time()
    cfg = _check_secrets()

    bot_token = cfg["TELEGRAM_BOT_TOKEN"]
    chat_id   = cfg["TELEGRAM_CHAT_ID"]
    wallet    = cfg["WALLET_ADDRESS"]

    min_profit = float(os.getenv("MIN_PROFIT_USD", "10"))
    send_ping  = os.getenv("SEND_PING", "false").lower() == "true"

    # Force the static floor for this short run (override the dynamic one).
    # The scanner uses bot_state.dynamic_floor() = base + 2.5×gas; setting
    # base to min_profit AND zeroing the gas multiplier is heavy-handed, so
    # we just use a high-enough base and re-filter below with strict ">".
    bot_state.set_min_profit(min_profit)

    # ── Banner ──────────────────────────────────────────────────────────────
    rpc_status = get_private_rpc_status()
    log.info("=" * 62)
    log.info("DeFi Arbitrage Hunter — GitHub Actions one-shot run")
    log.info("Wallet     : %s", wallet)
    log.info("Min profit : > $%.2f net (static floor for this run)", min_profit)
    log.info(
        "Trade RPCs : Polygon=%s  Arbitrum=%s  Base=%s",
        "PRIVATE" if rpc_status.get("Polygon")  else "public",
        "PRIVATE" if rpc_status.get("Arbitrum") else "public",
        "PRIVATE" if rpc_status.get("Base")     else "public",
    )
    log.info("=" * 62)

    # ── 1. Balance snapshot ─────────────────────────────────────────────────
    log.info("[1/4] Wallet balance snapshot")
    balances = check_all_chains(wallet)
    for r in balances:
        if r.get("error"):
            log.warning("  [%s] ERROR: %s", r["name"], r["error"])
        else:
            flag = "  *** LOW ***" if r.get("is_low") else ""
            log.info("  [%s] %.6f %s%s",
                     r["name"], r["balance"], r["native_token"], flag)

    # ── 2. DEX price scan ───────────────────────────────────────────────────
    log.info("[2/4] Scanning all DEXes across the watchlist")
    _all_prices, opportunities = scan_all_dexes()
    same_chain = [o for o in opportunities if o["buy_chain"] == o["sell_chain"]]
    log.info("  → %d same-chain opportunit(ies) above the dynamic floor",
             len(same_chain))

    # Apply the strict static $10 gate on top of whatever the scanner returned.
    qualifying = [o for o in same_chain if o["net_profit"] > min_profit]
    log.info("  → %d opportunit(ies) clear the strict $%.2f gate",
             len(qualifying), min_profit)

    # ── 3. Telegram alerts (only when something qualifies) ──────────────────
    if not qualifying:
        elapsed = time.time() - t0
        log.info("[3/4] No qualifying opportunities — nothing to fire.")
        if send_ping:
            _telegram_send(
                bot_token, chat_id,
                f"*🟢 GH-Actions ping*\n"
                f"Cycle finished in `{elapsed:.1f}s` — no opps above "
                f"`${min_profit:.2f}` this round.",
            )
        log.info("[4/4] Done in %.1fs.", elapsed)
        return 0

    send_arb_alerts(bot_token, chat_id, qualifying)

    # ── 4. Auto-fire the best qualifying trade ──────────────────────────────
    best   = qualifying[0]   # scan_all_dexes already sorts by net_profit desc
    symbol = best["symbol"]
    chain  = best["buy_chain"]
    buy    = best["buy_dex"]
    sell   = best["sell_dex"]
    est    = best["net_profit"]

    if is_on_cooldown(symbol, buy, sell):
        rem = cooldown_remaining(symbol, buy, sell)
        log.info("[3/4] Top opp %s (%s→%s) on cooldown — %.0fs left. Skipping.",
                 symbol, buy, sell, rem)
        log.info("[4/4] Done in %.1fs.", time.time() - t0)
        return 0

    store_optimal_route(best)

    executor = FlashLoanExecutor(
        contract_address=cfg["EXECUTOR_CONTRACT_ADDRESS"],
        private_key=cfg["PRIVATE_KEY"],
    )
    ready, why = executor.validate_ready()
    if not ready:
        log.error("[3/4] Executor pre-flight failed: %s", why)
        return 1

    route     = get_optimal_route()
    execution = route.get("execution") or {}
    if not execution.get("executable"):
        log.warning("[3/4] Stored route not executable: %s",
                    execution.get("reason", "unknown"))
        return 0

    log_attempt(symbol, chain, buy, sell, est)
    log.info("[3/4] FIRING: %s on %s | %s → %s | est net $%.2f",
             symbol, chain, buy, sell, est)

    try:
        result = executor.fire(execution)
        log_success(symbol, chain, buy, sell,
                    result["tx_hash"], result["explorer_url"], est)
        send_trade_executed(
            bot_token, chat_id,
            symbol=symbol,
            chain=chain,
            estimated_profit_usd=est,
            tx_hash=result["tx_hash"],
            explorer_url=result["explorer_url"],
        )
        log.info("[4/4] ✅ Broadcast: %s  (run took %.1fs)",
                 result["tx_hash"], time.time() - t0)
        return 0

    except Exception as exc:
        log_failure(symbol, chain, buy, sell, str(exc), est)
        log.error("[4/4] ❌ Trade failed: %s  (run took %.1fs)",
                  exc, time.time() - t0)
        return 1


if __name__ == "__main__":
    sys.exit(main())
