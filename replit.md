# DeFi Multi-Chain Wallet Monitor

## Overview

A Python script that monitors a single EVM wallet address across multiple
blockchain networks (Polygon, Arbitrum, Base) and sends a consolidated
balance report via Telegram.

## Stack

- **Language**: Python 3.11
- **Blockchain**: Web3.py (EVM-compatible chains)
- **Notifications**: Telegram Bot API via `requests`
- **Secrets management**: Replit Secrets (environment variables)

## Project Structure

```
main.py                    # Entry point — loads config, runs polling loop
monitor/
  __init__.py              # Package exports
  balance_checker.py       # Web3.py multi-chain balance queries
  telegram_notifier.py     # Formats and sends Telegram reports
  flash_loan.py            # Stub for future Flash Loan execution module
requirements.txt           # Python dependencies (web3, requests)
```

## Required Secrets (Replit Secrets)

| Key                  | Description                                      |
|----------------------|--------------------------------------------------|
| `WALLET_ADDRESS`     | EVM wallet address to monitor (0x...)            |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from @BotFather           |
| `TELEGRAM_CHAT_ID`   | Chat ID that receives the balance alerts         |

## Optional Environment Variables

| Key        | Default | Description                                      |
|------------|---------|--------------------------------------------------|
| `RUN_ONCE` | `false` | Set to `true` to check once and exit immediately |

## Supported Chains

| Network  | Native Token | Low-balance Threshold |
|----------|--------------|-----------------------|
| Polygon  | MATIC        | 10 MATIC              |
| Arbitrum | ETH          | 0.01 ETH              |
| Base     | ETH          | 0.01 ETH              |

To add more chains, edit the `CHAINS` list in `monitor/balance_checker.py`.

## How to Run

1. Set the three secrets in the Replit Secrets tab.
2. Press **Run** (or use the "Start application" workflow).
3. The monitor will check all chains every 60 seconds and send one
   Telegram message per cycle.

## Adding a Flash Loan Module

The `monitor/flash_loan.py` file contains a `FlashLoanExecutor` stub class.
Subclass it or fill in the `execute()` and `on_flash_loan_received()` methods
with your smart-contract interaction logic.  Store the wallet private key as
a Replit Secret — never hard-code it.
