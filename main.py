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
  5. Stores the best route and (if EXECUTOR_CONTRACT_ADDRESS + PRIVATE_KEY are
     set) prompts Y/N confirmation before firing the flash-loan transaction.

Required Replit Secrets:
  WALLET_ADDRESS            — EVM wallet to monitor balances on
  TELEGRAM_BOT_TOKEN        — from Telegram @BotFather
  TELEGRAM_CHAT_ID          — target chat for alerts

Execution Secrets (add when ready to go live):
  EXECUTOR_CONTRACT_ADDRESS — deployed FlashLoanExecutor.sol address
  PRIVATE_KEY               — wallet private key that owns the contract

Optional env vars:
  MANUAL_CONFIRM  — "true" (default) = Y/N prompt before each trade
                    "false"          = fire automatically (use with caution)
  RUN_ONCE        — "true" → single pass then exit
  POLL_INTERVAL   — seconds between scans (default: 60)
  PRICE_SNAPSHOT  — "true" → send full price table even with no arb
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
from monitor.flash_loan import FlashLoanExecutor, get_optimal_route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = {
        "wallet_address":     os.environ.get("WALLET_ADDRESS", "").strip(),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id":   os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "run_once":           os.environ.get("RUN_ONCE",  "false").lower() == "true",
        "poll_interval":      int(os.environ.get("POLL_INTERVAL", "60")),
        "price_snapshot":     os.environ.get("PRICE_SNAPSHOT", "false").lower() == "true",
        # Execution settings
        "executor_address":   os.environ.get("EXECUTOR_CONTRACT_ADDRESS", "").strip(),
        "private_key":        os.environ.get("PRIVATE_KEY", "").strip(),
        "manual_confirm":     os.environ.get("MANUAL_CONFIRM", "true").lower() == "true",
    }

    missing = [k for k, v in {
        "WALLET_ADDRESS":     cfg["wallet_address"],
        "TELEGRAM_BOT_TOKEN": cfg["telegram_bot_token"],
        "TELEGRAM_CHAT_ID":   cfg["telegram_chat_id"],
    }.items() if not v]

    if missing:
        print(
            f"[ERROR] Missing required secret(s): {', '.join(missing)}\n"
            "Add them in Replit → Secrets and restart.",
            file=sys.stderr,
        )
        sys.exit(1)

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Execution logic  (validation → confirmation → fire)
# ─────────────────────────────────────────────────────────────────────────────

SEPARATOR = "=" * 62


def _print_trade_summary(payload: dict) -> None:
    """Print a formatted trade summary before asking for confirmation."""
    hr  = payload["human_readable"]
    ap  = payload["arb_params"]

    print(f"\n{SEPARATOR}")
    print("  *** FLASH LOAN OPPORTUNITY DETECTED ***")
    print(SEPARATOR)
    print(f"  Chain          : {hr['chain']}")
    print(f"  Token          : {hr['symbol']}")
    print(f"  Loan size      : {hr['loan_amount_tokens']} {hr['symbol']}  "
          f"(${hr['loan_amount_usd']:,.2f})")
    print(f"  Buy on         : {hr['buy_dex']}  @ ${hr['buy_price_usd']:,.4f}")
    print(f"  Sell on        : {hr['sell_dex']} @ ${hr['sell_price_usd']:,.4f}")
    print(f"  Spread         : {hr['spread_pct']:.3f}%")
    print(f"  Gross profit   : ${hr['gross_profit_usd']:,.2f}")
    print(f"  Flash-loan fee : ${hr['flash_loan_fee_usd']:,.2f}")
    print(f"  Gas cost       : ${hr['gas_cost_usd']:,.4f}")
    print(f"  NET PROFIT EST : ${hr['estimated_profit_usd']:,.2f}")
    print(f"  Min-profit floor: ${hr['min_profit_floor_usd']:,.2f}  "
          "(tx reverts if missed)")
    print(SEPARATOR)
    print("  ArbParams for contract:")
    print(f"    routerA   : {ap['routerA']}")
    print(f"    routerB   : {ap['routerB']}")
    print(f"    tokenIn   : {ap['tokenIn']}")
    print(f"    tokenOut  : {ap['tokenOut']}")
    print(f"    feeA      : {ap['feeA']}   feeB: {ap['feeB']}")
    print(f"    loanAmount: {ap['loanAmount']} wei")
    print(f"    minProfit : {ap['minProfit']} wei")
    print(SEPARATOR)


def _ask_confirmation() -> bool:
    """
    Prompt Y/N in the terminal.
    Returns True if user typed 'y' / 'yes', False otherwise.
    Falls back to False (safe) when stdin is not a tty (non-interactive).
    """
    if not sys.stdin.isatty():
        logger.warning(
            "[Confirm] stdin is not a terminal — skipping execution. "
            "Set MANUAL_CONFIRM=false to auto-fire, or run interactively."
        )
        return False

    try:
        answer = input("\n  Execute Flash Loan? (Y/N) : ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def maybe_execute_trade(cfg: dict) -> None:
    """
    Called after every scan cycle.

    Steps:
      1. Check that executor address and private key are configured.
      2. Retrieve latest optimal route; skip if not executable.
      3. In MANUAL_CONFIRM mode: print trade details and ask Y/N.
      4. Fire the transaction; print tx hash + explorer link.
    """
    executor_address = cfg["executor_address"]
    private_key      = cfg["private_key"]

    # ── Validation check — both must be present ──────────────────────────────
    if not executor_address:
        logger.debug(
            "[Executor] EXECUTOR_CONTRACT_ADDRESS not set — "
            "monitoring only, no trades will fire."
        )
        return
    if not private_key:
        logger.warning(
            "[Executor] EXECUTOR_CONTRACT_ADDRESS is set but PRIVATE_KEY is missing. "
            "Add PRIVATE_KEY in Replit Secrets to enable live trading."
        )
        return

    # ── Check executor pre-flight (address format, key length) ───────────────
    executor = FlashLoanExecutor(
        contract_address=executor_address,
        private_key=private_key,
    )
    ready, reason = executor.validate_ready()
    if not ready:
        logger.error(f"[Executor] Pre-flight check failed: {reason}")
        return

    # ── Get the latest stored opportunity ────────────────────────────────────
    route = get_optimal_route()
    if route.get("status") != "ready":
        return

    execution = route.get("execution", {})
    if not execution or not execution.get("executable"):
        reason = execution.get("reason", "no executable payload") if execution else "none"
        logger.debug(f"[Executor] Opportunity not executable: {reason}")
        return

    # ── Manual confirmation gate ──────────────────────────────────────────────
    if cfg["manual_confirm"]:
        _print_trade_summary(execution)
        confirmed = _ask_confirmation()
        if not confirmed:
            print("  [Executor] Trade skipped.\n")
            logger.info("[Executor] User declined — trade skipped.")
            return
        print("  [Executor] Confirmed — broadcasting transaction...\n")
    else:
        hr = execution["human_readable"]
        logger.info(
            f"[Executor] Auto-firing: {hr['symbol']} "
            f"${hr['estimated_profit_usd']:,.2f} net profit on {hr['chain']}"
        )

    # ── Fire ─────────────────────────────────────────────────────────────────
    try:
        result = executor.fire(execution)
        print(f"\n{SEPARATOR}")
        print("  *** TRANSACTION BROADCAST ***")
        print(f"  Chain   : {result['chain']}")
        print(f"  Tx Hash : {result['tx_hash']}")
        print(f"  Track   : {result['explorer_url']}")
        print(f"{SEPARATOR}\n")
        logger.info(f"[Executor] Fired! tx={result['tx_hash']}")
    except Exception as exc:
        logger.error(f"[Executor] Transaction failed: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# One monitoring cycle
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle(cfg: dict) -> None:
    bot  = cfg["telegram_bot_token"]
    chat = cfg["telegram_chat_id"]

    # ── 1. Wallet balance check ───────────────────────────────────────────────
    logger.info("=== Wallet balance check ===")
    balance_results = check_all_chains(cfg["wallet_address"])
    for r in balance_results:
        if r["error"]:
            logger.warning(f"  [{r['name']}] ERROR: {r['error']}")
        else:
            flag = " *** LOW ***" if r["is_low"] else ""
            logger.info(f"  [{r['name']}] {r['balance']:.6f} {r['native_token']}{flag}")
    send_telegram_report(bot, chat, balance_results)

    # ── 2. Price scan ─────────────────────────────────────────────────────────
    logger.info("=== Price scan across DEXes ===")
    all_prices, opportunities = scan_all_dexes()

    # ── 3. Split same-chain vs cross-chain ───────────────────────────────────
    same_chain  = [o for o in opportunities if o["buy_chain"] == o["sell_chain"]]
    cross_chain = [o for o in opportunities if o["buy_chain"] != o["sell_chain"]]

    if cross_chain:
        for o in cross_chain:
            logger.debug(
                f"[Arb] Cross-chain gap skipped "
                f"({o['symbol']} {o['buy_chain']}→{o['sell_chain']} "
                f"${o['net_profit']:,.2f}) — not alertable."
            )

    # ── 4. Store best SAME-CHAIN route for executor ───────────────────────────
    best = same_chain[0] if same_chain else None
    store_optimal_route(best)

    # ── 5. Telegram alerts — same-chain only ──────────────────────────────────
    if same_chain:
        n = send_arb_alerts(bot, chat, same_chain)
        logger.info(f"[Arb] {n} same-chain alert(s) sent.")
    elif cross_chain:
        logger.info(
            f"[Arb] {len(cross_chain)} cross-chain gap(s) found but filtered "
            "(same-chain only mode). No alerts sent."
        )
    elif cfg["price_snapshot"]:
        send_price_snapshot(bot, chat, all_prices)
        logger.info("[Arb] No opportunities — price snapshot sent.")
    else:
        logger.info("[Arb] No profitable opportunities this cycle.")

    # ── 6. Execution gate — same-chain only (fires only if secrets set + confirmed)
    maybe_execute_trade(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()

    exec_configured = bool(cfg["executor_address"])
    exec_mode = (
        "MANUAL CONFIRM" if cfg["manual_confirm"] else "AUTO-FIRE"
    ) if exec_configured else "monitor-only (add PRIVATE_KEY to enable trading)"

    logger.info("DeFi Arbitrage Hunter starting...")
    logger.info(f"Wallet   : {cfg['wallet_address']}")
    logger.info("DEXes    : Polygon  → Uniswap V3  ↔  SushiSwap V3")
    logger.info("           Arbitrum → Uniswap V3  ↔  SushiSwap V3")
    logger.info("           Base     → Uniswap V3  ↔  PancakeSwap V3")
    logger.info(f"Tokens   : {len(__import__('config').WATCHLIST)} symbols monitored")
    logger.info("Alerts   : Same-chain only (cross-chain gaps silently dropped)")
    logger.info(f"Min profit: $10 USD  |  Trade size: $10,000  |  Flash-loan fee: 0.05%")
    logger.info(f"Executor : {cfg['executor_address'] or 'NOT SET'}")
    logger.info(f"Mode     : {exec_mode}")
    logger.info(f"Poll     : every {cfg['poll_interval']}s  (RUN_ONCE={cfg['run_once']})")

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
