"""
flash_loan.py
-------------
Flash Loan execution engine — speed-optimised for autonomous HFT arbitrage.

Speed & MEV-protection features
-------------------------------
1. Private RPC bundles — per-chain private endpoints (e.g. FastLane on Polygon,
                         Flashbots Protect on Arbitrum) hide trades from
                         sandwich bots in the public mempool.
2. Aggressive gas      — maxPriorityFeePerGas = network tip × 1.30 ("Fast +30%")
                         so our tx lands at the top of the validator queue.
3. Local nonces        — NonceManager tracks nonces in memory; no on-chain
                         roundtrip between consecutive trades on the same chain.
4. Pre-flight sim      — eth_call simulation before broadcast; cancels for free
                         if profit has already been arbed away.

Secrets / env vars (all via os.getenv):
  PRIVATE_KEY                — wallet private key
  EXECUTOR_CONTRACT_ADDRESS  — deployed FlashLoanExecutor.sol address

  Private RPC bundles (any subset; if absent the chain falls back to public):
    PRIVATE_RPC_URL_POLYGON   — preferred for Polygon (e.g. FastLane MEV Protect)
    PRIVATE_RPC_URL_ARBITRUM  — preferred for Arbitrum (e.g. Flashbots Protect)
    PRIVATE_RPC_URL_BASE      — preferred for Base
    PRIVATE_RPC_URL           — legacy fallback (treated as Polygon's private RPC
                                if PRIVATE_RPC_URL_POLYGON is not set)
"""

from __future__ import annotations

import json
import os
import threading
import time
import logging
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Block-explorer base URLs
# ---------------------------------------------------------------------------
EXPLORER = {
    "Polygon":  "https://polygonscan.com/tx/",
    "Arbitrum": "https://arbiscan.io/tx/",
    "Base":     "https://basescan.org/tx/",
}

# ---------------------------------------------------------------------------
# FlashLoanExecutor ABI  (only the two functions we call)
# ---------------------------------------------------------------------------
EXECUTOR_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "routerA",    "type": "address"},
                    {"internalType": "address", "name": "routerB",    "type": "address"},
                    {"internalType": "address", "name": "tokenIn",    "type": "address"},
                    {"internalType": "address", "name": "tokenOut",   "type": "address"},
                    {"internalType": "uint24",  "name": "feeA",       "type": "uint24"},
                    {"internalType": "uint24",  "name": "feeB",       "type": "uint24"},
                    {"internalType": "uint256", "name": "loanAmount", "type": "uint256"},
                    {"internalType": "uint256", "name": "minProfit",  "type": "uint256"},
                ],
                "internalType": "struct FlashLoanExecutor.ArbParams",
                "name":         "params",
                "type":         "tuple",
            }
        ],
        "name":            "initiateArbitrage",
        "outputs":         [],
        "stateMutability": "nonpayable",
        "type":            "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "token",  "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name":            "rescueTokens",
        "outputs":         [],
        "stateMutability": "nonpayable",
        "type":            "function",
    },
]

# ---------------------------------------------------------------------------
# Shared optimal route (updated every scan cycle)
# ---------------------------------------------------------------------------
optimal_route: dict = {}

# ---------------------------------------------------------------------------
# Private RPC resolution — per-chain MEV-protected endpoints
# ---------------------------------------------------------------------------
# When set, ALL trade broadcasts on that chain go through the private endpoint
# instead of the public RPC, hiding the tx from public-mempool sandwich bots.
_PRIVATE_RPCS: dict[str, str] = {
    "Polygon":  (os.getenv("PRIVATE_RPC_URL_POLYGON", "").strip()
                 or os.getenv("PRIVATE_RPC_URL", "").strip()),
    "Arbitrum": os.getenv("PRIVATE_RPC_URL_ARBITRUM", "").strip(),
    "Base":     os.getenv("PRIVATE_RPC_URL_BASE",     "").strip(),
}


def get_private_rpc_status() -> dict[str, bool]:
    """Used by the startup banner — {chain: True if private endpoint configured}."""
    return {chain: bool(url) for chain, url in _PRIVATE_RPCS.items()}


def _get_rpc_for_chain(chain: str) -> tuple[str, bool]:
    """
    Return (rpc_url, is_private) for `chain`.

    Preference: per-chain private RPC → PRIVATE_RPC_URL fallback (Polygon only)
                → first matching public RPC from DEXES config.
    """
    private = _PRIVATE_RPCS.get(chain, "")
    if private:
        return private, True
    for dex_cfg in config.DEXES.values():
        if dex_cfg.get("chain") == chain:
            return dex_cfg["rpc_url"], False
    raise ValueError(f"No RPC URL found for chain '{chain}'")


# ---------------------------------------------------------------------------
# Local Nonce Manager
# ---------------------------------------------------------------------------

class _NonceManager:
    """
    Tracks the wallet nonce in memory so consecutive trades on the same
    chain never stall waiting for the previous tx to be mined.

    Thread-safe — fire() is called from daemon threads.
    """

    def __init__(self) -> None:
        self._lock:   threading.Lock = threading.Lock()
        self._store:  dict[tuple, int] = {}   # (chain, address_lower) → nonce

    def _key(self, chain: str, address: str) -> tuple:
        return (chain, address.lower())

    def get_and_increment(self, chain: str, address: str, w3: Web3) -> int:
        """
        Return the next nonce to use and immediately increment the local counter.
        Initialises from the chain's pending tx count on first call per chain.
        """
        key = self._key(chain, address)
        with self._lock:
            if key not in self._store:
                on_chain = w3.eth.get_transaction_count(address, "pending")
                self._store[key] = on_chain
                logger.debug(
                    f"[Nonce] Initialised {chain}/{address[:8]}... → {on_chain}"
                )
            nonce = self._store[key]
            self._store[key] += 1
        return nonce

    def reset(self, chain: str, address: str, w3: Web3) -> None:
        """Re-sync from chain (call after a nonce-related error)."""
        fresh = w3.eth.get_transaction_count(address, "pending")
        key   = self._key(chain, address)
        with self._lock:
            self._store[key] = fresh
        logger.info(
            f"[Nonce] Reset {chain}/{address[:8]}... → {fresh} (re-synced from chain)"
        )

    def peek(self, chain: str, address: str) -> Optional[int]:
        """Return current local nonce without incrementing, or None if not initialised."""
        return self._store.get(self._key(chain, address))


nonce_manager = _NonceManager()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_decimals(chain: str, symbol: str) -> int:
    return config.TOKENS.get(chain, {}).get(symbol, {}).get("decimals", 18)


def _build_execution_payload(opportunity: dict) -> Optional[dict]:
    """Build the ABI-encoded execution payload for an opportunity."""
    buy_dex_key  = opportunity.get("buy_dex")
    sell_dex_key = opportunity.get("sell_dex")
    if not buy_dex_key or not sell_dex_key:
        return None

    buy_cfg  = config.DEXES.get(buy_dex_key,  {})
    sell_cfg = config.DEXES.get(sell_dex_key, {})

    buy_chain  = buy_cfg.get("chain")
    sell_chain = sell_cfg.get("chain")

    if buy_chain != sell_chain:
        return {
            "executable": False,
            "reason": (
                f"Cross-chain: buy on {buy_chain}, sell on {sell_chain}. "
                "Flash loans are single-chain only."
            ),
        }

    chain         = buy_chain
    router_a      = buy_cfg.get("router")
    router_b      = sell_cfg.get("router")
    aave_provider = buy_cfg.get("aave_addresses_provider")

    # ── Token-direction wiring (matches FlashLoanExecutor.sol semantics) ────
    # Per the contract:
    #   tokenIn  = the asset BORROWED from Aave (and repaid). For X/USDC pairs
    #              this is ALWAYS USDC — we don't borrow the volatile asset.
    #   tokenOut = the intermediate (the base token, e.g. WETH/WBTC/LINK).
    # Leg A: USDC → base on routerA  (the cheaper venue = buy_dex)
    # Leg B: base → USDC on routerB  (the expensive venue = sell_dex)
    # ───────────────────────────────────────────────────────────────────────
    quote_cfg = config.TOKENS.get(chain, {}).get("USDC")
    if not quote_cfg:
        return {
            "executable": False,
            "reason":     f"USDC address not configured for chain {chain}",
        }

    token_in_addr  = quote_cfg["address"]                          # USDC (borrow)
    token_out_addr = opportunity.get("buy_token_address")           # base token
    fee_a          = opportunity.get("buy_fee",  500)
    fee_b          = opportunity.get("sell_fee", 500)

    quote_decimals = quote_cfg.get("decimals", 6)                   # USDC = 6
    # Loan amount + minProfit are denominated in tokenIn = USDC
    loan_amount      = int(opportunity["trade_size_usd"] * (10 ** quote_decimals))
    net_profit_usd   = opportunity["net_profit"]
    min_profit_wei   = int(net_profit_usd * 0.80 * (10 ** quote_decimals))

    return {
        "executable":              True,
        "chain":                   chain,
        "aave_addresses_provider": aave_provider,
        "arb_params": {
            "routerA":    router_a,
            "routerB":    router_b,
            "tokenIn":    token_in_addr,
            "tokenOut":   token_out_addr,
            "feeA":       fee_a,
            "feeB":       fee_b,
            "loanAmount": loan_amount,
            "minProfit":  min_profit_wei,
        },
        "human_readable": {
            "chain":                chain,
            "symbol":               opportunity["symbol"],
            "loan_amount_tokens":   round(opportunity["token_amount"], 6),
            "loan_amount_usd":      opportunity["trade_size_usd"],
            "buy_dex":              buy_dex_key,
            "buy_price_usd":        opportunity["buy_price"],
            "sell_dex":             sell_dex_key,
            "sell_price_usd":       opportunity["sell_price"],
            "spread_pct":           opportunity["spread_pct"],
            "gross_profit_usd":     opportunity["gross_profit"],
            "flash_loan_fee_usd":   opportunity["flash_loan_fee"],
            "gas_cost_usd":         opportunity["total_gas_usd"],
            "estimated_profit_usd": net_profit_usd,
            "min_profit_floor_usd": net_profit_usd * 0.80,
        },
        "abi": EXECUTOR_ABI,
    }


# ---------------------------------------------------------------------------
# Public route storage API
# ---------------------------------------------------------------------------

def store_optimal_route(opportunity: dict | None) -> None:
    global optimal_route

    if opportunity is None:
        optimal_route = {"status": "no_opportunity", "updated_at": time.time()}
        return

    payload    = _build_execution_payload(opportunity)
    executable = payload.get("executable") if payload else False

    optimal_route = {
        "status":     "ready",
        "updated_at": time.time(),
        "symbol":     opportunity["symbol"],
        "trade_size_usd":           opportunity["trade_size_usd"],
        "token_amount":             opportunity["token_amount"],
        "estimated_net_profit_usd": opportunity["net_profit"],
        "buy": {
            "dex":           opportunity["buy_dex"],
            "chain":         opportunity["buy_chain"],
            "price_usd":     opportunity["buy_price"],
            "pool_address":  opportunity["buy_pool"],
            "token_address": opportunity["buy_token_address"],
            "swap_url":      opportunity["buy_url"],
        },
        "sell": {
            "dex":           opportunity["sell_dex"],
            "chain":         opportunity["sell_chain"],
            "price_usd":     opportunity["sell_price"],
            "pool_address":  opportunity["sell_pool"],
            "token_address": opportunity["sell_token_address"],
            "swap_url":      opportunity["sell_url"],
        },
        "execution": payload,
    }

    logger.info(
        f"[FlashLoan] Route stored: {opportunity['symbol']} | "
        f"buy {opportunity['buy_chain']} @${opportunity['buy_price']:,.4f} | "
        f"sell {opportunity['sell_chain']} @${opportunity['sell_price']:,.4f} | "
        f"net≈${opportunity['net_profit']:,.2f} | executable={executable}"
    )


def get_optimal_route() -> dict:
    return optimal_route


def dump_optimal_route_json() -> str:
    return json.dumps(optimal_route, indent=2, default=str)


# ---------------------------------------------------------------------------
# FlashLoanExecutor
# ---------------------------------------------------------------------------

class FlashLoanExecutor:
    """
    Builds, validates, and broadcasts initiateArbitrage() — speed-optimised.

    Speed path:
      1. Private RPC (PRIVATE_RPC_URL) for Polygon — bypasses rate-limited public nodes.
      2. Aggressive EIP-1559 tip (×1.25) — pushes tx to top of validator queue.
      3. Local nonce — skips on-chain nonce query for back-to-back trades.
      4. Pre-flight eth_call — cancels instantly if profit has evaporated.
    """

    def __init__(self,
                 contract_address: str,
                 private_key:      str | None = None):
        self.contract_address = (contract_address or "").strip()
        self.private_key      = (private_key      or "").strip()

    # ------------------------------------------------------------------
    # Pre-flight validation
    # ------------------------------------------------------------------

    def validate_ready(self) -> tuple[bool, str]:
        if not self.contract_address:
            return False, "EXECUTOR_CONTRACT_ADDRESS is not set."
        try:
            Web3.to_checksum_address(self.contract_address)
        except Exception:
            return False, (
                f"EXECUTOR_CONTRACT_ADDRESS '{self.contract_address}' "
                "is not a valid EVM address."
            )
        if not self.private_key:
            return False, "PRIVATE_KEY is not set."
        if len(self.private_key) not in (64, 66):
            return False, "PRIVATE_KEY looks malformed (expected 64 hex or 0x-prefixed 66)."
        return True, "OK"

    # ------------------------------------------------------------------
    # Aggressive EIP-1559 gas
    # ------------------------------------------------------------------

    # Network "Fast" priority tip + 30% premium (Hyper-Speed Mode).
    # Kept module-level so /gas can show the same value the bot is paying.
    PRIORITY_TIP_MULTIPLIER: float = 1.30

    @staticmethod
    def _build_gas_params(w3: Web3) -> dict:
        """
        EIP-1559 gas with a 30% priority-tip boost over the network average
        ("Fast +30% premium" — Hyper-Speed Mode).

        Formula:
          aggressive_tip  = network_tip × 1.30
          maxFeePerGas    = (2 × baseFee) + aggressive_tip

        The 30% tip premium beats other arbitrage bots that typically use the
        network "Fast" tip at par, landing us at the top of the validator queue.
        Falls back to legacy gasPrice × 1.20 if EIP-1559 is unsupported.
        """
        mult = FlashLoanExecutor.PRIORITY_TIP_MULTIPLIER
        try:
            block    = w3.eth.get_block("latest")
            base_fee = block.get("baseFeePerGas")

            if base_fee is not None:
                network_tip    = w3.eth.max_priority_fee
                aggressive_tip = int(network_tip * mult)
                max_fee        = (2 * base_fee) + aggressive_tip

                logger.debug(
                    f"[Gas] EIP-1559 ×{mult}: baseFee={base_fee} "
                    f"tip={network_tip}→{aggressive_tip} maxFee={max_fee}"
                )
                return {
                    "maxPriorityFeePerGas": aggressive_tip,
                    "maxFeePerGas":         max_fee,
                }
        except Exception as exc:
            logger.debug(f"[Gas] EIP-1559 fetch failed ({exc}), falling back to legacy")

        gas_price = w3.eth.gas_price
        buffered  = int(gas_price * 1.20)
        logger.debug(f"[Gas] Legacy gasPrice: {gas_price} → {buffered} (+20%)")
        return {"gasPrice": buffered}

    # ------------------------------------------------------------------
    # Pre-flight simulation (eth_call)
    # ------------------------------------------------------------------

    @staticmethod
    def _simulate(contract, arb_tuple: tuple, caller: str) -> None:
        """
        Dry-run initiateArbitrage() via eth_call.

        Raises ContractLogicError / Exception if the tx would revert,
        letting the caller cancel before spending gas. On revert we surface
        the most useful diagnostic we can extract:
          • Solidity require() string  → "...: profit below minimum"
          • Empty 0x revert            → likely a DEX-router inner revert
                                          (path/liquidity/slippage)
          • The exact arb_tuple        → so the user can replay on Tenderly
        """
        try:
            contract.functions.initiateArbitrage(arb_tuple).call({"from": caller})
        except ContractLogicError as cle:
            msg = str(cle)
            if msg.strip() in ("execution reverted", "execution reverted: 0x", ""):
                # No reason string → almost always an inner DEX call (no pool,
                # tokenIn==tokenOut, slippage limit, or insufficient liquidity).
                hint = (
                    "empty revert (no reason). Most likely cause: a DEX "
                    "router rejected the swap (no pool at this fee tier, "
                    "or the borrowed size exceeds available liquidity). "
                    f"arb_tuple={arb_tuple}"
                )
                raise ContractLogicError(hint) from cle
            # Solidity reason string came through — re-raise unchanged
            raise

    # ------------------------------------------------------------------
    # Transaction builder
    # ------------------------------------------------------------------

    def build_tx(self, execution_payload: dict,
                 w3: Web3,
                 contract,
                 arb_tuple: tuple,
                 caller: str,
                 chain: str) -> dict:
        """
        Encode initiateArbitrage() into a signed-ready tx dict.
        Uses local NonceManager — no on-chain roundtrip if nonce is known.
        """
        nonce      = nonce_manager.get_and_increment(chain, caller, w3)
        gas_params = self._build_gas_params(w3)

        tx = contract.functions.initiateArbitrage(arb_tuple).build_transaction({
            "from":  caller,
            "nonce": nonce,
            "gas":   900_000,
            **gas_params,
        })
        return tx

    # ------------------------------------------------------------------
    # Broadcaster
    # ------------------------------------------------------------------

    def fire(self, execution_payload: dict) -> dict:
        """
        Simulate → build → sign → broadcast initiateArbitrage().

        Returns {"tx_hash", "explorer_url", "chain"}.
        Raises on any failure (caller should handle cooldown logic).
        """
        # ── 1. Config pre-flight ─────────────────────────────────────────────
        ready, reason = self.validate_ready()
        if not ready:
            raise RuntimeError(f"Pre-flight FAILED: {reason}")

        if not execution_payload.get("executable"):
            raise ValueError(
                "Opportunity is not executable: "
                + execution_payload.get("reason", "unknown")
            )

        # ── 2. Connect via private (MEV-protected) RPC if configured ─────────
        chain               = execution_payload["chain"]
        rpc_url, is_private = _get_rpc_for_chain(chain)
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))

        if not w3.is_connected():
            # Private RPC unreachable → fall back to public to avoid a stuck trade
            if is_private:
                public_rpc = None
                for dex_cfg in config.DEXES.values():
                    if dex_cfg.get("chain") == chain:
                        public_rpc = dex_cfg["rpc_url"]
                        break
                logger.warning(
                    f"[FlashLoan] Private RPC unreachable ({rpc_url}); "
                    f"falling back to public RPC. ⚠ trade will be visible in mempool."
                )
                if public_rpc:
                    w3 = Web3(Web3.HTTPProvider(public_rpc, request_kwargs={"timeout": 12}))
                    is_private = False
            if not w3.is_connected():
                raise RuntimeError(f"RPC unreachable: {rpc_url}")

        logger.info(
            f"[FlashLoan] Broadcast channel: {chain} via "
            f"{'PRIVATE bundle (MEV-protected)' if is_private else 'public mempool'}"
        )

        contract_addr = Web3.to_checksum_address(self.contract_address)
        contract      = w3.eth.contract(address=contract_addr, abi=EXECUTOR_ABI)
        caller        = w3.eth.account.from_key(self.private_key).address

        params    = execution_payload["arb_params"]
        arb_tuple = (
            Web3.to_checksum_address(params["routerA"]),
            Web3.to_checksum_address(params["routerB"]),
            Web3.to_checksum_address(params["tokenIn"]),
            Web3.to_checksum_address(params["tokenOut"]),
            int(params["feeA"]),
            int(params["feeB"]),
            int(params["loanAmount"]),
            int(params["minProfit"]),
        )

        # ── 3. Pre-flight simulation — cancel if profit has evaporated ───────
        try:
            self._simulate(contract, arb_tuple, caller)
            logger.info("[FlashLoan] ✅ Simulation passed — profit still available.")
        except (ContractLogicError, Exception) as sim_exc:
            # Re-sync nonce (we never sent anything, so decrement)
            local = nonce_manager.peek(chain, caller)
            if local is not None:
                # simulation failed before we consumed a nonce — harmless
                pass
            raise ValueError(
                f"[FlashLoan] ❌ Simulation reverted (profit gone): {sim_exc}"
            )

        # ── 4. Build tx with local nonce + aggressive gas ─────────────────────
        tx     = self.build_tx(execution_payload, w3, contract, arb_tuple, caller, chain)
        nonce_used = tx["nonce"]

        # ── 5. Sign ──────────────────────────────────────────────────────────
        signed = w3.eth.account.sign_transaction(tx, self.private_key)

        # ── 6. Broadcast ─────────────────────────────────────────────────────
        try:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        except Exception as send_exc:
            err = str(send_exc).lower()
            if "nonce" in err or "replacement" in err or "already known" in err:
                logger.warning(
                    f"[Nonce] Collision on nonce={nonce_used} — re-syncing."
                )
                nonce_manager.reset(chain, caller, w3)
            raise

        explorer_url = EXPLORER.get(chain, "") + tx_hash

        logger.info(f"[FlashLoan] Broadcast! nonce={nonce_used} hash={tx_hash}")
        logger.info(f"[FlashLoan] Track: {explorer_url}")

        return {
            "tx_hash":      tx_hash,
            "explorer_url": explorer_url,
            "chain":        chain,
        }
