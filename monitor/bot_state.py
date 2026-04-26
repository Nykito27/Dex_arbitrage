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

    def __init__(self, base_min_profit_usd: float = 5.0) -> None:
        self._lock = threading.Lock()

        # Pause / resume scanning loop
        self.paused: bool = False

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


# Module-level singleton — import as: from monitor.bot_state import bot_state
bot_state = BotState()
