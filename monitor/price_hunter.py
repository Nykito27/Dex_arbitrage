"""
price_hunter.py
---------------
Multi-DEX Price Engine.

For every token in the watchlist it fetches the spot price on each configured
DEX by reading the Uniswap V3-compatible pool's slot0 (sqrtPriceX96) via
on-chain view calls — no API keys or external price feeds required.

Supports:
  - Uniswap V3  (Polygon)
  - SushiSwap V3 (Arbitrum)   — identical pool interface to Uniswap V3
  - PancakeSwap V3 (Base)     — identical pool interface to Uniswap V3

Public entry point:
  scan_all_dexes() -> (all_prices: list[dict], opportunities: list[dict])
"""

from __future__ import annotations

import logging
from web3 import Web3

from config import (
    TOKENS,
    DEXES,
    WATCHLIST,
    FLASH_LOAN_FEE_BPS,
    TRADE_SIZE_USD,
    MIN_NET_PROFIT_USD,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Minimal ABI fragments (read-only pool + factory calls)
# ---------------------------------------------------------------------------

_FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24",  "name": "fee",    "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

_POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96",  "type": "uint160"},
            {"internalType": "int24",   "name": "tick",          "type": "int24"},
            {"internalType": "uint16",  "name": "",              "type": "uint16"},
            {"internalType": "uint16",  "name": "",              "type": "uint16"},
            {"internalType": "uint16",  "name": "",              "type": "uint16"},
            {"internalType": "uint8",   "name": "",              "type": "uint8"},
            {"internalType": "bool",    "name": "unlocked",      "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_NULL_ADDR = "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _sqrt_price_to_token0_in_token1(sqrt_price_x96: int,
                                    token0_dec: int,
                                    token1_dec: int) -> float:
    """
    Convert sqrtPriceX96 to a human-readable price:
    "how many token1 does 1 token0 cost?"

    Formula:
        raw_ratio = (sqrtPriceX96 / 2^96)^2
                  = token1_raw / token0_raw
        human_price = raw_ratio * 10^token0_dec / 10^token1_dec
    """
    raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    return raw * (10 ** token0_dec) / (10 ** token1_dec)


# ---------------------------------------------------------------------------
# On-chain price fetching
# ---------------------------------------------------------------------------

def _get_pool_price_usd(w3: Web3,
                        factories: list[str],
                        fee_tiers: list[int],
                        target: dict,
                        usdc: dict,
                        label: str) -> dict | None:
    """
    Try each (factory, fee_tier) combination for a target/USDC pool.
    Tries all factories in order; returns the first valid result or None.
    Returns {"price_usd": float, "fee": int, "pool_address": str} or None.
    """
    addr_target = Web3.to_checksum_address(target["address"])
    addr_usdc   = Web3.to_checksum_address(usdc["address"])

    for factory_addr in factories:
        try:
            factory = w3.eth.contract(
                address=Web3.to_checksum_address(factory_addr),
                abi=_FACTORY_ABI,
            )
        except Exception as exc:
            logger.debug(f"[{label}] Bad factory addr {factory_addr}: {exc}")
            continue

        for fee in fee_tiers:
            try:
                pool_addr = factory.functions.getPool(addr_target, addr_usdc, fee).call()
                if not pool_addr or pool_addr == _NULL_ADDR:
                    continue

                pool = w3.eth.contract(address=pool_addr, abi=_POOL_ABI)
                slot0     = pool.functions.slot0().call()
                sqrt_px96 = slot0[0]
                if sqrt_px96 == 0:
                    continue

                actual_token0 = pool.functions.token0().call().lower()

                if actual_token0 == addr_target.lower():
                    # target is token0, USDC is token1 → price = USDC per target ✓
                    price_usd = _sqrt_price_to_token0_in_token1(
                        sqrt_px96, target["decimals"], usdc["decimals"]
                    )
                else:
                    # USDC is token0, target is token1 → invert for USDC per target
                    price_usdc_in_target = _sqrt_price_to_token0_in_token1(
                        sqrt_px96, usdc["decimals"], target["decimals"]
                    )
                    # Guard against division by near-zero (broken/empty pool)
                    if price_usdc_in_target < 1e-12:
                        logger.debug(f"[{label}] fee={fee}: near-zero intermediate price, skipping")
                        continue
                    price_usd = 1.0 / price_usdc_in_target

                # Sanity range: reject dust pools and obviously wrong prices.
                # Real tracked assets: $0.001 (lowest stablecoin fringe) – $1,000,000 (BTC cap)
                if not (0.001 <= price_usd <= 1_000_000):
                    logger.debug(
                        f"[{label}] fee={fee}: price ${price_usd:.8f} outside sanity range, skipping"
                    )
                    continue

                return {
                    "price_usd":    price_usd,
                    "fee":          fee,
                    "pool_address": pool_addr,
                    "factory":      factory_addr,
                }

            except Exception as exc:
                logger.debug(f"[{label}] factory={factory_addr} fee={fee}: {exc}")

    return None


def _native_token_price_usd(w3: Web3,
                             chain_name: str,
                             factories: list[str],
                             fee_tiers: list[int]) -> float:
    """Return the native gas-token price in USD (for gas cost calculation)."""
    tokens = TOKENS.get(chain_name, {})
    usdc   = tokens.get("USDC")

    native = tokens.get("MATIC") if chain_name == "Polygon" else tokens.get("WETH")

    if not native or not usdc:
        return 0.0

    result = _get_pool_price_usd(w3, factories, fee_tiers, native, usdc, chain_name)
    return result["price_usd"] if result else 0.0


# ---------------------------------------------------------------------------
# Per-DEX scanner
# ---------------------------------------------------------------------------

def fetch_prices_for_dex(dex_key: str, dex_cfg: dict) -> list[dict]:
    """
    Fetch spot prices for all watchlist tokens on one DEX.
    Returns a list of price records (one per found token).
    """
    records     = []
    chain_name  = dex_cfg["chain"]
    tokens      = TOKENS.get(chain_name, {})
    usdc        = tokens.get("USDC")

    if not usdc:
        logger.warning(f"[{dex_key}] No USDC config for chain {chain_name} — skipping.")
        return records

    try:
        w3 = Web3(Web3.HTTPProvider(dex_cfg["rpc_url"],
                                    request_kwargs={"timeout": 12}))
        if not w3.is_connected():
            logger.error(f"[{dex_key}] RPC unreachable: {dex_cfg['rpc_url']}")
            return records

        factories = dex_cfg["factories"]

        native_price = _native_token_price_usd(
            w3, chain_name, factories, dex_cfg["fee_tiers"]
        )

        try:
            gas_price_wei = w3.eth.gas_price
        except Exception:
            gas_price_wei = 0

        gas_cost_usd = (
            gas_price_wei * dex_cfg["gas_units"] * native_price / 1e18
        )

        print(f"  [{dex_key}] native={native_price:.4f} USD  "
              f"gas≈${gas_cost_usd:.4f}")

        for symbol in WATCHLIST:
            if symbol not in tokens:
                continue
            token = tokens[symbol]

            result = _get_pool_price_usd(
                w3, factories, dex_cfg["fee_tiers"],
                token, usdc, dex_key,
            )

            if result:
                records.append({
                    "dex":            dex_key,
                    "dex_name":       dex_cfg["dex_name"],
                    "chain":          chain_name,
                    "symbol":         symbol,
                    "price_usd":      result["price_usd"],
                    "fee_tier":       result["fee"],
                    "pool_address":   result["pool_address"],
                    "token_address":  token["address"],
                    "usdc_address":   usdc["address"],
                    "gas_cost_usd":   gas_cost_usd,
                    "native_price_usd": native_price,
                    "swap_url": dex_cfg["swap_url"].format(
                        token_in=token["address"],
                        token_out=usdc["address"],
                    ),
                })
                print(f"  [{dex_key}] {symbol:6s} = ${result['price_usd']:,.4f} "
                      f"(fee {result['fee']})")
            else:
                logger.debug(f"[{dex_key}] No pool found for {symbol}/USDC")

    except Exception as exc:
        logger.error(f"[{dex_key}] Fatal error: {exc}")

    return records


# ---------------------------------------------------------------------------
# Arbitrage detector
# ---------------------------------------------------------------------------

def find_arbitrage_opportunities(all_prices: list[dict]) -> list[dict]:
    """
    Compare prices across DEXes for the same symbol.
    Returns opportunities where net profit > MIN_NET_PROFIT_USD, sorted
    by net profit descending.
    """
    by_symbol: dict[str, list[dict]] = {}
    for rec in all_prices:
        by_symbol.setdefault(rec["symbol"], []).append(rec)

    opportunities = []

    for symbol, records in by_symbol.items():
        if len(records) < 2:
            continue

        buy_rec  = min(records, key=lambda r: r["price_usd"])   # cheapest
        sell_rec = max(records, key=lambda r: r["price_usd"])   # most expensive

        buy_price  = buy_rec["price_usd"]
        sell_price = sell_rec["price_usd"]

        if buy_price <= 0 or sell_price <= buy_price:
            continue

        spread_pct   = (sell_price - buy_price) / buy_price * 100
        token_amount = TRADE_SIZE_USD / buy_price
        gross_profit = token_amount * (sell_price - buy_price)

        flash_loan_fee = TRADE_SIZE_USD * (FLASH_LOAN_FEE_BPS / 10_000)
        total_gas      = buy_rec["gas_cost_usd"] + sell_rec["gas_cost_usd"]
        net_profit     = gross_profit - flash_loan_fee - total_gas

        if net_profit > MIN_NET_PROFIT_USD:
            opportunities.append({
                "symbol":           symbol,
                "buy_dex":          buy_rec["dex"],          # also used as dex_key
                "buy_dex_name":     buy_rec["dex_name"],
                "buy_chain":        buy_rec["chain"],
                "buy_price":        buy_price,
                "buy_fee":          buy_rec["fee_tier"],     # pool fee tier (e.g. 500)
                "buy_url":          buy_rec["swap_url"],
                "buy_pool":         buy_rec["pool_address"],
                "buy_token_address": buy_rec["token_address"],
                "sell_dex":         sell_rec["dex"],         # also used as dex_key
                "sell_dex_name":    sell_rec["dex_name"],
                "sell_chain":       sell_rec["chain"],
                "sell_price":       sell_price,
                "sell_fee":         sell_rec["fee_tier"],    # pool fee tier
                "sell_url":         sell_rec["swap_url"],
                "sell_pool":        sell_rec["pool_address"],
                "sell_token_address": sell_rec["token_address"],
                "spread_pct":       spread_pct,
                "gross_profit":     gross_profit,
                "flash_loan_fee":   flash_loan_fee,
                "total_gas_usd":    total_gas,
                "net_profit":       net_profit,
                "trade_size_usd":   TRADE_SIZE_USD,
                "token_amount":     token_amount,
                "all_prices":       {r["dex"]: r["price_usd"] for r in records},
            })

    opportunities.sort(key=lambda x: x["net_profit"], reverse=True)
    return opportunities


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scan_all_dexes() -> tuple[list[dict], list[dict]]:
    """
    Scan every DEX in config, return (all_prices, opportunities).
    Opportunities are pre-filtered by MIN_NET_PROFIT_USD.
    """
    all_prices: list[dict] = []
    for dex_key, dex_cfg in DEXES.items():
        prices = fetch_prices_for_dex(dex_key, dex_cfg)
        all_prices.extend(prices)

    opportunities = find_arbitrage_opportunities(all_prices)
    return all_prices, opportunities
