"""
flash_loan.py
-------------
Flash Loan execution module.

Stores the most recent optimal arbitrage route so it can be picked up
and triggered by an AI agent or automated executor in the next step.

Usage now  : The bot populates `optimal_route` automatically each cycle.
Usage later: Subclass FlashLoanExecutor and implement execute() / on_received().
"""

from __future__ import annotations
import json
import time
import logging
from web3 import Web3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state — latest optimal route
# ---------------------------------------------------------------------------

# This dict is updated by the main loop every scan cycle.
# An external executor (AI agent, cron job, etc.) can read it at any time.
optimal_route: dict = {}


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
    }

    logger.info(
        f"[FlashLoan] Optimal route stored: "
        f"{opportunity['symbol']} | "
        f"buy on {opportunity['buy_chain']} @ ${opportunity['buy_price']:,.4f} | "
        f"sell on {opportunity['sell_chain']} @ ${opportunity['sell_price']:,.4f} | "
        f"net ≈ ${opportunity['net_profit']:,.2f}"
    )


def get_optimal_route() -> dict:
    """Return the latest stored optimal route (read by executor agents)."""
    return optimal_route


def dump_optimal_route_json() -> str:
    """Serialize the current optimal route to a JSON string."""
    return json.dumps(optimal_route, indent=2)


# ---------------------------------------------------------------------------
# FlashLoanExecutor — ready for AI-triggered execution
# ---------------------------------------------------------------------------

class FlashLoanExecutor:
    """
    Flash loan execution engine.

    Currently a structured stub — all data routing is in place.
    Implement execute() and on_flash_loan_received() with your
    smart-contract interaction logic when ready.

    Aave v3 integration points (same interface on Polygon, Arbitrum, Base):
      Pool address (Polygon):  0x794a61358D6845594F94dc1DB02A252b5b4814aD
      Pool address (Arbitrum): 0x794a61358D6845594F94dc1DB02A252b5b4814aD
      Pool address (Base):     0xA238Dd80C259a72e81d7e4664a9801593F98d1c5
    """

    AAVE_V3_POOL_ABI = [
        {
            "inputs": [
                {"internalType": "address",   "name": "receiverAddress", "type": "address"},
                {"internalType": "address[]", "name": "assets",          "type": "address[]"},
                {"internalType": "uint256[]", "name": "amounts",         "type": "uint256[]"},
                {"internalType": "uint256[]", "name": "interestRateModes","type": "uint256[]"},
                {"internalType": "address",   "name": "onBehalfOf",      "type": "address"},
                {"internalType": "bytes",     "name": "params",          "type": "bytes"},
                {"internalType": "uint16",    "name": "referralCode",    "type": "uint16"},
            ],
            "name": "flashLoan",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        }
    ]

    def __init__(self, rpc_url: str, private_key: str | None = None):
        """
        Parameters
        ----------
        rpc_url:     RPC endpoint of the target chain.
        private_key: Wallet private key — store as a Replit Secret, never hard-code.
        """
        self.w3          = Web3(Web3.HTTPProvider(rpc_url))
        self.private_key = private_key

    def execute(self,
                pool_address: str,
                token_address: str,
                amount_wei: int) -> str:
        """
        Initiate an Aave v3 flash loan.

        Parameters
        ----------
        pool_address:  Aave v3 Pool contract address on the target chain.
        token_address: ERC-20 token to borrow.
        amount_wei:    Amount to borrow in wei.

        Returns
        -------
        Transaction hash as a hex string.
        """
        raise NotImplementedError(
            "FlashLoanExecutor.execute() — implement the Aave v3 flashLoan() "
            "call and sign the transaction with self.private_key."
        )

    def on_flash_loan_received(self,
                               token: str,
                               amount: int,
                               fee: int,
                               data: bytes) -> None:
        """
        Called by the Aave pool after funds land in your receiver contract.

        Steps to implement:
          1. Execute the arbitrage swap on the buy DEX.
          2. Execute the reverse swap on the sell DEX.
          3. Approve and repay (amount + fee) to the pool before returning.
        """
        raise NotImplementedError(
            "Implement swap logic here, then repay (amount + fee) to the pool."
        )
