// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// =============================================================================
//  FlashLoanExecutor — Single-chain arbitrage via Aave V3 flash loans
// =============================================================================
//
//  EXECUTION FLOW (all steps in one atomic transaction)
//  ─────────────────────────────────────────────────────
//  1. Owner calls initiateArbitrage(params)
//  2. Contract borrows `tokenIn` from Aave V3 pool (zero collateral)
//  3. Aave calls back executeOperation(...)
//     a. Approve DEX A router → swap tokenIn → tokenOut
//     b. Approve DEX B router → swap tokenOut → tokenIn
//     c. Approve Aave pool to pull back (amount + premium)
//     d. Send remaining profit to owner
//  4. Transaction reverts automatically if profit < repayment (self-enforcing)
//
//  ⚠  IMPORTANT — CROSS-CHAIN NOTE
//  Flash loans are atomic: borrow, swap, repay must all happen in ONE tx on
//  ONE chain.  The Python hunter detects cross-chain price gaps for awareness;
//  this contract targets same-chain opportunities (e.g. Uniswap V3 vs
//  SushiSwap V3 on Polygon, or two DEXes on Arbitrum/Base).
//
//  DEPLOYED POOL ADDRESS PROVIDERS (Aave V3)
//  ─────────────────────────────────────────
//  Polygon  : 0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb
//  Arbitrum : 0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb
//  Base     : 0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D
//
//  UNISWAP V3-COMPATIBLE ROUTERS (use as routerA / routerB)
//  ─────────────────────────────────────────────────────────
//  Uniswap V3  (Polygon)  : 0xE592427A0AEce92De3Edee1F18E0157C05861564
//  SushiSwap V3 (Arbitrum): 0x8A21F6768C1f8075791D08546Dadf6daA0bE820c
//  PancakeSwap V3 (Base)  : 0x1b81D678ffb9C0263b24A97847620C99d213eB14
// =============================================================================

// ---------------------------------------------------------------------------
//  Minimal IERC20
// ---------------------------------------------------------------------------
interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

// ---------------------------------------------------------------------------
//  Aave V3 — IFlashLoanSimpleReceiver
// ---------------------------------------------------------------------------
interface IFlashLoanSimpleReceiver {
    /**
     * @notice Called by Aave after funds are sent to the receiver contract.
     * @param asset        The address of the flash-borrowed asset.
     * @param amount       The amount borrowed (before premium).
     * @param premium      The fee owed in addition to `amount`.
     * @param initiator    The address that called flashLoanSimple.
     * @param params       Arbitrary bytes passed through from the initiator.
     * @return             Must return true or the tx reverts.
     */
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

// ---------------------------------------------------------------------------
//  Aave V3 — IPool (only the functions we call)
// ---------------------------------------------------------------------------
interface IPool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
         amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

// ---------------------------------------------------------------------------
//  Aave V3 — IPoolAddressesProvider
// ---------------------------------------------------------------------------
interface IPoolAddressesProvider {
    function getPool() external view returns (address);
}

// ---------------------------------------------------------------------------
//  Uniswap V3-compatible swap router  (works with SushiSwap V3, PancakeSwap V3)
// ---------------------------------------------------------------------------
interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params)
        external
        payable
        returns (uint256 amountOut);
}

// =============================================================================
//  FlashLoanExecutor
// =============================================================================
contract FlashLoanExecutor is IFlashLoanSimpleReceiver {

    // -------------------------------------------------------------------------
    //  State
    // -------------------------------------------------------------------------
    address public immutable owner;
    IPoolAddressesProvider public immutable addressesProvider;

    // Minimum acceptable output on each leg (basis points of input).
    // 9950 = 99.5%, i.e. max 0.5% slippage per leg. Adjustable by owner.
    uint256 public slippageToleranceBps = 9950;

    // -------------------------------------------------------------------------
    //  Structs
    // -------------------------------------------------------------------------

    /**
     * @notice All parameters needed to define a two-leg same-chain arbitrage.
     *
     * @param routerA       DEX A router address (buy leg — tokenIn → tokenOut)
     * @param routerB       DEX B router address (sell leg — tokenOut → tokenIn)
     * @param tokenIn       Token we borrow (and repay) — e.g. WETH, USDC
     * @param tokenOut      Intermediate token — e.g. WBTC, LINK
     * @param feeA          Pool fee tier on DEX A  (e.g. 500 = 0.05%)
     * @param feeB          Pool fee tier on DEX B  (e.g. 3000 = 0.30%)
     * @param loanAmount    Exact flash-loan amount (18-decimal units for WETH)
     * @param minProfit     Minimum net profit in tokenIn units; revert if missed
     */
    struct ArbParams {
        address routerA;
        address routerB;
        address tokenIn;
        address tokenOut;
        uint24  feeA;
        uint24  feeB;
        uint256 loanAmount;
        uint256 minProfit;
    }

    // -------------------------------------------------------------------------
    //  Events
    // -------------------------------------------------------------------------
    event ArbitrageExecuted(
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 loanAmount,
        uint256 profit,
        address indexed recipient
    );

    event SlippageUpdated(uint256 oldBps, uint256 newBps);

    // -------------------------------------------------------------------------
    //  Modifiers
    // -------------------------------------------------------------------------
    modifier onlyOwner() {
        require(msg.sender == owner, "FlashLoanExecutor: caller is not owner");
        _;
    }

    modifier onlyAavePool() {
        require(
            msg.sender == addressesProvider.getPool(),
            "FlashLoanExecutor: caller is not Aave pool"
        );
        _;
    }

    // -------------------------------------------------------------------------
    //  Constructor
    // -------------------------------------------------------------------------
    /**
     * @param _addressesProvider  Aave V3 PoolAddressesProvider for the target chain.
     *                            See the address table at the top of this file.
     */
    constructor(address _addressesProvider) {
        require(_addressesProvider != address(0), "Zero address provider");
        owner             = msg.sender;
        addressesProvider = IPoolAddressesProvider(_addressesProvider);
    }

    // -------------------------------------------------------------------------
    //  Owner actions
    // -------------------------------------------------------------------------

    /**
     * @notice Kick off a two-leg flash-loan arbitrage.
     *         Only callable by the owner (you).
     *
     * @param params  ArbParams struct populated by the Python hunter.
     */
    function initiateArbitrage(ArbParams calldata params) external onlyOwner {
        require(params.routerA  != address(0), "routerA is zero");
        require(params.routerB  != address(0), "routerB is zero");
        require(params.tokenIn  != address(0), "tokenIn is zero");
        require(params.tokenOut != address(0), "tokenOut is zero");
        require(params.loanAmount > 0,         "loanAmount is zero");

        bytes memory encodedParams = abi.encode(params);

        IPool(addressesProvider.getPool()).flashLoanSimple(
            address(this),    // receiver — this contract
            params.tokenIn,   // asset to borrow
            params.loanAmount,
            encodedParams,    // passed back verbatim to executeOperation
            0                 // referral code (unused)
        );
    }

    /**
     * @notice Update the per-leg slippage tolerance.
     * @param newBps  New tolerance in basis points (e.g. 9950 = 0.5% max slippage).
     */
    function setSlippageTolerance(uint256 newBps) external onlyOwner {
        require(newBps <= 10000, "Cannot exceed 100%");
        require(newBps >= 9000,  "Too loose — max 10% slippage allowed");
        emit SlippageUpdated(slippageToleranceBps, newBps);
        slippageToleranceBps = newBps;
    }

    /**
     * @notice Emergency token rescue — sweep any ERC-20 stuck in this contract.
     *         Only callable by owner.
     */
    function rescueTokens(address token, uint256 amount) external onlyOwner {
        IERC20(token).transfer(owner, amount);
    }

    /**
     * @notice Emergency ETH rescue.
     */
    function rescueETH() external onlyOwner {
        (bool ok, ) = owner.call{value: address(this).balance}("");
        require(ok, "ETH transfer failed");
    }

    // -------------------------------------------------------------------------
    //  Aave V3 flash-loan callback
    // -------------------------------------------------------------------------

    /**
     * @notice Called by the Aave pool immediately after it sends the borrowed
     *         funds to this contract.  All arbitrage logic lives here.
     *
     *         By the time this function returns:
     *           • (amount + premium) must be approved back to the Aave pool
     *           • Profit is transferred to owner
     *
     *         If the trade is not profitable enough the require() revert unwinds
     *         the entire transaction — no funds are lost.
     */
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override onlyAavePool returns (bool) {
        require(initiator == address(this), "FlashLoanExecutor: invalid initiator");

        ArbParams memory arb = abi.decode(params, (ArbParams));

        uint256 repayAmount = amount + premium;

        // ── LEG A: tokenIn → tokenOut on DEX A (the cheaper venue) ──────────
        uint256 amountOutA = _swap(
            arb.routerA,
            arb.tokenIn,
            arb.tokenOut,
            arb.feeA,
            amount,
            0   // amountOutMinimum set dynamically below
        );

        // ── LEG B: tokenOut → tokenIn on DEX B (the more expensive venue) ──
        uint256 amountOutB = _swap(
            arb.routerB,
            arb.tokenOut,
            arb.tokenIn,
            arb.feeB,
            amountOutA,
            repayAmount   // must receive at least enough to repay
        );

        // ── Verify minimum profit ────────────────────────────────────────────
        uint256 profit = amountOutB - repayAmount;
        require(profit >= arb.minProfit, "FlashLoanExecutor: profit below minimum");

        // ── Repay Aave (approve pool to pull repayAmount) ───────────────────
        IERC20(asset).approve(addressesProvider.getPool(), repayAmount);

        // ── Send profit to owner ─────────────────────────────────────────────
        IERC20(asset).transfer(owner, profit);

        emit ArbitrageExecuted(arb.tokenIn, arb.tokenOut, amount, profit, owner);
        return true;
    }

    // -------------------------------------------------------------------------
    //  Internal helpers
    // -------------------------------------------------------------------------

    /**
     * @dev Execute a single Uniswap V3-style exactInputSingle swap.
     *
     * @param router          DEX router (Uniswap V3, SushiSwap V3, or PancakeSwap V3)
     * @param tokenIn         Input token
     * @param tokenOut        Output token
     * @param fee             Pool fee tier
     * @param amountIn        Exact input amount
     * @param amountOutMin    Minimum acceptable output (0 = use slippage bps from storage)
     * @return amountOut      Actual output received
     */
    function _swap(
        address router,
        address tokenIn,
        address tokenOut,
        uint24  fee,
        uint256 amountIn,
        uint256 amountOutMin
    ) internal returns (uint256 amountOut) {
        IERC20(tokenIn).approve(router, amountIn);

        // If no hard minimum was supplied, apply the stored slippage tolerance.
        if (amountOutMin == 0) {
            amountOutMin = (amountIn * slippageToleranceBps) / 10000;
        }

        amountOut = ISwapRouter(router).exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn:           tokenIn,
                tokenOut:          tokenOut,
                fee:               fee,
                recipient:         address(this),
                deadline:          block.timestamp + 60,  // 60-second window
                amountIn:          amountIn,
                amountOutMinimum:  amountOutMin,
                sqrtPriceLimitX96: 0
            })
        );
    }

    // -------------------------------------------------------------------------
    //  Receive ETH (needed if a router wraps/unwraps)
    // -------------------------------------------------------------------------
    receive() external payable {}
}
