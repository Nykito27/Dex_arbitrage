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
import os
from web3 import Web3

# Private RPC for Polygon — bypasses rate-limited public nodes for faster scanning.
# Set PRIVATE_RPC_URL in Replit Secrets to activate.
_PRIVATE_RPC_URL: str = os.getenv("PRIVATE_RPC_URL", "").strip()

# Chain enable filter — comma-separated list. Lets you skip chains where the
# wallet is unfunded (e.g. set ENABLED_CHAINS=Polygon to ignore Arb/Base
# entirely until those wallets are topped up). Default = all chains.
_ENABLED_CHAINS: set[str] = {
    c.strip()
    for c in os.getenv("ENABLED_CHAINS", "Polygon,Arbitrum,Base").split(",")
    if c.strip()
}

from config import (
    TOKENS,
    DEXES,
    WATCHLIST,
    FLASH_LOAN_FEE_BPS,
    TRADE_SIZE_USD,
)
from .bot_state import bot_state

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

_ERC20_BALANCE_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# Uniswap V3-compatible QuoterV2 (also used by SushiSwap V3, PancakeSwap V3).
# Returns the REAL swap output for a given input size — accounts for slippage,
# tick crossings, and pool depth. We call it via .call() (eth_call), which
# captures the result even though the function is marked nonpayable.
_QUOTER_V2_ABI = [
    {
        "inputs": [{
            "components": [
                {"internalType": "address", "name": "tokenIn",          "type": "address"},
                {"internalType": "address", "name": "tokenOut",         "type": "address"},
                {"internalType": "uint256", "name": "amountIn",         "type": "uint256"},
                {"internalType": "uint24",  "name": "fee",              "type": "uint24"},
                {"internalType": "uint160", "name": "sqrtPriceLimitX96","type": "uint160"},
            ],
            "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
            "name": "params",
            "type": "tuple",
        }],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut",               "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After",       "type": "uint160"},
            {"internalType": "uint32",  "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate",             "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

_NULL_ADDR = "0x0000000000000000000000000000000000000000"

# Pools with less than this much USDC are considered stale / garbage-priced
_MIN_POOL_USDC_RAW_1000 = 1_000  # USD — checked via USDC.balanceOf(pool)


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

                # Liquidity gate: skip pools with < $1,000 USDC reserve
                # (catches stale/abandoned pools that give garbage prices)
                try:
                    usdc_contract = w3.eth.contract(
                        address=addr_usdc, abi=_ERC20_BALANCE_ABI
                    )
                    usdc_balance_raw = usdc_contract.functions.balanceOf(pool_addr).call()
                    usdc_balance_usd = usdc_balance_raw / (10 ** usdc["decimals"])
                    if usdc_balance_usd < _MIN_POOL_USDC_RAW_1000:
                        logger.debug(
                            f"[{label}] fee={fee}: thin pool "
                            f"(${usdc_balance_usd:,.0f} USDC), skipping"
                        )
                        continue
                except Exception:
                    pass  # if check fails, proceed with price fetch anyway

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
        # Use private RPC for Polygon DEXes when PRIVATE_RPC_URL is configured.
        chain_rpc = (
            _PRIVATE_RPC_URL
            if _PRIVATE_RPC_URL and chain_name == "Polygon"
            else dex_cfg["rpc_url"]
        )
        w3 = Web3(Web3.HTTPProvider(chain_rpc, request_kwargs={"timeout": 8}))
        if not w3.is_connected():
            if _PRIVATE_RPC_URL and chain_name == "Polygon":
                logger.warning(
                    f"[{dex_key}] Private RPC unreachable ({chain_rpc}), "
                    "falling back to public node."
                )
                w3 = Web3(Web3.HTTPProvider(dex_cfg["rpc_url"],
                                            request_kwargs={"timeout": 12}))
            if not w3.is_connected():
                logger.error(f"[{dex_key}] RPC unreachable: {chain_rpc}")
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

        # ── Cache gas snapshot for the /gas Telegram command ────────────────
        try:
            block       = w3.eth.get_block("latest")
            base_fee_wei = block.get("baseFeePerGas")
            try:
                tip_wei = w3.eth.max_priority_fee
            except Exception:
                tip_wei = None

            bot_state.set_gas(chain_name, {
                "gas_price_gwei": gas_price_wei / 1e9 if gas_price_wei else None,
                "base_fee_gwei":  base_fee_wei / 1e9 if base_fee_wei else None,
                "tip_gwei":       tip_wei / 1e9 if tip_wei else None,
                # Mirror the multiplier in flash_loan.FlashLoanExecutor.PRIORITY_TIP_MULTIPLIER
                "aggressive_tip_gwei": (tip_wei * 1.30 / 1e9) if tip_wei else None,
                "native_price_usd":    native_price,
            })
        except Exception:
            pass  # gas cache is best-effort; never fail a scan over it

        print(f"  [{dex_key}] native={native_price:.4f} USD  "
              f"gas≈${gas_cost_usd:.4f}")

        for symbol in WATCHLIST:
            if symbol not in tokens:
                continue
            token = tokens[symbol]

            try:
                result = _get_pool_price_usd(
                    w3, factories, dex_cfg["fee_tiers"],
                    token, usdc, dex_key,
                )
            except Exception as sym_exc:
                logger.warning(
                    f"[{dex_key}] Skipping {symbol}: {sym_exc}"
                )
                continue

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
                print(f"  [{dex_key}] {symbol:8s} = ${result['price_usd']:,.4f} "
                      f"(fee {result['fee']})")
            else:
                logger.debug(f"[{dex_key}] No pool found for {symbol}/USDC")

    except Exception as exc:
        logger.error(f"[{dex_key}] Fatal error: {exc}")

    return records


# ---------------------------------------------------------------------------
# Depth-aware swap-output validation (QuoterV2)
# ---------------------------------------------------------------------------

def _quote_exact_input_single(w3: Web3,
                               quoter_addr: str,
                               token_in: str,
                               token_out: str,
                               fee: int,
                               amount_in_raw: int) -> int | None:
    """
    Call QuoterV2.quoteExactInputSingle to get the REAL swap output for a
    given input size on a specific pool. Returns amount_out_raw, or None on
    any failure (insufficient liquidity, RPC error, bad address, etc.).
    """
    try:
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(quoter_addr),
            abi=_QUOTER_V2_ABI,
        )
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            int(amount_in_raw),
            int(fee),
            0,  # sqrtPriceLimitX96 = 0 → no price limit
        )
        result = quoter.functions.quoteExactInputSingle(params).call()
        # Result tuple: (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)
        return int(result[0])
    except Exception as exc:
        logger.debug(f"[Quoter] {quoter_addr} fee={fee}: {exc}")
        return None


def _validate_with_quoter(buy_rec: dict,
                           sell_rec: dict,
                           trade_size_usd: float) -> float | None:
    """
    Compute the REAL net profit by simulating both swap legs through the
    V3 QuoterV2 contracts of the buy and sell DEXes. This accounts for:
      - actual pool depth at $TRADE_SIZE_USD borrow size
      - tick crossings (large trades cross multiple price ticks)
      - per-pool fee tier slippage

    Returns net profit in USD, or None if validation can't run (e.g.
    quoter not configured, RPC error, insufficient liquidity).
    Caller should treat None as "use spot estimate as fallback".
    """
    buy_cfg  = DEXES.get(buy_rec["dex"])
    sell_cfg = DEXES.get(sell_rec["dex"])
    if not buy_cfg or not sell_cfg:
        return None

    buy_quoter  = buy_cfg.get("quoter_v2")
    sell_quoter = sell_cfg.get("quoter_v2")
    if not buy_quoter or not sell_quoter:
        return None  # depth check unavailable for this DEX combination

    # USDC has 6 decimals on every chain we support.
    usdc_in_raw = int(trade_size_usd * 1_000_000)

    try:
        # ── Leg 1: USDC → base token on the buy DEX ────────────────────────
        w3_buy = Web3(Web3.HTTPProvider(
            buy_cfg["rpc_url"], request_kwargs={"timeout": 8}
        ))
        base_out_raw = _quote_exact_input_single(
            w3_buy, buy_quoter,
            buy_rec["usdc_address"], buy_rec["token_address"],
            buy_rec["fee_tier"], usdc_in_raw,
        )
        if not base_out_raw:
            return None

        # ── Leg 2: base token → USDC on the sell DEX ───────────────────────
        w3_sell = Web3(Web3.HTTPProvider(
            sell_cfg["rpc_url"], request_kwargs={"timeout": 8}
        ))
        usdc_out_raw = _quote_exact_input_single(
            w3_sell, sell_quoter,
            sell_rec["token_address"], sell_rec["usdc_address"],
            sell_rec["fee_tier"], base_out_raw,
        )
        if usdc_out_raw is None:
            return None

        usdc_returned = usdc_out_raw / 1_000_000
        gross_real    = usdc_returned - trade_size_usd
        flash_fee     = trade_size_usd * (FLASH_LOAN_FEE_BPS / 10_000)
        gas_total     = buy_rec["gas_cost_usd"] + sell_rec["gas_cost_usd"]
        return gross_real - flash_fee - gas_total

    except Exception as exc:
        logger.debug(f"[Quoter] Validation aborted: {exc}")
        return None


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

        # Dynamic profit floor: base (live-mutable via /setprofit) + 2.5 × gas
        # so the bot demands more profit when the network is busy.
        dynamic_floor = bot_state.dynamic_floor(total_gas)

        if net_profit > dynamic_floor:
            # ── Depth-aware validation ──────────────────────────────────────
            # The slot0 spot price assumes infinite liquidity. At $10k borrow
            # size the actual swap output is typically lower (you eat 0.05–
            # 0.30% of slippage per leg depending on pool depth). Use the
            # V3 QuoterV2 to compute the REAL net profit; if it's still
            # above the floor, queue the opp using the more accurate number.
            real_net = _validate_with_quoter(buy_rec, sell_rec, TRADE_SIZE_USD)
            if real_net is not None:
                if real_net < dynamic_floor:
                    logger.info(
                        f"[Quoter] {symbol} dropped — spot says ${net_profit:,.2f}, "
                        f"real swap output says ${real_net:,.2f} "
                        f"< floor ${dynamic_floor:,.2f}"
                    )
                    continue
                logger.info(
                    f"[Quoter] {symbol} VALIDATED — spot ${net_profit:,.2f} "
                    f"→ real ${real_net:,.2f}"
                )
                gross_profit  = real_net + flash_loan_fee + total_gas
                net_profit    = real_net
                profit_source = "quoter"
            else:
                profit_source = "spot"

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
                "min_profit_floor": dynamic_floor,
                "profit_source":    profit_source,  # "quoter" or "spot"
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
        # Skip chains the operator has disabled (e.g. unfunded wallets).
        if dex_cfg["chain"] not in _ENABLED_CHAINS:
            logger.debug(
                f"[{dex_key}] Chain {dex_cfg['chain']} not in ENABLED_CHAINS "
                f"({sorted(_ENABLED_CHAINS)}) — skipping"
            )
            continue
        prices = fetch_prices_for_dex(dex_key, dex_cfg)
        all_prices.extend(prices)

    opportunities = find_arbitrage_opportunities(all_prices)
    return all_prices, opportunities


def get_enabled_chains() -> set[str]:
    """Return the set of chains the scanner is currently configured to scan."""
    return set(_ENABLED_CHAINS)
