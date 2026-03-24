"""
flash_loan.py
-------------
Flash Loan execution module.

Responsibilities
----------------
1. Store the latest optimal arbitrage route each scan cycle.
2. Build the ABI-encoded ArbParams struct for FlashLoanExecutor.initiateArbitrage().
3. Fire the transaction on-chain via web3.py with dynamic EIP-1559 gas pricing.
4. Gate every execution attempt behind pre-flight validation and (optionally)
   a manual Y/N confirmation prompt.

Secrets required (add in Replit Secrets before firing live trades):
  PRIVATE_KEY                — wallet private key (never hard-code)
  EXECUTOR_CONTRACT_ADDRESS  — deployed FlashLoanExecutor.sol address

Env vars (set automatically, or override in Replit Secrets):
  MANUAL_CONFIRM  — "true" (default) = prompt Y/N before each trade
                    "false"           = fire automatically when opportunity found

Cross-chain note
----------------
Flash loans require all steps (borrow → swap A → swap B → repay) to fit in a
SINGLE Ethereum transaction on ONE chain.  Cross-chain gaps detected by the
hunter are surfaced as alerts but execution is only attempted for same-chain
opportunities.
"""

from __future__ import annotations
import json
import os
import time
import logging
from typing import Optional
from web3 import Web3

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Block-explorer base URLs (for printing tx links after broadcast)
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
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "initiateArbitrage",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "token",  "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "rescueTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# ---------------------------------------------------------------------------
# Shared state — latest optimal route (updated every scan cycle)
# ---------------------------------------------------------------------------
optimal_route: dict = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_decimals(chain: str, symbol: str) -> int:
    """Look up token decimals from config; default to 18."""
    chain_tokens = config.TOKENS.get(chain, {})
    token        = chain_tokens.get(symbol, {})
    return token.get("decimals", 18)


def _get_rpc_for_chain(chain: str) -> str:
    """Return the RPC URL for the given chain name by scanning DEXES config."""
    for dex_cfg in config.DEXES.values():
        if dex_cfg.get("chain") == chain:
            return dex_cfg["rpc_url"]
    raise ValueError(f"No RPC URL found for chain '{chain}' in config.DEXES")


def _build_execution_payload(opportunity: dict) -> Optional[dict]:
    """
    Build the execution payload for a single arbitrage opportunity.

    Returns a dict with:
      executable   — True if both legs are on the same chain
      arb_params   — the ArbParams struct values ready to pass to the contract
      chain        — chain name (e.g. "Arbitrum")
      human_readable — plain-English summary for the confirmation prompt

    Returns None if the opportunity dict is missing required fields.
    """
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
                f"Cross-chain gap: buy on {buy_chain}, sell on {sell_chain}. "
                "Flash loans are single-chain only. Alert sent for awareness."
            ),
        }

    chain         = buy_chain
    router_a      = buy_cfg.get("router")
    router_b      = sell_cfg.get("router")
    aave_provider = buy_cfg.get("aave_addresses_provider")

    token_in_addr  = opportunity.get("buy_token_address")
    token_out_addr = opportunity.get("sell_token_address")
    fee_a          = opportunity.get("buy_fee",  500)
    fee_b          = opportunity.get("sell_fee", 500)

    decimals_in = _get_decimals(chain, opportunity["symbol"])
    loan_amount = int(opportunity["token_amount"] * (10 ** decimals_in))

    net_profit_usd   = opportunity["net_profit"]
    token_price_usd  = opportunity["buy_price"]
    min_profit_token = (net_profit_usd * 0.80) / token_price_usd
    min_profit_wei   = int(min_profit_token * (10 ** decimals_in))

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
    """
    Called by the main loop every scan cycle.
    Persists the best opportunity and its execution payload.
    """
    global optimal_route

    if opportunity is None:
        optimal_route = {"status": "no_opportunity", "updated_at": time.time()}
        return

    payload = _build_execution_payload(opportunity)
    executable = payload.get("executable") if payload else False

    optimal_route = {
        "status":     "ready",
        "updated_at": time.time(),
        "symbol":     opportunity["symbol"],
        "trade_size_usd":            opportunity["trade_size_usd"],
        "token_amount":              opportunity["token_amount"],
        "estimated_net_profit_usd":  opportunity["net_profit"],
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
        f"[FlashLoan] Optimal route stored: "
        f"{opportunity['symbol']} | "
        f"buy on {opportunity['buy_chain']} @ ${opportunity['buy_price']:,.4f} | "
        f"sell on {opportunity['sell_chain']} @ ${opportunity['sell_price']:,.4f} | "
        f"net ≈ ${opportunity['net_profit']:,.2f} | "
        f"executable={executable}"
    )


def get_optimal_route() -> dict:
    """Return the latest stored optimal route."""
    return optimal_route


def dump_optimal_route_json() -> str:
    """Serialize the current optimal route to a JSON string."""
    return json.dumps(optimal_route, indent=2, default=str)


# ---------------------------------------------------------------------------
# FlashLoanExecutor — on-chain transaction builder and broadcaster
# ---------------------------------------------------------------------------

class FlashLoanExecutor:
    """
    Builds, validates, and fires the initiateArbitrage() transaction.

    Instantiate once at startup; call fire() when ready to execute.
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
        """
        Check that the executor is fully configured before touching real money.

        Returns (True, "OK") or (False, "<reason>").
        """
        if not self.contract_address:
            return False, (
                "EXECUTOR_CONTRACT_ADDRESS is not set. "
                "Add it in Replit Secrets (Secrets tab)."
            )
        try:
            Web3.to_checksum_address(self.contract_address)
        except Exception:
            return False, (
                f"EXECUTOR_CONTRACT_ADDRESS '{self.contract_address}' "
                "is not a valid EVM address."
            )
        if not self.private_key:
            return False, (
                "PRIVATE_KEY is not set. "
                "Add your wallet private key in Replit Secrets."
            )
        if len(self.private_key) not in (64, 66):
            return False, (
                "PRIVATE_KEY looks malformed (expected 64 hex chars or 0x-prefixed 66)."
            )
        return True, "OK"

    # ------------------------------------------------------------------
    # Dynamic gas pricing
    # ------------------------------------------------------------------

    @staticmethod
    def _build_gas_params(w3: Web3) -> dict:
        """
        Return EIP-1559 gas params when the chain supports them,
        otherwise fall back to a legacy gasPrice with a 20% buffer.

        EIP-1559 formula:
          maxPriorityFeePerGas = suggested tip from node
          maxFeePerGas         = (2 × baseFee) + tip
          — the 2× multiplier means the tx still lands even if the next
            two blocks double the base fee (very safe margin).
        """
        try:
            block    = w3.eth.get_block("latest")
            base_fee = block.get("baseFeePerGas")

            if base_fee is not None:
                tip = w3.eth.max_priority_fee          # node's suggested tip
                max_fee = (2 * base_fee) + tip
                logger.debug(
                    f"[Gas] EIP-1559: baseFee={base_fee} tip={tip} maxFee={max_fee}"
                )
                return {
                    "maxPriorityFeePerGas": tip,
                    "maxFeePerGas":         max_fee,
                }
        except Exception as e:
            logger.debug(f"[Gas] EIP-1559 fetch failed ({e}), falling back to legacy")

        # Legacy fallback (+20% buffer so the tx is competitive)
        gas_price = w3.eth.gas_price
        buffered  = int(gas_price * 1.20)
        logger.debug(f"[Gas] Legacy: gasPrice={gas_price} (+20% → {buffered})")
        return {"gasPrice": buffered}

    # ------------------------------------------------------------------
    # Transaction builder
    # ------------------------------------------------------------------

    def build_tx(self, execution_payload: dict) -> dict:
        """
        Encode initiateArbitrage() into an unsigned transaction dict.

        Parameters
        ----------
        execution_payload : the 'execution' key from optimal_route

        Returns
        -------
        Unsigned tx dict — ready for sign_transaction / eth_sendRawTransaction.
        """
        if not execution_payload.get("executable"):
            raise ValueError(
                "Cannot build tx: "
                + execution_payload.get("reason", "opportunity is not executable")
            )

        chain   = execution_payload["chain"]
        rpc_url = _get_rpc_for_chain(chain)
        w3      = Web3(Web3.HTTPProvider(rpc_url))

        contract = w3.eth.contract(
            address=Web3.to_checksum_address(self.contract_address),
            abi=EXECUTOR_ABI,
        )

        params = execution_payload["arb_params"]
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

        caller = w3.eth.account.from_key(self.private_key).address
        nonce  = w3.eth.get_transaction_count(caller, "pending")

        gas_params = self._build_gas_params(w3)

        tx = contract.functions.initiateArbitrage(arb_tuple).build_transaction({
            "from":  caller,
            "nonce": nonce,
            "gas":   900_000,          # generous ceiling — unused gas is refunded
            **gas_params,
        })
        return tx, w3

    # ------------------------------------------------------------------
    # Broadcaster
    # ------------------------------------------------------------------

    def fire(self, execution_payload: dict) -> dict:
        """
        Validate, sign, and broadcast initiateArbitrage().

        Returns
        -------
        {
          "tx_hash":      "0x...",
          "explorer_url": "https://arbiscan.io/tx/0x...",
          "chain":        "Arbitrum",
        }
        Raises RuntimeError / ValueError on any pre-flight failure.
        """
        # ── 1. Pre-flight checks ─────────────────────────────────────────────
        ready, reason = self.validate_ready()
        if not ready:
            raise RuntimeError(f"[FlashLoan] Pre-flight FAILED: {reason}")

        if not execution_payload.get("executable"):
            raise ValueError(
                "[FlashLoan] Opportunity is not executable: "
                + execution_payload.get("reason", "unknown reason")
            )

        # ── 2. Build tx ──────────────────────────────────────────────────────
        chain   = execution_payload["chain"]
        tx, w3  = self.build_tx(execution_payload)

        # ── 3. Sign ──────────────────────────────────────────────────────────
        signed = w3.eth.account.sign_transaction(tx, self.private_key)

        # ── 4. Broadcast ─────────────────────────────────────────────────────
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()

        explorer_base = EXPLORER.get(chain, "")
        explorer_url  = f"{explorer_base}{tx_hash}" if explorer_base else tx_hash

        logger.info(f"[FlashLoan] Transaction broadcast! hash={tx_hash}")
        logger.info(f"[FlashLoan] Track it: {explorer_url}")

        return {
            "tx_hash":      tx_hash,
            "explorer_url": explorer_url,
            "chain":        chain,
        }
