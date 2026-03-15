"""
telegram_notifier.py
--------------------
Sends a formatted multi-chain balance report to a Telegram chat
via the Telegram Bot API.
"""

import requests

TELEGRAM_API_BASE = "https://api.telegram.org"


def _build_message(results: list[dict]) -> str:
    """
    Construct a Markdown-formatted Telegram message from chain results.
    """
    lines = ["*DeFi Wallet Balance Report*", ""]

    any_low = False
    for r in results:
        if r["error"]:
            lines.append(f"*{r['name']}* — ERROR: `{r['error']}`")
            continue

        status_icon = ""
        if r["is_low"]:
            status_icon = " WARNING: LOW BALANCE"
            any_low = True

        lines.append(
            f"*{r['name']}* ({r['native_token']}): "
            f"`{r['balance']:.6f}` {r['native_token']}"
            f"{status_icon}"
        )
        lines.append(
            f"  Threshold: {r['low_balance_threshold']} {r['native_token']}"
        )

    lines.append("")
    if any_low:
        lines.append("ACTION REQUIRED: One or more balances are below threshold.")
    else:
        lines.append("All balances are healthy.")

    return "\n".join(lines)


def send_telegram_report(
    bot_token: str,
    chat_id: str,
    results: list[dict],
) -> None:
    """
    Send a single Telegram message containing the full multi-chain report.

    Raises requests.HTTPError on a non-2xx response.
    """
    message = _build_message(results)
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()
    print(f"[Telegram] Report sent successfully (status {response.status_code}).")
