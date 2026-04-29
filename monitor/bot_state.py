"""
bot_state.py
------------
Thread-safe shared state for the entire bot.

Read & mutated from:
  - main.py          (scan loop, trade executor, heartbeat scheduler)
  - price_hunter.py  (dynamic profit floor lookup, gas cache writes)
  - telegram_commands.py (Telegram /status, /toggle, /setprofit, /gas)
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class BotState:
    """Single source of truth for runtime-mutable bot state."""

    # Auto-pause threshold: pause scanner after N consecutive failed trades.
    CIRCUIT_BREAKER_THRESHOLD = 5

    def __init__(self, base_min_profit_usd: float = 5.0) -> None:
        self._lock = threading.Lock()

        # Pause / resume scanning loop
        self.paused: bool = False
        # Reason for the most recent auto-pause (e.g. "circuit-breaker: 5 reverts")
        self.pause_reason: str = ""

        # Base floor for the dynamic profit gate.
        # Actual floor = base_min_profit_usd + 2.5 × estimated gas cost
        self.min_profit_usd: float = base_min_profit_usd

        # Last balance snapshot (list of dicts from balance_checker)
        self.last_balances: list[dict] = []

        # Last trade (regardless of outcome) — populated by execute_trade()
        self.last_trade: Optional[dict] = None

        # Per-chain gas snapshot, populated each scan cycle.
        # { chain_name: {"base_fee_gwei", "tip_gwei", "gas_price_gwei",
        #                "aggressive_tip_gwei", "updated_at"} }
        self.last_gas: dict[str, dict] = {}

        # Heartbeat counters — reset every 6h window
        self._hb_opps:     int   = 0
        self._hb_executed: int   = 0
        self.heartbeat_window_start: float = time.time()

        # ── Lifetime P&L tracking (persists across heartbeat windows) ───────
        self.total_attempted:   int   = 0
        self.total_succeeded:   int   = 0
        self.total_failed:      int   = 0
        self.total_est_profit_usd: float = 0.0   # sum of estimated profit on successful broadcasts
        self.consecutive_reverts: int = 0        # for circuit breaker
        # Last 10 trade outcomes (newest first) — fuel for /lasttrades
        self.recent_trades: list[dict] = []

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------
    def toggle_pause(self) -> bool:
        with self._lock:
            self.paused = not self.paused
            return self.paused

    # ------------------------------------------------------------------
    # Profit floor
    # ------------------------------------------------------------------
    def set_min_profit(self, value: float) -> None:
        with self._lock:
            self.min_profit_usd = float(value)

    def dynamic_floor(self, total_gas_usd: float) -> float:
        """Active floor: base + 2.5 × gas (auto-adjusts to network conditions)."""
        return self.min_profit_usd + 2.5 * float(total_gas_usd)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def set_last_balances(self, balances: list[dict]) -> None:
        with self._lock:
            self.last_balances = list(balances)

    def set_last_trade(self, trade: dict) -> None:
        with self._lock:
            self.last_trade = dict(trade)

    def set_gas(self, chain: str, info: dict) -> None:
        info = {**info, "updated_at": time.time()}
        with self._lock:
            self.last_gas[chain] = info

    # ------------------------------------------------------------------
    # Heartbeat counters (6-hour windows)
    # ------------------------------------------------------------------
    def add_opportunities(self, n: int = 1) -> None:
        with self._lock:
            self._hb_opps += int(n)

    def add_executed(self, n: int = 1) -> None:
        with self._lock:
            self._hb_executed += int(n)

    def take_heartbeat_counts(self) -> tuple[int, int, float]:
        """Atomically read+reset the 6h window counters. Returns (opps, executed, window_hours)."""
        with self._lock:
            opps     = self._hb_opps
            executed = self._hb_executed
            window_h = (time.time() - self.heartbeat_window_start) / 3600.0
            self._hb_opps = 0
            self._hb_executed = 0
            self.heartbeat_window_start = time.time()
        return opps, executed, window_h

    # ------------------------------------------------------------------
    # Lifetime P&L + circuit breaker
    # ------------------------------------------------------------------
    def record_trade_outcome(
        self,
        success: bool,
        symbol: str = "",
        chain: str = "",
        est_profit_usd: float = 0.0,
        tx_hash: str = "",
        error: str = "",
    ) -> None:
        """
        Record a trade attempt's final outcome. On success, resets the
        consecutive-revert counter. On failure, increments it (used by
        should_circuit_break).
        """
        with self._lock:
            self.total_attempted += 1
            if success:
                self.total_succeeded += 1
                self.total_est_profit_usd += float(est_profit_usd)
                self.consecutive_reverts = 0
            else:
                self.total_failed += 1
                self.consecutive_reverts += 1

            self.recent_trades.insert(0, {
                "ts":      time.time(),
                "symbol":  symbol,
                "chain":   chain,
                "success": success,
                "est":     float(est_profit_usd),
                "tx":      tx_hash,
                "error":   (error or "")[:120],
            })
            # Keep only the most recent 10
            del self.recent_trades[10:]

    def should_circuit_break(self) -> bool:
        """True when consecutive failures >= threshold and bot is not already paused."""
        with self._lock:
            return (
                not self.paused
                and self.consecutive_reverts >= self.CIRCUIT_BREAKER_THRESHOLD
            )

    def trip_circuit_breaker(self, reason: str) -> None:
        """Pause the scanner with a recorded reason."""
        with self._lock:
            self.paused = True
            self.pause_reason = reason

    def reset_consecutive_reverts(self) -> None:
        """Manually clear the revert counter (e.g. after /toggle resume)."""
        with self._lock:
            self.consecutive_reverts = 0
            if self.paused and self.pause_reason.startswith("circuit-breaker"):
                self.pause_reason = ""

    def pnl_snapshot(self) -> dict:
        """Snapshot of lifetime P&L counters for /status and /pnl."""
        with self._lock:
            attempted = self.total_attempted
            succeeded = self.total_succeeded
            failed    = self.total_failed
            return {
                "attempted":  attempted,
                "succeeded":  succeeded,
                "failed":     failed,
                "success_rate_pct": (succeeded / attempted * 100) if attempted else 0.0,
                "total_est_profit_usd": self.total_est_profit_usd,
                "consecutive_reverts": self.consecutive_reverts,
                "circuit_threshold":   self.CIRCUIT_BREAKER_THRESHOLD,
                "pause_reason":        self.pause_reason,
            }

    def recent_trades_snapshot(self, n: int = 10) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self.recent_trades[:n]]


# Module-level singleton — import as: from monitor.bot_state import bot_state
bot_state = BotState()
