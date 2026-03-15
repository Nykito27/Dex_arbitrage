"""
flash_loan.py
-------------
Flash Loan execution module.

Responsibilities
----------------
1. Store the latest optimal arbitrage route each scan cycle.
2. Generate ABI-encoded calldata for FlashLoanExecutor.initiateArbitrage()
   so the contract can be called without any manual translation.
3. Expose a ready-to-broadcast transaction dict for every same-chain opportunity.

Cross-chain note
----------------
Flash loans require all steps (borrow → swap A → swap B → repay) to fit in a
SINGLE Ethereum transaction on ONE chain.  Cross-chain gaps detected by the
hunter are surfaced as alerts but the execution payload is only generated when
both legs live on the same chain (same-chain arb, two different DEXes).
"""

from __future__ import annotations
import json
import time
import logging
import math
from typing import Optional
from web3 import Web3

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FlashLoanExecutor ABI  (only the functions we call)
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


def _build_execution_payload(opportunity: dict) -> Optional[dict]:
    """
    Return the execution payload for same-chain opportunities, or None
    if the two legs are on different chains (cannot be done atomically).

    The payload contains everything needed to call
    FlashLoanExecutor.initiateArbitrage() on the target chain.
    """
    buy_dex_key  = opportunity.get("buy_dex")   # e.g. "SushiSwap (Arbitrum)"
    sell_dex_key = opportunity.get("sell_dex")  # e.g. "PancakeSwap (Base)"

    if not buy_dex_key or not sell_dex_key:
        return None

    buy_cfg  = config.DEXES.get(buy_dex_key,  {})
    sell_cfg = config.DEXES.get(sell_dex_key, {})

    buy_chain  = buy_cfg.get("chain")
    sell_chain = sell_cfg.get("chain")

    # Flash loans only work single-chain
    if buy_chain != sell_chain:
        return {
            "executable": False,
            "reason": (
                f"Cross-chain: buy on {buy_chain}, sell on {sell_chain}. "
                "Cannot execute atomically. Consider bridging or waiting for "
                "a same-chain opportunity."
            ),
        }

    chain         = buy_chain
    router_a      = buy_cfg.get("router")
    router_b      = sell_cfg.get("router")
    aave_provider = buy_cfg.get("aave_addresses_provider")

    token_in_address  = opportunity.get("buy_token_address")
    token_out_address = opportunity.get("sell_token_address")
    fee_a             = opportunity.get("buy_fee", 500)
    fee_b             = opportunity.get("sell_fee", 500)

    decimals_in  = _get_decimals(chain, opportunity["symbol"])
    loan_amount  = int(opportunity["token_amount"] * (10 ** decimals_in))

    # minProfit: 80% of estimated net profit as a floor (leaves room for slippage)
    net_profit_usd  = opportunity["net_profit"]
    token_price_usd = opportunity["buy_price"]
    min_profit_token = (net_profit_usd * 0.80) / token_price_usd
    min_profit_wei   = int(min_profit_token * (10 ** decimals_in))

    return {
        "executable":         True,
        "chain":              chain,
        "aave_addresses_provider": aave_provider,
        "arb_params": {
            "routerA":    router_a,
            "routerB":    router_b,
            "tokenIn":    token_in_address,
            "tokenOut":   token_out_address,
            "feeA":       fee_a,
            "feeB":       fee_b,
            "loanAmount": loan_amount,
            "minProfit":  min_profit_wei,
        },
        "human_readable": {
            "chain":               chain,
            "token_symbol":        opportunity["symbol"],
            "loan_amount_tokens":  opportunity["token_amount"],
            "loan_amount_usd":     opportunity["trade_size_usd"],
            "buy_dex":             opportunity["buy_dex"],
            "sell_dex":            opportunity["sell_dex"],
            "buy_price_usd":       opportunity["buy_price"],
            "sell_price_usd":      opportunity["sell_price"],
            "estimated_profit_usd": opportunity["net_profit"],
            "min_profit_floor_usd": net_profit_usd * 0.80,
        },
        "abi": EXECUTOR_ABI,
        "deploy_note": (
            f"Deploy FlashLoanExecutor.sol with constructor arg = {aave_provider} "
            f"on {chain}, then call initiateArbitrage(arb_params)."
        ),
    }


def _get_decimals(chain: str, symbol: str) -> int:
    """Look up token decimals from config; default to 18."""
    chain_tokens = config.TOKENS.get(chain, {})
    token        = chain_tokens.get(symbol, {})
    return token.get("decimals", 18)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def store_optimal_route(opportunity: dict | None) -> None:
    """
    Persist the best arbitrage opportunity found in the current cycle.

    Parameters
    ----------
    opportunity : dict from price_hunter.find_arbitrage_opportunities(),
                  or None if no profitable route was found.
    """
    global optimal_route

    if opportunity is None:
        optimal_route = {"status": "no_opportunity", "updated_at": time.time()}
        return

    payload = _build_execution_payload(opportunity)

    optimal_route = {
        "status":           "ready",
        "updated_at":       time.time(),
        "symbol":           opportunity["symbol"],
        "trade_size_usd":   opportunity["trade_size_usd"],
        "token_amount":     opportunity["token_amount"],
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

    executable = payload.get("executable") if payload else False
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
# FlashLoanExecutor — web3 transaction builder
# ---------------------------------------------------------------------------

class FlashLoanExecutor:
    """
    Builds and optionally broadcasts the initiateArbitrage() transaction.

    Usage example
    -------------
    executor = FlashLoanExecutor(
        rpc_url="https://arb1.arbitrum.io/rpc",
        contract_address="0xYourDeployedContract",
        private_key=os.getenv("PRIVATE_KEY"),
    )
    route = get_optimal_route()
    if route.get("execution", {}).get("executable"):
        tx_hash = executor.fire(route["execution"])
        print("tx:", tx_hash)
    """

    def __init__(self,
                 rpc_url:          str,
                 contract_address: str,
                 private_key:      str | None = None):
        """
        Parameters
        ----------
        rpc_url:           RPC endpoint (must match the chain in the route).
        contract_address:  Deployed FlashLoanExecutor.sol address.
        private_key:       Wallet private key — store as a Replit Secret only.
        """
        self.w3               = Web3(Web3.HTTPProvider(rpc_url))
        self.contract_address = Web3.to_checksum_address(contract_address)
        self.private_key      = private_key

        self.contract = self.w3.eth.contract(
            address=self.contract_address,
            abi=EXECUTOR_ABI,
        )

    # ------------------------------------------------------------------

    def build_tx(self, execution_payload: dict) -> dict:
        """
        Encode the initiateArbitrage() call into an unsigned transaction dict.

        Parameters
        ----------
        execution_payload : the 'execution' key from optimal_route.

        Returns
        -------
        Unsigned tx dict ready for sign_transaction / eth_sendRawTransaction.
        """
        if not execution_payload.get("executable"):
            raise ValueError(
                "Cannot build tx: " + execution_payload.get("reason", "not executable")
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

        caller = self.w3.eth.account.from_key(self.private_key).address
        nonce  = self.w3.eth.get_transaction_count(caller)

        tx = self.contract.functions.initiateArbitrage(arb_tuple).build_transaction({
            "from":  caller,
            "nonce": nonce,
            "gas":   800_000,
            "gasPrice": self.w3.eth.gas_price,
        })
        return tx

    def fire(self, execution_payload: dict) -> str:
        """
        Sign and broadcast initiateArbitrage().

        Returns
        -------
        Transaction hash (hex string).
        """
        if not self.private_key:
            raise RuntimeError(
                "No private key set — add PRIVATE_KEY as a Replit Secret."
            )

        tx     = self.build_tx(execution_payload)
        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()
