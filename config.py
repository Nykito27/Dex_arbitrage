"""
config.py
---------
Central configuration for the DeFi Arbitrage Hunter.

DEX layout — 2 independent DEXes per chain so same-chain arb is detectable:
  Polygon  : Uniswap V3  ↔  SushiSwap V3
  Arbitrum : Uniswap V3  ↔  SushiSwap V3
  Base     : Uniswap V3  ↔  PancakeSwap V3
"""

# ---------------------------------------------------------------------------
# Token watchlist — 50 high-liquidity ERC-20 symbols
# Symbols absent from a chain's TOKENS dict are silently skipped on that chain.
# ---------------------------------------------------------------------------
WATCHLIST = [
    # Blue-chip / wrapped assets
    "WETH", "WBTC", "MATIC",
    # DeFi blue-chips
    "AAVE", "UNI", "CRV", "SUSHI", "COMP", "BAL", "MKR", "YFI", "SNX",
    "FXS", "FRAX", "GNS", "GHO", "USDe",
    # Liquid staking
    "wstETH", "stMATIC", "MaticX", "rETH", "cbETH",
    # Arbitrum-native
    "ARB", "GMX", "MAGIC", "PENDLE", "RDNT", "GRAIL", "DPX", "JOE", "LDO",
    # Polygon-native / gaming / metaverse
    "LINK", "QUICK", "GHST", "SAND", "MANA", "AXS", "IMX", "DPI",
    # Stablecoins (arb-able against each other)
    "DAI", "USDT", "LUSD",
    # Additional high-volume
    "SHIB",
]

# ---------------------------------------------------------------------------
# Token addresses and decimals per chain
# USDC (the quote token) is listed separately at the bottom of each chain.
# Only include tokens with a realistic USDC V3 pool on that chain.
# ---------------------------------------------------------------------------
TOKENS = {
    # ── Polygon ───────────────────────────────────────────────────────────────
    "Polygon": {
        "WETH":    {"address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", "decimals": 18},
        "WBTC":    {"address": "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6", "decimals": 8},
        "MATIC":   {"address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "decimals": 18},
        "AAVE":    {"address": "0xD6DF932A45C0f255f85145f286eA0b292B21C90B", "decimals": 18},
        "UNI":     {"address": "0xb33EaAd8d922B1083446DC23f610c2567fB5180f", "decimals": 18},
        "CRV":     {"address": "0x172370d5Cd63279eFa6d502DAB29171933a610AF", "decimals": 18},
        "SUSHI":   {"address": "0x0b3F868E0BE5597D5DB7fEB59E1CADBb0fdDa50a", "decimals": 18},
        "COMP":    {"address": "0x8505b9d2254A7Ae468c0E9dd10Ccea3A837aef5c", "decimals": 18},
        "BAL":     {"address": "0x9a71012B13CA4d3D0Cdc72A177DF3ef03b0E76A3", "decimals": 18},
        "MKR":     {"address": "0x6f7C932e7684666C9fd1d44527765433e01fF61d", "decimals": 18},
        "YFI":     {"address": "0xDA537104D6A5edd53c6fBba9A898708E465260b6", "decimals": 18},
        "SNX":     {"address": "0x50B728D8D964fd00C2d0AAD81718b71311feF68a", "decimals": 18},
        "FXS":     {"address": "0x1a3acf6D19267E2d3e7f898f42803e90129a1531", "decimals": 18},
        "FRAX":    {"address": "0x45c32fA6DF82ead1e2EF74d17b76547EDdFaFF89", "decimals": 18},
        "GNS":     {"address": "0xE5417Af564e4bFDA1c483642db72007871397896", "decimals": 18},
        "wstETH":  {"address": "0x03b54A6e9a984069379fae1a4fC4dBAE93B3bCCD", "decimals": 18},
        "stMATIC": {"address": "0x3A58a54C066FdC0f2D55FC9C89F0415C92eBf3C4", "decimals": 18},
        "MaticX":  {"address": "0xfa68FB4628DFF1028CFEc22b4162FCcd0d45efb6", "decimals": 18},
        "LINK":    {"address": "0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39", "decimals": 18},
        "QUICK":   {"address": "0xB5C064F955D8e7F38fE0460C556a72987494eE17", "decimals": 18},
        "GHST":    {"address": "0x385Eeac5cB85A38A9a07A70c73e0a3271CfB54A7", "decimals": 18},
        "SAND":    {"address": "0xBbba073C31bF03b8ACf7c28EF0738DeCF3695683", "decimals": 18},
        "MANA":    {"address": "0xA1c57f48F0Deb89f569dFbE6E2B7f46D33606fD4", "decimals": 18},
        "AXS":     {"address": "0x61BDD9C7d4dF4Bf47A4508c0c8245505F2Af5b7b", "decimals": 18},
        "IMX":     {"address": "0xa974c709cFb4566686553a20DaF516291d38C99e", "decimals": 18},
        "DPI":     {"address": "0x85955046DF4668e1DD369D2DE9f3AEB98DD2A369", "decimals": 18},
        "DAI":     {"address": "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063", "decimals": 18},
        "USDT":    {"address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", "decimals": 6},
        "SHIB":    {"address": "0x6f8a06447Ff6FcF75d803135a7de15CE88C1d4ec", "decimals": 18},
        # Quote currency — not in WATCHLIST, used internally
        "USDC":    {"address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "decimals": 6},
    },

    # ── Arbitrum ──────────────────────────────────────────────────────────────
    "Arbitrum": {
        "WETH":    {"address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18},
        "WBTC":    {"address": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", "decimals": 8},
        "AAVE":    {"address": "0xba5DdD1f9d7F570dc94a51479a000E3BCE967196", "decimals": 18},
        "UNI":     {"address": "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0", "decimals": 18},
        "CRV":     {"address": "0x11cDb42B0EB46D95f990BeDD4695A6e3fA034978", "decimals": 18},
        "SUSHI":   {"address": "0xd4d42F0b6DEF4CE0383636770eF773390d85c61A", "decimals": 18},
        "COMP":    {"address": "0x354A6dA3fcde098F8389cad84b0182725c6C91dE", "decimals": 18},
        "BAL":     {"address": "0x040d1EdC9569d4Bab2D15287Dc5A4F10F56a56B8", "decimals": 18},
        "MKR":     {"address": "0x2e9a898A87dE11E9a5f804FC2Fb050F0FD2b1E50", "decimals": 18},
        "SNX":     {"address": "0xcBA56Cd8216FCBBF3fA6DF6b9CDA20b8905C88B4", "decimals": 18},
        "FXS":     {"address": "0x9d2F299715D94d8A7E6F5eaa8E654E8c74a988A7", "decimals": 18},
        "FRAX":    {"address": "0x17FC002b466eEc40DaE837Fc4bE5c67993ddBd6F", "decimals": 18},
        "GNS":     {"address": "0x18c11FD286C5EC11c3b683Caa813B77f5163A122", "decimals": 18},
        "GHO":     {"address": "0x7dfF72693f6A4149b17e7C6314655f6A9F7c8B33", "decimals": 18},
        "USDe":    {"address": "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34", "decimals": 18},
        "wstETH":  {"address": "0x5979D7b546E38E414F7E9822514be443A4800529", "decimals": 18},
        "rETH":    {"address": "0xEC70Dcb4A1EFa46b8F2D97C310C9c4790ba5ffA8", "decimals": 18},
        "cbETH":   {"address": "0x1DEBd73E752bEaF79865Fd6446b0c970EaE7732f", "decimals": 18},
        "ARB":     {"address": "0x912CE59144191C1204E64559FE8253a0e49E6548", "decimals": 18},
        "GMX":     {"address": "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a", "decimals": 18},
        "MAGIC":   {"address": "0x539bdE0d7Dbd336b79148AA742883198BBF60342", "decimals": 18},
        "PENDLE":  {"address": "0x0c880f6761F1af8d9Aa9C466984b80DAb9a8c9e8", "decimals": 18},
        "RDNT":    {"address": "0x3082CC23568eA640225c2467653dB90e9250AaA0", "decimals": 18},
        "GRAIL":   {"address": "0x3d9907F9a368ad0a51Be60f7Da3b97cf940982D8", "decimals": 18},
        "DPX":     {"address": "0x6C2C06790b3E3E3c38e12Ee22F8183b37a13EE55", "decimals": 18},
        "JOE":     {"address": "0x371c7ec6D8039ff7933a2AA28EB827Ffe1F52f07", "decimals": 18},
        "LDO":     {"address": "0x13Ad51ed4F1B7e9Dc168d8a00cB3f4dDD85EfA60", "decimals": 18},
        "LINK":    {"address": "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4", "decimals": 18},
        "DAI":     {"address": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1", "decimals": 18},
        "USDT":    {"address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "decimals": 6},
        "LUSD":    {"address": "0x93b346b6BC2548dA6A1E7d98E9a421B42541425b", "decimals": 18},
        # Quote currency — used internally
        "USDC":    {"address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
    },

    # ── Base ──────────────────────────────────────────────────────────────────
    "Base": {
        "WETH":    {"address": "0x4200000000000000000000000000000000000006", "decimals": 18},
        "WBTC":    {"address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "decimals": 8},
        "LINK":    {"address": "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196", "decimals": 18},
        "cbETH":   {"address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": 18},
        "DAI":     {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18},
        "USDT":    {"address": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "decimals": 6},
        "AAVE":    {"address": "0xA700b4eB416Be35b2911fd5Dee80678ff64fF6C9", "decimals": 18},
        "UNI":     {"address": "0xc3De830EA07524a0761646a6a4e4be0e114a3C83", "decimals": 18},
        "SNX":     {"address": "0x22e6966B799c4D5B13BE962E1D117b56327FDa66", "decimals": 18},
        # Quote currency — used internally
        "USDC":    {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    },
}

# ---------------------------------------------------------------------------
# DEX configurations — 2 per chain for same-chain arbitrage detection
#
# Each entry uses a SINGLE factory so the two DEXes on the same chain
# quote from independent liquidity pools (no duplicate prices).
# ---------------------------------------------------------------------------
DEXES = {
    # ── Polygon DEX 1 ─────────────────────────────────────────────────────────
    "Uniswap V3 (Polygon)": {
        "chain":       "Polygon",
        "factories":   ["0x1F98431c8aD98523631AE4a59f267346ea31F984"],
        "dex_name":    "Uniswap V3",
        "rpc_url":     "https://polygon-bor-rpc.publicnode.com",
        "native_token": "MATIC",
        "fee_tiers":   [500, 3000, 10000],
        "gas_units":   300_000,
        "router":      "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        "aave_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "swap_url": (
            "https://app.uniswap.org/#/swap"
            "?chain=polygon&inputCurrency={token_in}&outputCurrency={token_out}"
        ),
    },

    # ── Polygon DEX 2 ─────────────────────────────────────────────────────────
    "SushiSwap V3 (Polygon)": {
        "chain":       "Polygon",
        "factories":   ["0x917933899c6a5F8E37F31E19f92CdBFF7e8FF0e2"],
        "dex_name":    "SushiSwap V3",
        "rpc_url":     "https://polygon-bor-rpc.publicnode.com",
        "native_token": "MATIC",
        # SushiSwap V3 fee tiers (same as Uniswap V3)
        "fee_tiers":   [500, 3000, 100, 10000],
        "gas_units":   300_000,
        "router":      "0x0a6e511Fe663827b9cA7e2D2542b20B37fC217A6",
        "aave_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "swap_url": (
            "https://www.sushi.com/swap"
            "?chainId=137&token0={token_in}&token1={token_out}"
        ),
    },

    # ── Arbitrum DEX 1 ────────────────────────────────────────────────────────
    "Uniswap V3 (Arbitrum)": {
        "chain":       "Arbitrum",
        "factories":   ["0x1F98431c8aD98523631AE4a59f267346ea31F984"],
        "dex_name":    "Uniswap V3",
        "rpc_url":     "https://arb1.arbitrum.io/rpc",
        "native_token": "ETH",
        "fee_tiers":   [500, 3000, 100, 10000],
        "gas_units":   300_000,
        "router":      "0xE592427A0AEce92De3Edee1F18E0157C05861564",
        "aave_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "swap_url": (
            "https://app.uniswap.org/#/swap"
            "?chain=arbitrum&inputCurrency={token_in}&outputCurrency={token_out}"
        ),
    },

    # ── Arbitrum DEX 2 ────────────────────────────────────────────────────────
    "SushiSwap V3 (Arbitrum)": {
        "chain":       "Arbitrum",
        # Single factory only — no Uniswap fallback, keeps prices independent
        "factories":   ["0x1af415a1EbA07a4986a52B6f2e7dE7003D82231b"],
        "dex_name":    "SushiSwap V3",
        "rpc_url":     "https://arb1.arbitrum.io/rpc",
        "native_token": "ETH",
        "fee_tiers":   [500, 3000, 100, 10000],
        "gas_units":   300_000,
        "router":      "0x8A21F6768C1f8075791D08546Dadf6daA0bE820c",
        "aave_addresses_provider": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
        "swap_url": (
            "https://www.sushi.com/swap"
            "?chainId=42161&token0={token_in}&token1={token_out}"
        ),
    },

    # ── Base DEX 1 ────────────────────────────────────────────────────────────
    "Uniswap V3 (Base)": {
        "chain":       "Base",
        "factories":   ["0x33128a8fC17869897dcE68Ed026d694621f6FDfD"],
        "dex_name":    "Uniswap V3",
        "rpc_url":     "https://mainnet.base.org",
        "native_token": "ETH",
        "fee_tiers":   [500, 3000, 100, 10000],
        "gas_units":   300_000,
        "router":      "0x2626664c2603336E57B271c5C0b26F421741e481",
        "aave_addresses_provider": "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D",
        "swap_url": (
            "https://app.uniswap.org/#/swap"
            "?chain=base&inputCurrency={token_in}&outputCurrency={token_out}"
        ),
    },

    # ── Base DEX 2 ────────────────────────────────────────────────────────────
    "PancakeSwap V3 (Base)": {
        "chain":       "Base",
        "factories":   ["0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"],
        "dex_name":    "PancakeSwap V3",
        "rpc_url":     "https://mainnet.base.org",
        "native_token": "ETH",
        "fee_tiers":   [500, 2500, 100, 10000],
        "gas_units":   300_000,
        "router":      "0x1b81D678ffb9C0263b24A97847620C99d213eB14",
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

# Base floor for the dynamic profit gate.
# Active floor used by the scanner = MIN_NET_PROFIT_USD + 2.5 × estimated gas cost.
# This auto-adjusts upward when the network is congested and gas is expensive,
# so the bot only fires trades whose profit comfortably beats network fees.
# Live-mutable from Telegram via /setprofit <usd>.
MIN_NET_PROFIT_USD = 5.0

# Flash loan fee in basis points (0.05% = 5 bps, standard Aave V3)
FLASH_LOAN_FEE_BPS = 5

# Notional trade size in USD used for profit simulation
TRADE_SIZE_USD = 10_000
