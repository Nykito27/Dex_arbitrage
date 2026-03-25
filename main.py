"""
main.py
-------
DeFi High-Frequency Multi-Chain Arbitrage Hunter — 100% Autonomous Mode

Each cycle:
  1. Checks native-token wallet balances on Polygon, Arbitrum, Base.
  2. Scans all DEXes for every token in the watchlist.
  3. Calculates net profit after gas fees and flash-loan fee (0.05%).
  4. Sends a Telegram alert for same-chain opportunities > $10.
  5. Auto-fires initiateArbitrage() on the FlashLoanExecutor contract
     without waiting for manual Y/N confirmation.
  6. On tx revert: logs the error to trade_history.log and puts the
     token pair on a 5-minute cooldown before retrying.
  7. Sends a 4-hour rolling summary to Telegram.
  8. Serves /health on port 8080 for external uptime monitors.

Required Replit Secrets (all read via os.getenv):
  WALLET_ADDRESS            — EVM wallet to monitor balances
  TELEGRAM_BOT_TOKEN        — from Telegram @BotFather
  TELEGRAM_CHAT_ID          — target chat for alerts
  PRIVATE_KEY               — wallet private key that owns the contract
  EXECUTOR_CONTRACT_ADDRESS — deployed FlashLoanExecutor.sol address

Optional env vars:
  RUN_ONCE        — "true" → single pass then exit
  POLL_INTERVAL   — seconds between scans (default 60)
  PRICE_SNAPSHOT  — "true" → send full price table when no arb found
  KEEPALIVE_PORT  — port for the Flask keep-alive server (default 8080)
"""

from __future__ import annotations

import os
import sys
import time
import logging
import threading

from monitor import (
    check_all_chains,
    send_telegram_report,
    send_arb_alerts,
    send_price_snapshot,
    send_trade_executed,
    send_4h_summary,
    scan_all_dexes,
    store_optimal_route,
    is_on_cooldown,
    cooldown_remaining,
    log_attempt,
    log_success,
    log_failure,
    start_keepalive_server,
)
from monitor.flash_loan import FlashLoanExecutor, get_optimal_route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config loader — all values from os.getenv / Replit Secrets
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = {
        "wallet_address":     os.getenv("WALLET_ADDRESS",            "").strip(),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN",        "").strip(),
        "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID",          "").strip(),
        "executor_address":   os.getenv("EXECUTOR_CONTRACT_ADDRESS",  "").strip(),
        "private_key":        os.getenv("PRIVATE_KEY",                "").strip(),
        "private_rpc_url":    os.getenv("PRIVATE_RPC_URL",            "").strip(),
        "run_once":           os.getenv("RUN_ONCE",      "false").lower() == "true",
        "poll_interval":      int(os.getenv("POLL_INTERVAL", "60")),
        "price_snapshot":     os.getenv("PRICE_SNAPSHOT","false").lower() == "true",
        "keepalive_port":     int(os.getenv("KEEPALIVE_PORT", "5050")),
    }

    missing = [k for k, v in {
        "WALLET_ADDRESS":     cfg["wallet_address"],
        "TELEGRAM_BOT_TOKEN": cfg["telegram_bot_token"],
        "TELEGRAM_CHAT_ID":   cfg["telegram_chat_id"],
    }.items() if not v]

    if missing:
        print(
            f"[ERROR] Missing required secret(s): {', '.join(missing)}\n"
            "Add them under Replit → Secrets and restart.",
            file=sys.stderr,
        )
        sys.exit(1)

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Global stats — shared with Flask /status endpoint and 4h summary
# ─────────────────────────────────────────────────────────────────────────────

_bot_start_time = time.time()

stats: dict = {
    "cycles_run":            0,
    "same_chain_found":      0,
    "cross_chain_filtered":  0,
    "trades_attempted":      0,
    "trades_succeeded":      0,
    "trades_failed":         0,
    "total_est_profit_usd":  0.0,
    "last_summary_sent_at":  time.time(),
}

_stats_lock = threading.Lock()     # guard stats writes from multiple trade threads
SUMMARY_INTERVAL_SECS = 4 * 3600  # 4 hours

# Tracks live trade threads — scanner keeps running while they're in flight
_pending_trade_threads: list[threading.Thread] = []


# ─────────────────────────────────────────────────────────────────────────────
# Autonomous execution — no Y/N prompt
# ─────────────────────────────────────────────────────────────────────────────

SEPARATOR = "=" * 62


def execute_trade(cfg: dict, opportunity: dict) -> None:
    """
    Auto-fire initiateArbitrage() for a same-chain opportunity.

    Steps:
      1. Check pair cooldown (skip if recently failed).
      2. Pre-flight validate executor config.
      3. Retrieve and verify the stored execution payload.
      4. Log the attempt.
      5. Build, sign, and broadcast the transaction.
      6. On success: log + Telegram notification.
      7. On failure: log error + set pair cooldown.
    """
    executor_address = cfg["executor_address"]
    private_key      = cfg["private_key"]

    if not executor_address or not private_key:
        logger.debug(
            "[Executor] Skipping — EXECUTOR_CONTRACT_ADDRESS or PRIVATE_KEY not set."
        )
        return

    symbol   = opportunity["symbol"]
    buy_dex  = opportunity["buy_dex"]
    sell_dex = opportunity["sell_dex"]
    chain    = opportunity["buy_chain"]

    # ── Cooldown gate ────────────────────────────────────────────────────────
    if is_on_cooldown(symbol, buy_dex, sell_dex):
        rem = cooldown_remaining(symbol, buy_dex, sell_dex)
        logger.info(
            f"[Executor] {symbol} ({buy_dex}→{sell_dex}) on cooldown "
            f"— {rem:.0f}s remaining. Skipping."
        )
        return

    # ── Pre-flight ───────────────────────────────────────────────────────────
    executor = FlashLoanExecutor(
        contract_address=executor_address,
        private_key=private_key,
    )
    ready, reason = executor.validate_ready()
    if not ready:
        logger.error(f"[Executor] Pre-flight failed: {reason}")
        return

    # ── Check stored payload ─────────────────────────────────────────────────
    route = get_optimal_route()
    if route.get("status") != "ready":
        return

    execution = route.get("execution", {})
    if not execution or not execution.get("executable"):
        logger.debug(
            "[Executor] Stored route not executable: "
            + (execution.get("reason", "none") if execution else "no payload")
        )
        return

    hr  = execution["human_readable"]
    est = hr["estimated_profit_usd"]

    # ── Log attempt ──────────────────────────────────────────────────────────
    log_attempt(symbol, chain, buy_dex, sell_dex, est)
    with _stats_lock:
        stats["trades_attempted"] += 1

    logger.info(
        f"[Executor] AUTO-FIRING {symbol} on {chain} | "
        f"buy {buy_dex} → sell {sell_dex} | "
        f"est net ${est:,.2f}"
    )

    # ── Fire ─────────────────────────────────────────────────────────────────
    try:
        result = executor.fire(execution)
        tx_hash      = result["tx_hash"]
        explorer_url = result["explorer_url"]

        with _stats_lock:
            stats["trades_succeeded"]     += 1
            stats["total_est_profit_usd"] += est

        log_success(symbol, chain, buy_dex, sell_dex, tx_hash, explorer_url, est)

        logger.info(f"[Executor] ✅ Broadcast! tx={tx_hash}")
        logger.info(f"[Executor]    Track: {explorer_url}")
        print(f"\n{SEPARATOR}")
        print(f"  *** TRADE BROADCAST ***")
        print(f"  Chain    : {chain}")
        print(f"  Symbol   : {symbol}")
        print(f"  Est. Net : ${est:,.2f}")
        print(f"  Tx Hash  : {tx_hash}")
        print(f"  Explorer : {explorer_url}")
        print(f"{SEPARATOR}\n")

        send_trade_executed(
            cfg["telegram_bot_token"],
            cfg["telegram_chat_id"],
            symbol=symbol,
            chain=chain,
            estimated_profit_usd=est,
            tx_hash=tx_hash,
            explorer_url=explorer_url,
        )

    except Exception as exc:
        with _stats_lock:
            stats["trades_failed"] += 1
        log_failure(symbol, chain, buy_dex, sell_dex, str(exc), est)
        logger.error(f"[Executor] ❌ Transaction failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Async launcher — scanner keeps hunting while the trade is in flight
# ─────────────────────────────────────────────────────────────────────────────

def fire_trade_async(cfg: dict, opportunity: dict) -> None:
    """
    Launch execute_trade() in a daemon thread.

    The scan loop returns immediately and continues looking for the
    next opportunity — it does not stall waiting for tx confirmation.
    Completed threads are pruned from _pending_trade_threads on each call.
    """
    global _pending_trade_threads

    # Clean up threads that have already finished
    _pending_trade_threads = [t for t in _pending_trade_threads if t.is_alive()]

    thread_name = (
        f"trade-{opportunity['symbol']}-{opportunity['buy_chain']}-"
        f"{int(time.time())}"
    )
    t = threading.Thread(
        target=execute_trade,
        args=(cfg, opportunity),
        name=thread_name,
        daemon=True,
    )
    t.start()
    _pending_trade_threads.append(t)
    logger.info(
        f"[Executor] 🚀 Trade thread launched: {thread_name} "
        f"({len(_pending_trade_threads)} in flight)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4-hour summary sender
# ─────────────────────────────────────────────────────────────────────────────

def maybe_send_summary(cfg: dict) -> None:
    """Send the 4-hour rolling summary if it's time."""
    if time.time() - stats["last_summary_sent_at"] < SUMMARY_INTERVAL_SECS:
        return

    uptime_h = (time.time() - _bot_start_time) / 3600
    summary_stats = {**stats, "uptime_hours": uptime_h}

    send_4h_summary(
        cfg["telegram_bot_token"],
        cfg["telegram_chat_id"],
        summary_stats,
    )
    logger.info("[Summary] 4-hour summary sent to Telegram.")
    stats["last_summary_sent_at"] = time.time()


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
            logger.info(
                f"  [{r['name']}] {r['balance']:.6f} {r['native_token']}{flag}"
            )
    send_telegram_report(bot, chat, balance_results)

    # ── 2. Price scan ─────────────────────────────────────────────────────────
    logger.info("=== Price scan across DEXes ===")
    all_prices, opportunities = scan_all_dexes()
    stats["cycles_run"] += 1

    # ── 3. Split same-chain vs cross-chain ───────────────────────────────────
    same_chain  = [o for o in opportunities if o["buy_chain"] == o["sell_chain"]]
    cross_chain = [o for o in opportunities if o["buy_chain"] != o["sell_chain"]]

    stats["same_chain_found"]     += len(same_chain)
    stats["cross_chain_filtered"] += len(cross_chain)

    for o in cross_chain:
        logger.debug(
            f"[Arb] Cross-chain gap dropped: "
            f"{o['symbol']} {o['buy_chain']}→{o['sell_chain']} "
            f"net=${o['net_profit']:,.2f}"
        )

    # ── 4. Store best same-chain route for executor ───────────────────────────
    best = same_chain[0] if same_chain else None
    store_optimal_route(best)

    # ── 5. Telegram alerts — same-chain only ─────────────────────────────────
    if same_chain:
        n = send_arb_alerts(bot, chat, same_chain)
        logger.info(f"[Arb] {n} same-chain alert(s) sent.")
    elif cross_chain:
        logger.info(
            f"[Arb] {len(cross_chain)} cross-chain gap(s) filtered "
            "(same-chain only mode). No alerts sent."
        )
    elif cfg["price_snapshot"]:
        send_price_snapshot(bot, chat, all_prices)
        logger.info("[Arb] No opportunities — price snapshot sent.")
    else:
        logger.info("[Arb] No profitable opportunities this cycle.")

    # ── 6. Auto-execute best same-chain opportunity (non-blocking) ───────────
    if best:
        fire_trade_async(cfg, best)   # scanner keeps scanning; trade fires in background

    # ── 7. 4-hour summary check ───────────────────────────────────────────────
    maybe_send_summary(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()

    exec_configured = bool(cfg["executor_address"] and cfg["private_key"])
    exec_mode = "AUTO-FIRE (autonomous)" if exec_configured else (
        "monitor-only (add PRIVATE_KEY + EXECUTOR_CONTRACT_ADDRESS to enable trading)"
    )

    # ── Start Flask keep-alive server ────────────────────────────────────────
    start_keepalive_server(port=cfg["keepalive_port"], stats=stats)

    # ── Startup banner ───────────────────────────────────────────────────────
    private_rpc_status = (
        f"ACTIVE ({cfg['private_rpc_url'][:28]}...)"
        if cfg["private_rpc_url"] else "not set — using public Polygon RPC"
    )

    logger.info("DeFi Arbitrage Hunter — 100% Autonomous Mode")
    logger.info(f"Wallet    : {cfg['wallet_address']}")
    logger.info("DEXes     : Polygon  → Uniswap V3  ↔  SushiSwap V3")
    logger.info("            Arbitrum → Uniswap V3  ↔  SushiSwap V3")
    logger.info("            Base     → Uniswap V3  ↔  PancakeSwap V3")
    logger.info(f"Tokens    : {len(__import__('config').WATCHLIST)} symbols")
    logger.info("Alerts    : Same-chain only — cross-chain gaps silently dropped")
    logger.info(f"Min profit: $10 USD  |  Trade size: $10,000  |  FL fee: 0.05%")
    logger.info(f"Executor  : {cfg['executor_address'] or 'NOT SET'}")
    logger.info(f"Mode      : {exec_mode}")
    logger.info("")
    logger.info("─── Speed Upgrades ────────────────────────────────────────")
    logger.info(f"  Private RPC   : {private_rpc_status}")
    logger.info("  Gas strategy  : EIP-1559 aggressive tip × 1.25 (top of block)")
    logger.info("  Nonce mgmt    : local in-memory (no on-chain roundtrip)")
    logger.info("  Execution     : async daemon thread (scanner never pauses)")
    logger.info("  Pre-flight sim: eth_call before every broadcast")
    logger.info("───────────────────────────────────────────────────────────")
    logger.info("")
    logger.info(f"KeepAlive : http://0.0.0.0:{cfg['keepalive_port']}/health")
    logger.info(f"Summary   : every 4h via Telegram")
    logger.info(f"Cooldown  : 5m per pair after revert")
    logger.info(f"Poll      : every {cfg['poll_interval']}s  (RUN_ONCE={cfg['run_once']})")

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
