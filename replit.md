# DeFi Multi-Chain Arbitrage Hunter — Pro

## Overview

A 100% autonomous Python bot that scans multiple DEXes across Polygon, Arbitrum, and Base
for same-chain arbitrage opportunities and auto-fires flash-loan trades through a deployed
`FlashLoanExecutor.sol` contract. Runs 24/7 on Replit with a production WSGI server keeping
the container awake, sends Telegram alerts for trades / heartbeats / 4h summaries, and
accepts live remote-control commands from the owner's Telegram chat.

## Stack

- **Language**: Python 3.11
- **Blockchain**: Web3.py (EVM-compatible chains)
- **Web server**: Flask + waitress (production WSGI, port 8080)
- **Notifications**: Telegram Bot API via `requests` (long-poll for commands)
- **Concurrency**: `threading` — async trade execution + Telegram listener + keep-alive
- **Secrets management**: Replit Secrets (`os.getenv`)

## Project Structure

```
main.py                       # Entry point — scan loop, async trade launcher,
                              # 4h summary + 6h heartbeat schedulers
keep_alive.py                 # waitress WSGI server on port 8080
                              #   /        → "Bot is Active"
                              #   /health  → "OK"  (uptime-pinger target)
config.py                     # 43-token watchlist, DEX configs, dynamic profit base
monitor/
  __init__.py                 # Package re-exports
  bot_state.py                # Thread-safe shared state singleton
  telegram_commands.py        # Long-poll listener: /status /setprofit /toggle /gas /help
  telegram_notifier.py        # Alerts, trade-executed, 4h summary, 6h heartbeat
  balance_checker.py          # Native balance queries on each chain
  price_hunter.py             # Cross-DEX price scan + dynamic-floor opportunity filter
  flash_loan.py               # Per-chain MEV-protected RPC, EIP-1559 ×1.30 tip,
                              # local nonce manager, eth_call pre-flight, broadcast
  trade_history.py            # 5-min cooldown per pair after revert
  keepalive.py                # (legacy stub kept for re-export compatibility)
contracts/
  FlashLoanExecutor.sol       # Aave V3 flash-loan + multi-DEX swap executor
artifacts/
  api-server/                 # (Node) auxiliary API on port 3001
  mockup-sandbox/             # (Vite) UI preview server
```

## Required Secrets (Replit Secrets)

| Key                         | Description                                                 |
|-----------------------------|-------------------------------------------------------------|
| `WALLET_ADDRESS`            | EVM wallet that owns the executor contract (0x...)          |
| `PRIVATE_KEY`               | Private key for `WALLET_ADDRESS` — signs trade transactions |
| `TELEGRAM_BOT_TOKEN`        | From @BotFather                                             |
| `TELEGRAM_CHAT_ID`          | Owner chat — receives alerts AND authorises commands        |
| `EXECUTOR_CONTRACT_ADDRESS` | Deployed `FlashLoanExecutor.sol` address                    |

## Optional Secrets — MEV-Protection / Speed

When set, ALL trade broadcasts on that chain go through the private endpoint instead of
the public mempool, hiding the tx from sandwich bots.

| Key                        | Description                                              |
|----------------------------|----------------------------------------------------------|
| `PRIVATE_RPC_URL_POLYGON`  | e.g. FastLane MEV Protect endpoint                       |
| `PRIVATE_RPC_URL_ARBITRUM` | e.g. Flashbots Protect endpoint                          |
| `PRIVATE_RPC_URL_BASE`     | Private Base endpoint                                    |
| `PRIVATE_RPC_URL`          | Legacy fallback — treated as Polygon's private RPC       |

If the configured private RPC is unreachable, the bot falls back to the public RPC for
that chain and logs a warning, so a misconfigured bundle endpoint never blocks trading.

## Optional Environment Variables

| Key              | Default | Description                                           |
|------------------|---------|-------------------------------------------------------|
| `RUN_ONCE`       | `false` | `true` → single scan then exit                        |
| `POLL_INTERVAL`  | `60`    | Seconds between scan cycles                           |
| `PRICE_SNAPSHOT` | `false` | Send full price table to Telegram on no-arb cycles    |

## Telegram Command Center

The owner chat (matching `TELEGRAM_CHAT_ID`) can issue these commands at any time —
messages from any other chat are silently ignored.

| Command              | Action                                                       |
|----------------------|--------------------------------------------------------------|
| `/status`            | Wallet balances, last trade attempted, scanning vs paused    |
| `/setprofit <usd>`   | Change the min-profit base floor (default 5)                 |
| `/toggle`            | Pause / resume the scanner (in-flight trades complete)       |
| `/gas`               | Per-chain gas price + the bot's actual priority tip          |
| `/help`              | Show command list                                            |

## Pro Features (current)

### Dynamic Profit Floor
The active profit gate is `MIN_NET_PROFIT_USD + 2.5 × estimated gas cost`, recalculated
per opportunity. When the network is busy (high gas), the bot automatically demands more
profit, so it never burns money on marginal trades. The base value is live-mutable from
Telegram via `/setprofit`.

### MEV-Protected Broadcasts
Per-chain private RPC endpoints (e.g. FastLane on Polygon, Flashbots Protect on Arbitrum)
hide trades from public-mempool sandwich bots. Configured chains show `🛡 PRIVATE bundle`
in the startup banner; unconfigured chains show `public mempool`.

### Hyper-Speed Gas (×1.30 priority tip)
EIP-1559 `maxPriorityFeePerGas = network_tip × 1.30` — beats other arb bots that use
"Fast" tip at par, landing our tx at the top of the validator queue.

### Async Trade Execution
Trades broadcast in daemon threads — the scanner never pauses to wait for receipts, so
no opportunity is missed while a previous trade is mining.

### Local Nonce Manager
Tracks nonces in memory across consecutive trades on the same chain — no redundant
on-chain `eth_getTransactionCount` round-trip between back-to-back fires.

### Pre-flight Simulation (`eth_call`)
Every trade is simulated against the live state before broadcast. If the profit has
already been arbed away, we abort for free instead of wasting gas on a revert.

### 5-Minute Cooldown
After a revert, the failing token-pair / DEX-route combination is locked out for 5
minutes so the bot stops hammering a broken route.

### 4-Hour Rolling Summary + 6-Hour Heartbeat
- **Summary** every 4h: cycles run, opportunities found, trades attempted/succeeded,
  total estimated profit.
- **Heartbeat** every 6h: `💓 System Healthy` pulse with opportunities/executed counts
  for the window — a positive confirmation the scanner is actually scanning.

### 24/7 Keep-Alive (`waitress` on port 8080)
Production-grade WSGI server (no Flask dev-server warnings). External uptime monitors
hit `/health → "OK"` to keep the Replit container awake.

**Pinger URL:** `https://1ac8f5dd-3866-488c-838c-c20935a95850-00-mz6k8j2gyym.riker.replit.dev/health`

## Supported Chains & DEXes

| Chain    | Native | DEXes                          | Low-balance flag |
|----------|--------|--------------------------------|------------------|
| Polygon  | MATIC  | Uniswap V3, SushiSwap V3       | < 10 MATIC       |
| Arbitrum | ETH    | Uniswap V3, SushiSwap V3       | < 0.01 ETH       |
| Base     | ETH    | Uniswap V3, PancakeSwap V3     | < 0.01 ETH       |

Watchlist: 43 symbols (see `config.py` `WATCHLIST`).

## How to Run

1. Set all required secrets (and any optional MEV-protection RPCs) in Replit → Secrets.
2. The "Start application" workflow runs `python main.py` automatically.
3. The bot will:
   - Scan every 60 seconds (configurable via `POLL_INTERVAL`)
   - Auto-fire profitable same-chain opportunities through the executor contract
   - Send Telegram alerts for every trade
   - Send a 4h summary + 6h heartbeat
   - Accept commands from the owner's Telegram chat
4. Point an external uptime monitor at `/health` to keep the container awake 24/7.

## Recent Architecture Changes

- **Apr 26 2026 — Pro upgrades**: Telegram command center, dynamic profit floor,
  per-chain MEV-protected RPC bundles, ×1.30 priority tip, 6-hour heartbeat,
  waitress production WSGI server with `/health → "OK"`.
- **Earlier**: 43-token watchlist, autonomous auto-fire, 5-min cooldown, async daemon
  trade threads, NonceManager, eth_call pre-flight simulation, EIP-1559 base support,
  Flask keep-alive on 8080.
