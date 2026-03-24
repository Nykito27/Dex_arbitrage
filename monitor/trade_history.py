"""
trade_history.py
----------------
Trade logging and per-pair cooldown management.

Every trade attempt (success or failure) is appended to trade_history.log
with a timestamp, token pair, chain, and outcome.

On a tx revert, the pair is put on a 5-minute cooldown so the bot
does not burn gas retrying the same broken route immediately.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_FILE       = Path("trade_history.log")
COOLDOWN_SECS  = 300          # 5 minutes per failed pair

# {(symbol, buy_dex, sell_dex): unix_timestamp_until}
_cooldowns: dict[tuple, float] = {}


# ---------------------------------------------------------------------------
# Cooldown helpers
# ---------------------------------------------------------------------------

def _key(symbol: str, buy_dex: str, sell_dex: str) -> tuple:
    return (symbol, buy_dex, sell_dex)


def is_on_cooldown(symbol: str, buy_dex: str, sell_dex: str) -> bool:
    """Return True if this pair is still cooling down after a failed tx."""
    return time.time() < _cooldowns.get(_key(symbol, buy_dex, sell_dex), 0)


def cooldown_remaining(symbol: str, buy_dex: str, sell_dex: str) -> float:
    """Seconds remaining on cooldown (0.0 if not active)."""
    remaining = _cooldowns.get(_key(symbol, buy_dex, sell_dex), 0) - time.time()
    return max(0.0, remaining)


def set_cooldown(symbol: str, buy_dex: str, sell_dex: str,
                 seconds: int = COOLDOWN_SECS) -> None:
    _cooldowns[_key(symbol, buy_dex, sell_dex)] = time.time() + seconds
    logger.info(
        f"[Cooldown] {symbol} {buy_dex}→{sell_dex} "
        f"blocked for {seconds // 60}m after revert."
    )


# ---------------------------------------------------------------------------
# Log writer
# ---------------------------------------------------------------------------

def _write(line: str) -> None:
    try:
        with LOG_FILE.open("a") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        logger.warning(f"[TradeHistory] Write failed: {exc}")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Public logging API
# ---------------------------------------------------------------------------

def log_attempt(symbol: str, chain: str, buy_dex: str, sell_dex: str,
                estimated_profit_usd: float) -> None:
    line = (
        f"{_ts()} | ATTEMPT | {chain:8s} | {symbol:8s} | "
        f"{buy_dex} → {sell_dex} | "
        f"est=${estimated_profit_usd:,.2f}"
    )
    _write(line)
    logger.info(f"[TradeHistory] {line}")


def log_success(symbol: str, chain: str, buy_dex: str, sell_dex: str,
                tx_hash: str, explorer_url: str,
                estimated_profit_usd: float) -> None:
    line = (
        f"{_ts()} | SUCCESS | {chain:8s} | {symbol:8s} | "
        f"{buy_dex} → {sell_dex} | "
        f"est=${estimated_profit_usd:,.2f} | "
        f"tx={tx_hash} | {explorer_url}"
    )
    _write(line)
    logger.info(f"[TradeHistory] {line}")


def log_failure(symbol: str, chain: str, buy_dex: str, sell_dex: str,
                error: str, estimated_profit_usd: float) -> None:
    error_short = str(error)[:150].replace("\n", " ")
    line = (
        f"{_ts()} | FAILED  | {chain:8s} | {symbol:8s} | "
        f"{buy_dex} → {sell_dex} | "
        f"est=${estimated_profit_usd:,.2f} | "
        f"err={error_short}"
    )
    _write(line)
    logger.error(f"[TradeHistory] {line}")
    set_cooldown(symbol, buy_dex, sell_dex)
