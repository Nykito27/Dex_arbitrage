"""
telegram_commands.py
--------------------
Telegram chat-command listener — long-polls getUpdates and dispatches:

  /status              → wallet balances, last trade, scanning/paused
  /setprofit <amount>  → change the min-profit base floor
  /toggle              → pause / resume the scanner
  /gas                 → current network gas + the bot's priority tip
  /help                → command list

Authentication: ONLY messages from the configured TELEGRAM_CHAT_ID are
honoured — commands from any other chat are ignored.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import requests

from .bot_state import bot_state

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
LONG_POLL_TIMEOUT = 30          # seconds — Telegram long-poll
HTTP_TIMEOUT      = LONG_POLL_TIMEOUT + 5
PRIORITY_TIP_MULT = 1.30        # must match flash_loan._build_gas_params


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------

class TelegramCommandListener:
    """Long-polls Telegram and dispatches commands from the owner chat."""

    def __init__(self, bot_token: str, owner_chat_id: str) -> None:
        self.bot_token      = bot_token.strip()
        self.owner_chat_id  = str(owner_chat_id).strip()
        self.last_update_id = 0
        self.api            = f"{TELEGRAM_API_BASE}/bot{self.bot_token}"

    # ------------------------------------------------------------------
    # Send helper
    # ------------------------------------------------------------------
    def _send(self, text: str) -> None:
        try:
            requests.post(
                f"{self.api}/sendMessage",
                json={
                    "chat_id":    self.owner_chat_id,
                    "text":       text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception as exc:
            logger.warning(f"[TgCmd] Reply failed: {exc}")

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------
    def _cmd_help(self) -> str:
        return (
            "*🤖 Bot Command Center*\n"
            "\n"
            "/status             — balances, last trade, scan state\n"
            "/setprofit `<usd>`  — change min-profit base floor\n"
            "/toggle             — pause / resume scanner\n"
            "/gas                — gas prices + priority tip in use\n"
            "/help               — this message\n"
            "\n"
            "_Dynamic floor = base + 2.5 × gas cost_"
        )

    def _cmd_status(self) -> str:
        scan_state = "⏸ *PAUSED*" if bot_state.paused else "▶ *SCANNING*"
        base       = bot_state.min_profit_usd

        lines = [
            "*📊 Bot Status*",
            "",
            f"State: {scan_state}",
            f"Profit floor (base): `${base:,.2f}`",
            "_active floor = base + 2.5 × gas_",
            "",
            "*Wallet balances:*",
        ]

        balances = bot_state.last_balances
        if not balances:
            lines.append("  _no balance snapshot yet_")
        else:
            for r in balances:
                if r.get("error"):
                    lines.append(f"  • {r['name']}: ERROR `{r['error']}`")
                    continue
                low = "  ⚠ LOW" if r.get("is_low") else ""
                lines.append(
                    f"  • {r['name']}: `{r['balance']:.6f}` {r['native_token']}{low}"
                )

        lines.append("")
        lines.append("*Last trade:*")
        lt = bot_state.last_trade
        if not lt:
            lines.append("  _no trades attempted yet_")
        else:
            ts = datetime.fromtimestamp(lt.get("ts", 0), tz=timezone.utc)
            lines.append(f"  Symbol:  `{lt.get('symbol', '?')}/USDC`")
            lines.append(f"  Chain:   `{lt.get('chain', '?')}`")
            lines.append(f"  Route:   `{lt.get('buy_dex', '?')}` → `{lt.get('sell_dex', '?')}`")
            lines.append(f"  Outcome: `{lt.get('outcome', '?')}`")
            lines.append(f"  Est:     `${lt.get('estimated_profit', 0):,.2f}`")
            lines.append(f"  When:    `{ts.strftime('%Y-%m-%d %H:%M:%SZ')}`")
            if lt.get("tx_hash"):
                lines.append(f"  Tx:      `{lt['tx_hash']}`")
            if lt.get("error"):
                lines.append(f"  Error:   `{lt['error'][:100]}`")

        return "\n".join(lines)

    def _cmd_setprofit(self, text: str) -> str:
        parts = text.split()
        if len(parts) < 2:
            return "Usage: `/setprofit <amount>`\nExample: `/setprofit 25`"
        try:
            value = float(parts[1])
        except ValueError:
            return f"❌ '{parts[1]}' is not a number. Example: `/setprofit 25`"
        if value < 0:
            return "❌ Amount must be ≥ 0."

        old = bot_state.min_profit_usd
        bot_state.set_min_profit(value)
        return (
            f"✅ Min-profit base floor: `${old:,.2f}` → `${value:,.2f}`\n"
            f"_Active floor = ${value:,.2f} + 2.5 × gas cost_"
        )

    def _cmd_toggle(self) -> str:
        now_paused = bot_state.toggle_pause()
        return (
            "⏸ Scanner *PAUSED*. Use /toggle again to resume."
            if now_paused else
            "▶ Scanner *RESUMED*."
        )

    def _cmd_gas(self) -> str:
        gas = bot_state.last_gas
        lines = [
            "*⛽ Network Gas*",
            f"_Priority-tip multiplier: ×{PRIORITY_TIP_MULT:.2f} (Fast +30%)_",
            "",
        ]
        if not gas:
            lines.append("_no gas snapshot yet — wait one scan cycle._")
            return "\n".join(lines)

        for chain, info in sorted(gas.items()):
            base = info.get("base_fee_gwei")
            tip  = info.get("tip_gwei")
            gp   = info.get("gas_price_gwei")
            agg  = info.get("aggressive_tip_gwei")
            age  = time.time() - info.get("updated_at", 0)

            lines.append(f"*{chain}*  _({age:.0f}s ago)_")
            if gp is not None:
                lines.append(f"  gasPrice:       `{gp:.4f}` gwei")
            if base is not None:
                lines.append(f"  baseFee:        `{base:.4f}` gwei")
            if tip is not None:
                lines.append(f"  network tip:    `{tip:.4f}` gwei")
            if agg is not None:
                lines.append(f"  *bot's tip:*    `{agg:.4f}` gwei")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------
    def _handle(self, text: str) -> str | None:
        text = (text or "").strip()
        if not text.startswith("/"):
            return None
        # Strip @BotName suffix that group chats append (/status@MyBot)
        head = text.split()[0].split("@")[0].lower()

        if head == "/status":
            return self._cmd_status()
        if head == "/setprofit":
            return self._cmd_setprofit(text)
        if head == "/toggle":
            return self._cmd_toggle()
        if head == "/gas":
            return self._cmd_gas()
        if head in ("/help", "/start"):
            return self._cmd_help()
        return None

    # ------------------------------------------------------------------
    # Long-poll loop
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        # Drain any backlog so old messages from before startup are skipped.
        try:
            r = requests.get(
                f"{self.api}/getUpdates",
                params={"timeout": 0, "offset": -1},
                timeout=10,
            )
            data = r.json()
            for upd in data.get("result", []):
                if upd["update_id"] > self.last_update_id:
                    self.last_update_id = upd["update_id"]
        except Exception as exc:
            logger.warning(f"[TgCmd] Initial drain failed: {exc}")

        logger.info(
            f"[TgCmd] Listening for commands from chat_id={self.owner_chat_id}"
        )

        while True:
            try:
                resp = requests.get(
                    f"{self.api}/getUpdates",
                    params={
                        "offset":  self.last_update_id + 1,
                        "timeout": LONG_POLL_TIMEOUT,
                    },
                    timeout=HTTP_TIMEOUT,
                )
                data = resp.json()
                if not data.get("ok"):
                    logger.warning(f"[TgCmd] API said: {data}")
                    time.sleep(5)
                    continue

                for upd in data.get("result", []):
                    self.last_update_id = upd["update_id"]
                    msg = upd.get("message") or upd.get("channel_post")
                    if not msg:
                        continue

                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != self.owner_chat_id:
                        # Ignore commands from any chat that isn't the owner
                        continue

                    text  = msg.get("text", "")
                    reply = self._handle(text)
                    if reply:
                        self._send(reply)
                        logger.info(f"[TgCmd] Handled: {text.split()[0] if text else '?'}")

            except requests.Timeout:
                # Long-poll expired with no messages — perfectly normal
                continue
            except Exception as exc:
                logger.warning(f"[TgCmd] Poll error: {exc}")
                time.sleep(5)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> threading.Thread:
        """Spawn the listener thread (daemon). Returns the thread."""
        t = threading.Thread(target=self._poll, name="tg-cmd-listener", daemon=True)
        t.start()
        return t


def start_telegram_command_listener(bot_token: str, chat_id: str) -> TelegramCommandListener:
    """Convenience: build, start, and return the listener."""
    listener = TelegramCommandListener(bot_token, chat_id)
    listener.start()
    return listener
