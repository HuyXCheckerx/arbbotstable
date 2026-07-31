// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20Minimal {
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address recipient, uint256 amount) external returns (bool);
}

interface IMorphoFlashLoan {
    function flashLoan(address token, uint256 assets, bytes calldata data) external;
}

interface IStablePool {
    struct SwapLocal {
        uint256 amountIn;
        address tokenIn;
        address tokenOut;
        uint64 chainId;
        address recipient;
        uint64 deadline;
        uint256 nonce;
    }

    function singleChainSwap(
        SwapLocal calldata params,
        bytes calldata maintainerSignature,
        uint256 executionFeeNative
    ) external payable;
}

/// @notice Atomic Ethereum (USDC or PYUSD) -> USDT -> loan-token arbitrage executor.
/// @dev Matcha's target/calldata are owner-supplied. Stable's contract and the
///      supported tokens are fixed so the second leg cannot be redirected.
contract MorphoMatchaStableArb {
    address public constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address public constant USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
    address public constant USDT = 0xdAC17F958D2ee523a2206206994597C13D831ec7;
    address public constant PYUSD = 0x6c3ea9036406852006290770BEdFcAbA0e23A0e8;
    address public constant STABLE_POOL = 0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da;

    struct MatchaRoute {
        address target;
        address allowanceTarget;
        uint256 sellAmount;
        uint256 value;
        bytes data;
    }

    struct StableOrder {
        uint256 amountIn;
        uint64 deadline;
        uint256 nonce;
        bytes maintainerSignature;
        uint256 executionFeeNative;
    }

    enum Phase {
        Idle,
        LoanRequested,
        InCallback
    }

    address public owner;
    Phase public phase;

    uint256 private _pendingLoanAmount;
    uint256 private _startingLoanToken;
    uint256 private _startingUsdt;
    uint256 private _minimumProfit;
    address private _loanToken;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event ArbitrageExecuted(
        uint256 loanAmount,
        address indexed loanToken,
        uint256 stableAmountIn,
        uint256 profit,
        uint256 usdtDust
    );

    error NotOwner();
    error WrongChain();
    error InvalidAddress();
    error InvalidAmount();
    error InvalidRoute();
    error InvalidLoanToken();
    error InvalidNativeValue();
    error InvalidCallback();
    error InsufficientMatchaOutput(uint256 received, uint256 required);
    error InsufficientProfit(uint256 available, uint256 required);
    error TokenCallFailed(address token);
    error ExternalCallFailed(address target, bytes reason);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyIdle() {
        if (phase != Phase.Idle) revert InvalidCallback();
        _;
    }

    constructor(address initialOwner) {
        if (initialOwner == address(0)) revert InvalidAddress();
        owner = initialOwner;
        emit OwnershipTransferred(address(0), initialOwner);
    }

    receive() external payable {}

    /// @notice Backward-compatible USDC -> USDT -> USDC entry point.
    function executeArbitrage(
        uint256 loanAmount,
        MatchaRoute calldata matcha,
        StableOrder calldata stable,
        uint256 minProfit
    ) external payable onlyOwner onlyIdle {
        _executeArbitrage(loanAmount, USDC, matcha, stable, minProfit);
    }

    /// @notice Executes a (USDC or PYUSD) -> USDT -> loan-token route.
    function executeArbitrageWithLoanToken(
        uint256 loanAmount,
        address loanToken,
        MatchaRoute calldata matcha,
        StableOrder calldata stable,
        uint256 minProfit
    ) external payable onlyOwner onlyIdle {
        _executeArbitrage(
            loanAmount,
            loanToken,
            matcha,
            stable,
            minProfit
        );
    }

    function _executeArbitrage(
        uint256 loanAmount,
        address loanToken,
        MatchaRoute calldata matcha,
        StableOrder calldata stable,
        uint256 minProfit
    ) private {
        if (block.chainid != 1) revert WrongChain();
        if (loanAmount == 0 || stable.amountIn == 0) revert InvalidAmount();
        if (!_isSupportedLoanToken(loanToken)) {
            revert InvalidLoanToken();
        }
        if (
            matcha.target.code.length == 0 ||
            matcha.allowanceTarget.code.length == 0 ||
            matcha.data.length < 4 ||
            matcha.sellAmount != loanAmount
        ) revert InvalidRoute();
        if (stable.deadline <= block.timestamp) revert InvalidRoute();

        uint256 requiredValue = matcha.value + stable.executionFeeNative;
        if (msg.value != requiredValue) revert InvalidNativeValue();

        _pendingLoanAmount = loanAmount;
        _startingLoanToken = _balanceOf(loanToken);
        _startingUsdt = _balanceOf(USDT);
        _minimumProfit = minProfit;
        _loanToken = loanToken;
        phase = Phase.LoanRequested;

        IMorphoFlashLoan(MORPHO).flashLoan(
            loanToken,
            loanAmount,
            abi.encode(loanToken, matcha, stable)
        );

        if (phase != Phase.InCallback) revert InvalidCallback();
        phase = Phase.Idle;

        uint256 endingLoanToken = _balanceOf(loanToken);
        uint256 requiredEnding = _startingLoanToken + minProfit;
        if (endingLoanToken < requiredEnding) {
            revert InsufficientProfit(endingLoanToken, requiredEnding);
        }

        uint256 profit = endingLoanToken - _startingLoanToken;
        uint256 endingUsdt = _balanceOf(USDT);
        uint256 usdtDust = endingUsdt > _startingUsdt
            ? endingUsdt - _startingUsdt
            : 0;

        _pendingLoanAmount = 0;
        _startingLoanToken = 0;
        _startingUsdt = 0;
        _minimumProfit = 0;
        _loanToken = address(0);

        if (profit != 0) _safeTransfer(loanToken, owner, profit);
        if (usdtDust != 0) _safeTransfer(USDT, owner, usdtDust);
        emit ArbitrageExecuted(
            loanAmount,
            loanToken,
            stable.amountIn,
            profit,
            usdtDust
        );
    }

    /// @dev Morpho calls this on msg.sender during flashLoan().
    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external {
        if (
            msg.sender != MORPHO ||
            phase != Phase.LoanRequested ||
            assets != _pendingLoanAmount
        ) revert InvalidCallback();
        phase = Phase.InCallback;

        (
            address loanToken,
            MatchaRoute memory matcha,
            StableOrder memory stable
        ) = abi.decode(
            data,
            (address, MatchaRoute, StableOrder)
        );
        if (
            matcha.sellAmount != assets ||
            loanToken != _loanToken ||
            !_isSupportedLoanToken(loanToken)
        ) {
            revert InvalidRoute();
        }
        if (_balanceOf(loanToken) < _startingLoanToken + assets) {
            revert InvalidCallback();
        }

        _forceApprove(loanToken, matcha.allowanceTarget, assets);
        _call(matcha.target, matcha.value, matcha.data);
        _forceApprove(loanToken, matcha.allowanceTarget, 0);

        uint256 currentUsdt = _balanceOf(USDT);
        uint256 receivedUsdt = currentUsdt > _startingUsdt
            ? currentUsdt - _startingUsdt
            : 0;
        if (receivedUsdt < stable.amountIn) {
            revert InsufficientMatchaOutput(receivedUsdt, stable.amountIn);
        }

        _forceApprove(USDT, STABLE_POOL, stable.amountIn);
        IStablePool.SwapLocal memory params = IStablePool.SwapLocal({
            amountIn: stable.amountIn,
            tokenIn: USDT,
            tokenOut: loanToken,
            chainId: uint64(block.chainid),
            recipient: address(this),
            deadline: stable.deadline,
            nonce: stable.nonce
        });
        IStablePool(STABLE_POOL).singleChainSwap{
            value: stable.executionFeeNative
        }(params, stable.maintainerSignature, stable.executionFeeNative);
        _forceApprove(USDT, STABLE_POOL, 0);

        uint256 currentLoanToken = _balanceOf(loanToken);
        uint256 requiredLoanToken = _startingLoanToken + assets + _minimumProfit;
        if (currentLoanToken < requiredLoanToken) {
            revert InsufficientProfit(currentLoanToken, requiredLoanToken);
        }

        // Morpho pulls the principal after this callback returns. Its fee is zero.
        _forceApprove(loanToken, MORPHO, assets);
    }

    function transferOwnership(address newOwner) external onlyOwner onlyIdle {
        if (newOwner == address(0)) revert InvalidAddress();
        address previousOwner = owner;
        owner = newOwner;
        emit OwnershipTransferred(previousOwner, newOwner);
    }

    /// @notice Recovers tokens accidentally sent to the executor while idle.
    function sweep(address token, uint256 amount) external onlyOwner onlyIdle {
        if (token == address(0)) revert InvalidAddress();
        _safeTransfer(token, owner, amount);
    }

    function sweepNative(uint256 amount) external onlyOwner onlyIdle {
        (bool ok, ) = payable(owner).call{value: amount}("");
        if (!ok) revert ExternalCallFailed(owner, "");
    }

    function supportsLoanToken(address token) external pure returns (bool) {
        return _isSupportedLoanToken(token);
    }

    function _isSupportedLoanToken(address token) private pure returns (bool) {
        return token == USDC || token == PYUSD;
    }

    function _balanceOf(address token) private view returns (uint256 balance) {
        (bool ok, bytes memory result) = token.staticcall(
            abi.encodeCall(IERC20Minimal.balanceOf, (address(this)))
        );
        if (!ok || result.length < 32) revert TokenCallFailed(token);
        balance = abi.decode(result, (uint256));
    }

    function _forceApprove(address token, address spender, uint256 amount) private {
        if (_allowance(token, spender) != amount) {
            if (!_tryApprove(token, spender, amount)) {
                if (!_tryApprove(token, spender, 0)) revert TokenCallFailed(token);
                if (!_tryApprove(token, spender, amount)) revert TokenCallFailed(token);
            }
        }
    }

    function _allowance(address token, address spender) private view returns (uint256 value) {
        (bool ok, bytes memory result) = token.staticcall(
            abi.encodeCall(IERC20Minimal.allowance, (address(this), spender))
        );
        if (!ok || result.length < 32) revert TokenCallFailed(token);
        value = abi.decode(result, (uint256));
    }

    function _tryApprove(address token, address spender, uint256 amount) private returns (bool) {
        (bool ok, bytes memory result) = token.call(
            abi.encodeCall(IERC20Minimal.approve, (spender, amount))
        );
        return ok && (result.length == 0 || (result.length >= 32 && abi.decode(result, (bool))));
    }

    function _safeTransfer(address token, address recipient, uint256 amount) private {
        (bool ok, bytes memory result) = token.call(
            abi.encodeCall(IERC20Minimal.transfer, (recipient, amount))
        );
        if (!ok || (result.length != 0 && !abi.decode(result, (bool)))) {
            revert TokenCallFailed(token);
        }
    }

    function _call(address target, uint256 value, bytes memory callData) private {
        (bool ok, bytes memory reason) = target.call{value: value}(callData);
        if (!ok) revert ExternalCallFailed(target, reason);
    }
}
