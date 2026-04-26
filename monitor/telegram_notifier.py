"""
telegram_notifier.py
--------------------
Sends two types of Telegram messages:
  1. Balance report   — multi-chain native-token balances
  2. Arbitrage alert  — profitable cross-DEX opportunity with DEX links
"""

import requests

TELEGRAM_API_BASE = "https://api.telegram.org"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _send(bot_token: str, chat_id: str, text: str) -> None:
    """POST a message to Telegram. Raises requests.HTTPError on failure."""
    url     = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
        # Disable web-page previews so DEX links don't generate noisy previews
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    print(f"[Telegram] Message sent (HTTP {resp.status_code}).")


# ---------------------------------------------------------------------------
# Balance report
# ---------------------------------------------------------------------------

def _build_balance_message(results: list[dict]) -> str:
    lines    = ["*DeFi Wallet Balance Report*", ""]
    any_low  = False

    for r in results:
        if r["error"]:
            lines.append(f"*{r['name']}* — ERROR: `{r['error']}`")
            continue

        flag = ""
        if r["is_low"]:
            flag    = "  ⚠ LOW BALANCE"
            any_low = True

        lines.append(
            f"*{r['name']}* ({r['native_token']}): "
            f"`{r['balance']:.6f}` {r['native_token']}{flag}"
        )
        lines.append(f"  Threshold: {r['low_balance_threshold']} {r['native_token']}")

    lines.append("")
    lines.append(
        "ACTION REQUIRED: Top up wallet." if any_low
        else "All balances are healthy."
    )
    return "\n".join(lines)


def send_telegram_report(bot_token: str, chat_id: str, results: list[dict]) -> None:
    """Send the multi-chain wallet balance report."""
    _send(bot_token, chat_id, _build_balance_message(results))


# ---------------------------------------------------------------------------
# Arbitrage alert
# ---------------------------------------------------------------------------

def _build_arb_message(opp: dict) -> str:
    """
    Format a single arbitrage opportunity into a Telegram alert.

    Fields shown:
      - Pair / Networks / DEXes
      - Buy price  / Sell price
      - Spread %
      - Gross profit, flash-loan fee, gas fees, NET profit
      - Direct DEX swap links
    """
    symbol      = opp["symbol"]
    buy_price   = opp["buy_price"]
    sell_price  = opp["sell_price"]
    spread      = opp["spread_pct"]
    gross       = opp["gross_profit"]
    fl_fee      = opp["flash_loan_fee"]
    gas         = opp["total_gas_usd"]
    net         = opp["net_profit"]
    size        = opp["trade_size_usd"]
    buy_dex     = opp["buy_dex_name"]
    sell_dex    = opp["sell_dex_name"]
    buy_chain   = opp["buy_chain"]
    sell_chain  = opp["sell_chain"]
    buy_url     = opp["buy_url"]
    sell_url    = opp["sell_url"]

    # Show all DEX prices in the watchlist scan
    price_lines = "\n".join(
        f"  • {dex}: ${price:,.4f}"
        for dex, price in opp.get("all_prices", {}).items()
    )

    same_chain = buy_chain == sell_chain
    chain_badge = f"✅ SAME-CHAIN ({buy_chain})" if same_chain else f"⛓ CROSS-CHAIN ({buy_chain} → {sell_chain})"

    return (
        f"*🔥 ARBITRAGE OPPORTUNITY*\n"
        f"*{chain_badge}*\n"
        f"\n"
        f"*Pair:*    {symbol}/USDC\n"
        f"*Spread:*  {spread:.3f}%\n"
        f"\n"
        f"*BUY*   on {buy_dex} ({buy_chain})\n"
        f"  Price: `${buy_price:,.4f}`\n"
        f"  [Open swap ↗]({buy_url})\n"
        f"\n"
        f"*SELL*  on {sell_dex} ({sell_chain})\n"
        f"  Price: `${sell_price:,.4f}`\n"
        f"  [Open swap ↗]({sell_url})\n"
        f"\n"
        f"*All DEX prices:*\n{price_lines}\n"
        f"\n"
        f"*P&L on ${size:,.0f} trade:*\n"
        f"  Gross profit:    `${gross:,.2f}`\n"
        f"  Flash-loan fee: `-${fl_fee:,.2f}`\n"
        f"  Gas cost:       `-${gas:,.4f}`\n"
        f"  ──────────────────────────\n"
        f"  *Est. Net Profit: `${net:,.2f}`*\n"
        + (f"\n_⚡ Flash-loan executable — check console for Y/N prompt_"
           if same_chain else "")
    )


def send_arb_alerts(bot_token: str, chat_id: str,
                    opportunities: list[dict]) -> int:
    """
    Send one Telegram alert per opportunity.
    Returns the number of alerts sent.
    """
    sent = 0
    for opp in opportunities:
        msg = _build_arb_message(opp)
        _send(bot_token, chat_id, msg)
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# Trade executed notification
# ---------------------------------------------------------------------------

def send_trade_executed(bot_token: str, chat_id: str,
                        symbol: str, chain: str,
                        estimated_profit_usd: float,
                        tx_hash: str, explorer_url: str) -> None:
    """Send a notification immediately after a flash-loan trade is broadcast."""
    msg = (
        f"*⚡ TRADE EXECUTED*\n"
        f"\n"
        f"*Chain:*   {chain}\n"
        f"*Token:*   {symbol}/USDC\n"
        f"*Est. Net Profit:* `${estimated_profit_usd:,.2f}`\n"
        f"\n"
        f"*Tx Hash:*\n`{tx_hash}`\n"
        f"\n"
        f"[View on Block Explorer ↗]({explorer_url})"
    )
    try:
        _send(bot_token, chat_id, msg)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            f"[Telegram] Execution notification failed: {exc}"
        )


# ---------------------------------------------------------------------------
# 4-hour rolling summary
# ---------------------------------------------------------------------------

def send_4h_summary(bot_token: str, chat_id: str, stats: dict) -> None:
    """
    Send a 4-hour summary of scanner activity.

    Expected keys in stats:
      cycles_run, same_chain_found, cross_chain_filtered,
      trades_attempted, trades_succeeded, trades_failed,
      total_est_profit_usd, uptime_hours
    """
    cycles          = stats.get("cycles_run", 0)
    same_chain      = stats.get("same_chain_found", 0)
    cross_chain     = stats.get("cross_chain_filtered", 0)
    attempted       = stats.get("trades_attempted", 0)
    succeeded       = stats.get("trades_succeeded", 0)
    failed          = stats.get("trades_failed", 0)
    est_profit      = stats.get("total_est_profit_usd", 0.0)
    uptime_h        = stats.get("uptime_hours", 0.0)

    msg = (
        f"*📊 4-Hour Activity Summary*\n"
        f"\n"
        f"*Uptime:*            {uptime_h:.1f}h\n"
        f"*Scan cycles:*       {cycles}\n"
        f"\n"
        f"*Same-chain opps:*   {same_chain}\n"
        f"*Cross-chain gaps:*  {cross_chain} _(filtered)_\n"
        f"\n"
        f"*Trades attempted:*  {attempted}\n"
        f"  ✅ Succeeded:      {succeeded}\n"
        f"  ❌ Failed/reverted: {failed}\n"
        f"\n"
        f"*Total est. profit:* `${est_profit:,.2f}`\n"
        f"\n"
        f"_Next summary in 4h._"
    )
    try:
        _send(bot_token, chat_id, msg)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            f"[Telegram] 4h summary failed: {exc}"
        )


# ---------------------------------------------------------------------------
# 6-hour heartbeat (Pulse / Health Check)
# ---------------------------------------------------------------------------

def send_heartbeat(bot_token: str, chat_id: str,
                   opportunities_found: int,
                   trades_executed: int,
                   window_hours: float = 6.0) -> None:
    """
    Pulse message confirming the bot is alive — sent every 6 hours.

    Lists opportunities found vs. executed in the rolling window, so the
    user can confirm the scanner is genuinely active (not silently stalled).
    """
    msg = (
        f"*💓 System Healthy*\n"
        f"\n"
        f"_Last {window_hours:.1f}h activity:_\n"
        f"  Opportunities found: `{opportunities_found}`\n"
        f"  Trades executed:     `{trades_executed}`\n"
        f"\n"
        f"_Next pulse in 6h._"
    )
    try:
        _send(bot_token, chat_id, msg)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            f"[Telegram] Heartbeat failed: {exc}"
        )


# ---------------------------------------------------------------------------
# Price snapshot (sent when no opportunities found)
# ---------------------------------------------------------------------------

def send_price_snapshot(bot_token: str, chat_id: str,
                        all_prices: list[dict]) -> None:
    """Send a compact price table across all DEXes (no-opportunity cycle)."""
    if not all_prices:
        return

    # Group by symbol
    by_symbol: dict[str, list[dict]] = {}
    for rec in all_prices:
        by_symbol.setdefault(rec["symbol"], []).append(rec)

    lines = ["*Multi-DEX Price Snapshot*", ""]
    for symbol, records in sorted(by_symbol.items()):
        lines.append(f"*{symbol}*")
        for r in records:
            lines.append(f"  {r['dex_name']:12s} ({r['chain']:8s}): `${r['price_usd']:,.4f}`")

    lines.append("\n_No profitable opportunities this cycle._")
    _send(bot_token, chat_id, "\n".join(lines))
