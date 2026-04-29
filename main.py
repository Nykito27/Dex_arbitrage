"""
main.py
-------
DeFi High-Frequency Multi-Chain Arbitrage Hunter — 100% Autonomous Mode

Each cycle:
  1. Checks native-token wallet balances on Polygon, Arbitrum, Base.
  2. Scans all DEXes for every token in the watchlist.
  3. Calculates net profit after gas fees and flash-loan fee (0.05%).
  4. Compares net profit to the DYNAMIC floor: base + 2.5 × gas cost.
  5. Sends a Telegram alert for same-chain opportunities above the floor.
  6. Auto-fires initiateArbitrage() on the FlashLoanExecutor contract via
     a private (MEV-protected) RPC bundle when one is configured for the chain.
  7. On tx revert: logs the error to trade_history.log and puts the
     token pair on a 5-minute cooldown before retrying.
  8. Sends a 4-hour rolling summary + a 6-hour heartbeat pulse.
  9. Listens for Telegram chat commands: /status /setprofit /toggle /gas
 10. Serves /health on port 8080 (waitress) for external uptime monitors.

Required Replit Secrets (all read via os.getenv):
  WALLET_ADDRESS            — EVM wallet to monitor balances
  TELEGRAM_BOT_TOKEN        — from Telegram @BotFather
  TELEGRAM_CHAT_ID          — owner chat (alerts + command authorisation)
  PRIVATE_KEY               — wallet private key that owns the contract
  EXECUTOR_CONTRACT_ADDRESS — deployed FlashLoanExecutor.sol address

Optional MEV-protection / speed secrets:
  PRIVATE_RPC_URL_POLYGON   — e.g. FastLane MEV Protect endpoint
  PRIVATE_RPC_URL_ARBITRUM  — e.g. Flashbots Protect endpoint
  PRIVATE_RPC_URL_BASE      — private Base endpoint
  PRIVATE_RPC_URL           — legacy single-URL fallback (treated as Polygon)

Optional behaviour env vars:
  RUN_ONCE        — "true" → single pass then exit
  POLL_INTERVAL   — seconds between scans (default 60)
  PRICE_SNAPSHOT  — "true" → send full price table when no arb found
"""

from __future__ import annotations

import os
import sys
import time
import logging
import threading

from keep_alive import keep_alive
from monitor.price_hunter import get_enabled_chains
from monitor import (
    check_all_chains,
    send_telegram_report,
    send_arb_alerts,
    send_alert,
    send_price_snapshot,
    send_trade_executed,
    send_4h_summary,
    send_heartbeat,
    scan_all_dexes,
    store_optimal_route,
    is_on_cooldown,
    cooldown_remaining,
    log_attempt,
    log_success,
    log_failure,
    bot_state,
    start_telegram_command_listener,
    get_private_rpc_status,
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
        "run_once":           os.getenv("RUN_ONCE",      "false").lower() == "true",
        "poll_interval":      int(os.getenv("POLL_INTERVAL", "60")),
        "price_snapshot":     os.getenv("PRICE_SNAPSHOT","false").lower() == "true",
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
# Global stats — shared with /status endpoint and 4h summary
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
    "last_heartbeat_at":     time.time(),
}

_stats_lock = threading.Lock()        # guard stats writes from multiple trade threads
SUMMARY_INTERVAL_SECS   = 4 * 3600    # 4 hours
HEARTBEAT_INTERVAL_SECS = 6 * 3600    # 6 hours

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
      4. Log the attempt + record snapshot in bot_state.
      5. Build, sign, and broadcast via private (or public) RPC.
      6. On success: log + Telegram notification + bump heartbeat counter.
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

    # ── Log attempt + snapshot ───────────────────────────────────────────────
    log_attempt(symbol, chain, buy_dex, sell_dex, est)
    with _stats_lock:
        stats["trades_attempted"] += 1

    bot_state.set_last_trade({
        "symbol":            symbol,
        "chain":             chain,
        "buy_dex":           buy_dex,
        "sell_dex":          sell_dex,
        "estimated_profit":  est,
        "outcome":           "PENDING",
        "ts":                time.time(),
    })

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
        bot_state.add_executed(1)
        bot_state.record_trade_outcome(
            success=True, symbol=symbol, chain=chain,
            est_profit_usd=est, tx_hash=tx_hash,
        )

        log_success(symbol, chain, buy_dex, sell_dex, tx_hash, explorer_url, est)

        bot_state.set_last_trade({
            "symbol": symbol, "chain": chain,
            "buy_dex": buy_dex, "sell_dex": sell_dex,
            "estimated_profit": est,
            "outcome": "SUCCESS",
            "tx_hash": tx_hash,
            "explorer_url": explorer_url,
            "ts": time.time(),
        })

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
        bot_state.record_trade_outcome(
            success=False, symbol=symbol, chain=chain,
            est_profit_usd=est, error=str(exc),
        )
        log_failure(symbol, chain, buy_dex, sell_dex, str(exc), est)
        bot_state.set_last_trade({
            "symbol": symbol, "chain": chain,
            "buy_dex": buy_dex, "sell_dex": sell_dex,
            "estimated_profit": est,
            "outcome": "FAILED",
            "error": str(exc)[:200],
            "ts": time.time(),
        })
        logger.error(f"[Executor] ❌ Transaction failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Async launcher — scanner keeps hunting while the trade is in flight
# ─────────────────────────────────────────────────────────────────────────────

def fire_trade_async(cfg: dict, opportunity: dict) -> None:
    """Launch execute_trade() in a daemon thread; scanner returns immediately."""
    global _pending_trade_threads
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
# Periodic Telegram messages — 4h summary + 6h heartbeat
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


def maybe_send_heartbeat(cfg: dict) -> None:
    """Send a System Healthy pulse every 6h with opp/exec counts for the window."""
    if time.time() - stats["last_heartbeat_at"] < HEARTBEAT_INTERVAL_SECS:
        return

    opps, executed, window_h = bot_state.take_heartbeat_counts()
    send_heartbeat(
        cfg["telegram_bot_token"],
        cfg["telegram_chat_id"],
        opportunities_found=opps,
        trades_executed=executed,
        window_hours=window_h,
    )
    logger.info(
        f"[Heartbeat] 💓 sent: {opps} opps / {executed} executed in last {window_h:.1f}h"
    )
    stats["last_heartbeat_at"] = time.time()


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
    bot_state.set_last_balances(balance_results)
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
    bot_state.add_opportunities(len(same_chain))   # heartbeat counter

    for o in cross_chain:
        logger.debug(
            f"[Arb] Cross-chain gap dropped: "
            f"{o['symbol']} {o['buy_chain']}→{o['sell_chain']} "
            f"net=${o['net_profit']:,.2f}"
        )

    # ── 4. Store best route + AUTO-FIRE IMMEDIATELY (zero alert delay) ──────
    # The trade thread starts BEFORE any Telegram call, so the eth_call sim +
    # broadcast happen in parallel with the alert HTTP POSTs. This shaves
    # ~200-500 ms off the detection → broadcast hot path.
    best = same_chain[0] if same_chain else None
    store_optimal_route(best)

    if best:
        fire_trade_async(cfg, best)   # scanner keeps scanning, alerts run in parallel

    # ── 5. Telegram alerts (sent WHILE the trade is already broadcasting) ───
    if same_chain:
        n = send_arb_alerts(bot, chat, same_chain)
        logger.info(f"[Arb] {n} same-chain alert(s) sent (trade already in flight).")
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

    # ── 7. Periodic Telegram pings ────────────────────────────────────────────
    maybe_send_summary(cfg)
    maybe_send_heartbeat(cfg)

    # ── 8. Circuit breaker — auto-pause after N consecutive reverts ──────────
    if bot_state.should_circuit_break():
        threshold = bot_state.CIRCUIT_BREAKER_THRESHOLD
        reason    = f"circuit-breaker: {threshold} consecutive trade reverts"
        bot_state.trip_circuit_breaker(reason)
        logger.error(f"[CircuitBreaker] {reason} — scanner PAUSED")
        try:
            send_alert(
                bot, chat,
                "🛑 *Circuit breaker tripped*\n"
                f"`{threshold}` consecutive trade reverts detected.\n"
                "Scanner *PAUSED* — investigate then send `/toggle` to resume."
            )
        except Exception as exc:
            logger.warning(f"[CircuitBreaker] alert send failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()

    exec_configured = bool(cfg["executor_address"] and cfg["private_key"])
    exec_mode = "AUTO-FIRE (autonomous)" if exec_configured else (
        "monitor-only (add PRIVATE_KEY + EXECUTOR_CONTRACT_ADDRESS to enable trading)"
    )

    # ── Start Flask keep-alive server (port 8080, daemon thread) ─────────────
    keep_alive()

    # ── Start Telegram command listener (daemon thread) ──────────────────────
    start_telegram_command_listener(
        cfg["telegram_bot_token"], cfg["telegram_chat_id"]
    )

    # ── Startup banner ───────────────────────────────────────────────────────
    rpc_status     = get_private_rpc_status()
    enabled_chains = get_enabled_chains()
    rpc_lines      = []
    for chain in ("Polygon", "Arbitrum", "Base"):
        if chain not in enabled_chains:
            tag = "❌ DISABLED (ENABLED_CHAINS)"
        elif rpc_status.get(chain):
            tag = "🛡 PRIVATE bundle"
        else:
            tag = "public mempool"
        rpc_lines.append(f"    {chain:8s}: {tag}")

    logger.info("DeFi Arbitrage Hunter — 100% Autonomous Mode")
    logger.info(f"Wallet    : {cfg['wallet_address']}")
    logger.info("DEXes     : Polygon  → Uniswap V3  ↔  SushiSwap V3")
    logger.info("            Arbitrum → Uniswap V3  ↔  SushiSwap V3")
    logger.info("            Base     → Uniswap V3  ↔  PancakeSwap V3")
    logger.info(f"Tokens    : {len(__import__('config').WATCHLIST)} symbols")
    logger.info(f"Chains    : enabled = {sorted(enabled_chains)}")
    logger.info("Alerts    : Same-chain only — cross-chain gaps silently dropped")
    logger.info(
        f"Min profit: dynamic floor = ${bot_state.min_profit_usd:.2f} + 2.5 × gas  "
        f"|  Trade size: $10,000  |  FL fee: 0.05%"
    )
    logger.info(f"Executor  : {cfg['executor_address'] or 'NOT SET'}")
    logger.info(f"Mode      : {exec_mode}")
    logger.info("")
    logger.info("─── Speed & MEV Protection ───────────────────────────────")
    logger.info("  Trade RPC channels:")
    for line in rpc_lines:
        logger.info(line)
    logger.info("  Gas strategy  : EIP-1559 aggressive tip × 1.30 (Fast +30%)")
    logger.info("  Profit floor  : DYNAMIC (auto-rises with gas)")
    logger.info("  Nonce mgmt    : local in-memory (no on-chain roundtrip)")
    logger.info("  Execution     : async daemon thread (scanner never pauses)")
    logger.info("  Pre-flight sim: eth_call before every broadcast")
    logger.info("  Profit check  : QuoterV2 depth-aware (real swap output @ borrow size)")
    logger.info("  Safety        : circuit breaker auto-pauses after 5 reverts")
    logger.info("──────────────────────────────────────────────────────────")
    logger.info("")
    logger.info("─── To enable MEV protection, set these Replit Secrets ──")
    logger.info("  PRIVATE_RPC_URL_POLYGON  = https://polygon-rpc.merkle.io")
    logger.info("  PRIVATE_RPC_URL_ARBITRUM = https://rpc.flashbots.net (Arbitrum coming)")
    logger.info("  PRIVATE_RPC_URL_BASE     = https://mainnet-sequencer.base.org")
    logger.info("  ENABLED_CHAINS           = Polygon  (skip unfunded chains)")
    logger.info("──────────────────────────────────────────────────────────")
    logger.info("")
    logger.info("Telegram cmds: /status /pnl /lasttrades /setprofit /toggle /gas /help")
    logger.info("KeepAlive    : http://0.0.0.0:8080/health  (returns 'OK')")
    logger.info(f"Summary      : every 4h via Telegram")
    logger.info(f"Heartbeat    : every 6h via Telegram (System Healthy pulse)")
    logger.info(f"Cooldown     : 5m per pair after revert")
    logger.info(f"Poll         : every {cfg['poll_interval']}s  (RUN_ONCE={cfg['run_once']})")

    if cfg["run_once"]:
        run_cycle(cfg)
        return

    while True:
        if bot_state.paused:
            logger.info("[Loop] ⏸ Paused via /toggle. Sleeping 5s, will check again.")
            time.sleep(5)
            continue
        try:
            run_cycle(cfg)
        except Exception as exc:
            logger.error(f"Cycle error: {exc}", exc_info=True)

        logger.info(f"Sleeping {cfg['poll_interval']}s...")
        time.sleep(cfg["poll_interval"])


if __name__ == "__main__":
    main()
