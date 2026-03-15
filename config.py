"""
config.py
---------
Central configuration for the DeFi Arbitrage Hunter.

Edit WATCHLIST, TOKENS, DEXES, and the profit constants below
to customise which assets and markets are monitored.
"""

# ---------------------------------------------------------------------------
# Token watchlist — symbols to monitor
# ---------------------------------------------------------------------------
WATCHLIST = ["WETH", "WBTC", "LINK", "GHO", "USDe", "MATIC"]

# ---------------------------------------------------------------------------
# Token addresses and decimals per chain
# (USDC is the universal quote currency)
# ---------------------------------------------------------------------------
TOKENS = {
    "Polygon": {
        "WETH":  {"address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", "decimals": 18},
        "WBTC":  {"address": "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6", "decimals": 8},
        "LINK":  {"address": "0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39", "decimals": 18},
        # MATIC is quoted via WMATIC in pools
        "MATIC": {"address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "decimals": 18},
        "USDC":  {"address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "decimals": 6},
        # GHO and USDe are not deployed on Polygon mainnet
    },
    "Arbitrum": {
        "WETH":  {"address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18},
        "WBTC":  {"address": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", "decimals": 8},
        "LINK":  {"address": "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4", "decimals": 18},
        "GHO":   {"address": "0x7dfF72693f6A4149b17e7C6314655f6A9F7c8B33", "decimals": 18},
        "USDe":  {"address": "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34", "decimals": 18},
        "USDC":  {"address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
    },
    "Base": {
        "WETH":  {"address": "0x4200000000000000000000000000000000000006", "decimals": 18},
        "WBTC":  {"address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "decimals": 8},
        "LINK":  {"address": "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196", "decimals": 18},
        "USDC":  {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
        # GHO and USDe not yet deployed on Base mainnet
    },
}

# ---------------------------------------------------------------------------
# DEX configurations (one per chain as requested)
# ---------------------------------------------------------------------------
DEXES = {
    "Uniswap V3 (Polygon)": {
        "chain": "Polygon",
        "factories": ["0x1F98431c8aD98523631AE4a59f267346ea31F984"],
        "dex_name": "Uniswap V3",
        "rpc_url": "https://polygon-bor-rpc.publicnode.com",
        "native_token": "MATIC",
        # Uniswap V3 fee tiers in hundredths of a bip (500=0.05%, 3000=0.3%, 10000=1%)
        "fee_tiers": [500, 3000, 10000],
        "gas_units": 300_000,
        # Uniswap V3 SwapRouter — handles exactInputSingle on Polygon
        "router": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        # Aave V3 PoolAddressesProvider on Polygon
        "aave_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "swap_url": (
            "https://app.uniswap.org/#/swap"
            "?chain=polygon&inputCurrency={token_in}&outputCurrency={token_out}"
        ),
    },
    "SushiSwap (Arbitrum)": {
        "chain": "Arbitrum",
        # factories tried in order; first pool found wins
        # [0] SushiSwap V3, [1] Uniswap V3 on Arbitrum (fallback)
        "factories": [
            "0x1af415a1EbA07a4986a52B6f2e7dE7003D82231b",
            "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        ],
        "dex_name": "SushiSwap",
        "rpc_url": "https://arb1.arbitrum.io/rpc",
        "native_token": "ETH",
        # Prioritise 0.05% and 0.3% tiers (most liquid for volatile assets).
        # fee=100 (0.01%) is for stablecoins; try it last to avoid illiquid pools.
        "fee_tiers": [500, 3000, 100, 10000],
        "gas_units": 300_000,
        # Uniswap V3 SwapRouter — also routes Uniswap-fallback pools on Arbitrum
        "router": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        # Aave V3 PoolAddressesProvider on Arbitrum
        "aave_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "swap_url": (
            "https://www.sushi.com/swap"
            "?chainId=42161&token0={token_in}&token1={token_out}"
        ),
    },
    "PancakeSwap (Base)": {
        "chain": "Base",
        # [0] PancakeSwap V3, [1] Uniswap V3 on Base (fallback)
        "factories": [
            "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
            "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        ],
        "dex_name": "PancakeSwap",
        "rpc_url": "https://mainnet.base.org",
        "native_token": "ETH",
        # Try liquid tiers first (500, 2500, 3000), then stablecoin/dust tiers last.
        "fee_tiers": [500, 2500, 3000, 10000, 100],
        "gas_units": 300_000,
        # Uniswap V3 SwapRouter — also routes Uniswap-fallback pools on Base
        "router": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        # Aave V3 PoolAddressesProvider on Base
        "aave_addresses_provider": "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D",
        "swap_url": (
            "https://pancakeswap.finance/swap"
            "?chain=base&inputCurrency={token_in}&outputCurrency={token_out}"
        ),
    },
}

# ---------------------------------------------------------------------------
# Profit filter constants
# ---------------------------------------------------------------------------

# Minimum net profit (USD) required to send a Telegram alert
MIN_NET_PROFIT_USD = 10.0

# Flash loan fee in basis points (0.05% = 5 bps, standard Aave v3 fee)
FLASH_LOAN_FEE_BPS = 5

# Notional trade size in USD used for profit simulation
TRADE_SIZE_USD = 10_000
