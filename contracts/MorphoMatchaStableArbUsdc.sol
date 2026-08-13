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

/// @notice Atomic Ethereum (PYUSD <-> USDC) -> intermediate-token arbitrage executor.
contract MorphoMatchaStableArbUsdc {
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
    uint256 private _startingIntermediate;
    uint256 private _minimumProfit;
    address private _loanToken;
    address private _intermediateToken;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event ArbitrageExecuted(
        uint256 loanAmount,
        address indexed loanToken,
        address indexed intermediateToken,
        uint256 stableAmountIn,
        uint256 profit,
        uint256 intermediateDust
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

    /// @notice Executes a PYUSD <-> USDC arbitrage route.
    function executeArbitrageWithLoanToken(
        uint256 loanAmount,
        address loanToken,
        MatchaRoute calldata matcha,
        StableOrder calldata stable,
        uint256 minProfit
    ) external payable onlyOwner onlyIdle {
        address intermediate = (loanToken == PYUSD) ? USDC : PYUSD;
        _executeArbitrage(
            loanAmount,
            loanToken,
            intermediate,
            matcha,
            stable,
            minProfit
        );
    }

    /// @notice Explicit entry point allowing explicit intermediate token specification.
    function executeArbitrageWithTokens(
        uint256 loanAmount,
        address loanToken,
        address intermediateToken,
        MatchaRoute calldata matcha,
        StableOrder calldata stable,
        uint256 minProfit
    ) external payable onlyOwner onlyIdle {
        _executeArbitrage(
            loanAmount,
            loanToken,
            intermediateToken,
            matcha,
            stable,
            minProfit
        );
    }

    function _executeArbitrage(
        uint256 loanAmount,
        address loanToken,
        address intermediateToken,
        MatchaRoute calldata matcha,
        StableOrder calldata stable,
        uint256 minProfit
    ) private {
        if (block.chainid != 1) revert WrongChain();
        if (loanAmount == 0 || stable.amountIn == 0) revert InvalidAmount();
        if (!_isSupportedToken(loanToken) || !_isSupportedToken(intermediateToken)) {
            revert InvalidLoanToken();
        }
        if (loanToken == intermediateToken) revert InvalidRoute();
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
        _startingIntermediate = _balanceOf(intermediateToken);
        _minimumProfit = minProfit;
        _loanToken = loanToken;
        _intermediateToken = intermediateToken;
        phase = Phase.LoanRequested;

        IMorphoFlashLoan(MORPHO).flashLoan(
            loanToken,
            loanAmount,
            abi.encode(loanToken, intermediateToken, matcha, stable)
        );

        if (phase != Phase.InCallback) revert InvalidCallback();
        phase = Phase.Idle;

        uint256 endingLoanToken = _balanceOf(loanToken);
        uint256 requiredEnding = _startingLoanToken + minProfit;
        if (endingLoanToken < requiredEnding) {
            revert InsufficientProfit(endingLoanToken, requiredEnding);
        }

        uint256 profit = endingLoanToken - _startingLoanToken;
        uint256 endingIntermediate = _balanceOf(intermediateToken);
        uint256 intermediateDust = endingIntermediate > _startingIntermediate
            ? endingIntermediate - _startingIntermediate
            : 0;

        _pendingLoanAmount = 0;
        _startingLoanToken = 0;
        _startingIntermediate = 0;
        _minimumProfit = 0;
        _loanToken = address(0);
        _intermediateToken = address(0);

        if (profit != 0) _safeTransfer(loanToken, owner, profit);
        if (intermediateDust != 0) _safeTransfer(intermediateToken, owner, intermediateDust);
        emit ArbitrageExecuted(
            loanAmount,
            loanToken,
            intermediateToken,
            stable.amountIn,
            profit,
            intermediateDust
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
            address intermediateToken,
            MatchaRoute memory matcha,
            StableOrder memory stable
        ) = abi.decode(
            data,
            (address, address, MatchaRoute, StableOrder)
        );
        if (
            matcha.sellAmount != assets ||
            loanToken != _loanToken ||
            intermediateToken != _intermediateToken
        ) {
            revert InvalidRoute();
        }
        if (_balanceOf(loanToken) < _startingLoanToken + assets) {
            revert InvalidCallback();
        }

        _forceApprove(loanToken, matcha.allowanceTarget, assets);
        _call(matcha.target, matcha.value, matcha.data);
        _forceApprove(loanToken, matcha.allowanceTarget, 0);

        uint256 currentIntermediate = _balanceOf(intermediateToken);
        uint256 receivedIntermediate = currentIntermediate > _startingIntermediate
            ? currentIntermediate - _startingIntermediate
            : 0;
        if (receivedIntermediate < stable.amountIn) {
            revert InsufficientMatchaOutput(receivedIntermediate, stable.amountIn);
        }

        _forceApprove(intermediateToken, STABLE_POOL, stable.amountIn);
        IStablePool.SwapLocal memory params = IStablePool.SwapLocal({
            amountIn: stable.amountIn,
            tokenIn: intermediateToken,
            tokenOut: loanToken,
            chainId: uint64(block.chainid),
            recipient: address(this),
            deadline: stable.deadline,
            nonce: stable.nonce
        });
        IStablePool(STABLE_POOL).singleChainSwap{
            value: stable.executionFeeNative
        }(params, stable.maintainerSignature, stable.executionFeeNative);
        _forceApprove(intermediateToken, STABLE_POOL, 0);

        uint256 currentLoanToken = _balanceOf(loanToken);
        uint256 requiredLoanToken = _startingLoanToken + assets + _minimumProfit;
        if (currentLoanToken < requiredLoanToken) {
            revert InsufficientProfit(currentLoanToken, requiredLoanToken);
        }

        _forceApprove(loanToken, MORPHO, assets);
    }

    function transferOwnership(address newOwner) external onlyOwner onlyIdle {
        if (newOwner == address(0)) revert InvalidAddress();
        address previousOwner = owner;
        owner = newOwner;
        emit OwnershipTransferred(previousOwner, newOwner);
    }

    function sweep(address token, uint256 amount) external onlyOwner onlyIdle {
        if (token == address(0)) revert InvalidAddress();
        _safeTransfer(token, owner, amount);
    }

    function sweepNative(uint256 amount) external onlyOwner onlyIdle {
        (bool ok, ) = payable(owner).call{value: amount}("");
        if (!ok) revert ExternalCallFailed(owner, "");
    }

    function supportsLoanToken(address token) external pure returns (bool) {
        return _isSupportedToken(token);
    }

    function _isSupportedToken(address token) private pure returns (bool) {
        return token == USDC || token == PYUSD || token == USDT;
    }

    function _balanceOf(address token) private view returns (uint256 balance) {
        (bool ok, bytes memory data) = token.staticcall(
            abi.encodeWithSelector(IERC20Minimal.balanceOf.selector, address(this))
        );
        if (!ok || data.length < 32) revert TokenCallFailed(token);
        return abi.decode(data, (uint256));
    }

    function _forceApprove(address token, address spender, uint256 amount) private {
        (bool ok, bytes memory data) = token.call(
            abi.encodeWithSelector(IERC20Minimal.approve.selector, spender, amount)
        );
        if (ok && (data.length == 0 || abi.decode(data, (bool)))) return;

        (ok, data) = token.call(
            abi.encodeWithSelector(IERC20Minimal.approve.selector, spender, 0)
        );
        if (!ok || (data.length > 0 && !abi.decode(data, (bool)))) {
            revert TokenCallFailed(token);
        }

        (ok, data) = token.call(
            abi.encodeWithSelector(IERC20Minimal.approve.selector, spender, amount)
        );
        if (!ok || (data.length > 0 && !abi.decode(data, (bool)))) {
            revert TokenCallFailed(token);
        }
    }

    function _safeTransfer(address token, address recipient, uint256 amount) private {
        (bool ok, bytes memory data) = token.call(
            abi.encodeWithSelector(
                IERC20Minimal.transfer.selector,
                recipient,
                amount
            )
        );
        if (!ok || (data.length > 0 && !abi.decode(data, (bool)))) {
            revert TokenCallFailed(token);
        }
    }

    function _call(address target, uint256 value, bytes memory data) private {
        (bool ok, bytes memory result) = target.call{value: value}(data);
        if (!ok) revert ExternalCallFailed(target, result);
    }
}
