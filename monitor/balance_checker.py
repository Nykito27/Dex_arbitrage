"""
balance_checker.py
------------------
Connects to multiple EVM-compatible chains via Web3.py and returns
the native-token balance for a given wallet address on each chain.
"""

from web3 import Web3


CHAINS = [
    {
        "name": "Polygon",
        "rpc_url": "https://polygon-bor-rpc.publicnode.com",
        "native_token": "MATIC",
        "low_balance_threshold": 10.0,
    },
    {
        "name": "Arbitrum",
        "rpc_url": "https://arb1.arbitrum.io/rpc",
        "native_token": "ETH",
        "low_balance_threshold": 0.01,
    },
    {
        "name": "Base",
        "rpc_url": "https://mainnet.base.org",
        "native_token": "ETH",
        "low_balance_threshold": 0.01,
    },
]


def _connect(rpc_url: str) -> Web3:
    """Return a connected Web3 instance or raise on failure."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {rpc_url}")
    return w3


def check_chain(wallet_address: str, chain: dict) -> dict:
    """
    Check the native-token balance for *wallet_address* on a single chain.

    Returns a dict:
      {
          "name":             str,
          "native_token":     str,
          "balance":          float,   # in native token units
          "low_balance_threshold": float,
          "is_low":           bool,
          "error":            str | None,
      }
    """
    result = {
        "name": chain["name"],
        "native_token": chain["native_token"],
        "balance": 0.0,
        "low_balance_threshold": chain["low_balance_threshold"],
        "is_low": False,
        "error": None,
    }
    try:
        w3 = _connect(chain["rpc_url"])
        checksum_addr = Web3.to_checksum_address(wallet_address)
        raw_balance = w3.eth.get_balance(checksum_addr)
        balance = float(Web3.from_wei(raw_balance, "ether"))
        result["balance"] = balance
        result["is_low"] = balance < chain["low_balance_threshold"]
    except Exception as exc:
        result["error"] = str(exc)
    return result


def check_all_chains(wallet_address: str) -> list[dict]:
    """
    Check the wallet balance across all configured chains.

    Returns a list of result dicts (one per chain).
    """
    return [check_chain(wallet_address, chain) for chain in CHAINS]
