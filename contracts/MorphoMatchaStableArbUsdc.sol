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

interface IUniswapV4PoolManager {
    function unlock(bytes calldata data) external returns (bytes memory result);
    function take(address currency, address to, uint256 amount) external;
    function sync(address currency) external;
    function settle() external payable returns (uint256 paid);
}

interface IAaveV3Pool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
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

/// @notice Atomic Ethereum stablecoin arbitrage executor with selectable funding.
contract MorphoMatchaStableArbUsdc {
    address public constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address public constant UNISWAP_V4_POOL_MANAGER = 0x000000000004444c5dc75cB358380D2e3dE08A90;
    address public constant AAVE_V3_POOL = 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2;
    address public constant USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
    address public constant USDT = 0xdAC17F958D2ee523a2206206994597C13D831ec7;
    address public constant PYUSD = 0x6c3ea9036406852006290770BEdFcAbA0e23A0e8;
    address public constant USDG = 0xe343167631d89B6Ffc58B88d6b7fB0228795491D;
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

    enum FlashProvider {
        Morpho,
        UniswapV4,
        AaveV3
    }

    address public owner;
    Phase public phase;

    uint256 private _pendingLoanAmount;
    uint256 private _startingLoanToken;
    uint256 private _startingIntermediate;
    uint256 private _minimumProfit;
    address private _loanToken;
    address private _intermediateToken;
    FlashProvider private _flashProvider;

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

    /// @notice Backward-compatible PYUSD <-> USDC entry point.
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
            FlashProvider.Morpho,
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
            FlashProvider.Morpho,
            matcha,
            stable,
            minProfit
        );
    }

    /// @notice Explicit entry point supporting Morpho, Uniswap v4, or Aave v3.
    function executeArbitrageWithTokensAndProvider(
        uint256 loanAmount,
        address loanToken,
        address intermediateToken,
        FlashProvider flashProvider,
        MatchaRoute calldata matcha,
        StableOrder calldata stable,
        uint256 minProfit
    ) external payable onlyOwner onlyIdle {
        _executeArbitrage(
            loanAmount,
            loanToken,
            intermediateToken,
            flashProvider,
            matcha,
            stable,
            minProfit
        );
    }

    function _executeArbitrage(
        uint256 loanAmount,
        address loanToken,
        address intermediateToken,
        FlashProvider flashProvider,
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
        _flashProvider = flashProvider;
        phase = Phase.LoanRequested;

        bytes memory callbackData = abi.encode(
            loanToken,
            intermediateToken,
            matcha,
            stable
        );
        if (flashProvider == FlashProvider.Morpho) {
            IMorphoFlashLoan(MORPHO).flashLoan(
                loanToken,
                loanAmount,
                callbackData
            );
            _forceApprove(loanToken, MORPHO, 0);
        } else if (flashProvider == FlashProvider.UniswapV4) {
            IUniswapV4PoolManager(UNISWAP_V4_POOL_MANAGER).unlock(callbackData);
        } else {
            IAaveV3Pool(AAVE_V3_POOL).flashLoanSimple(
                address(this),
                loanToken,
                loanAmount,
                callbackData,
                0
            );
            _forceApprove(loanToken, AAVE_V3_POOL, 0);
        }

        if (phase != Phase.InCallback) revert InvalidCallback();
        _finishArbitrage(stable.amountIn);
    }

    function _finishArbitrage(uint256 stableAmountIn) private {
        address loanToken = _loanToken;
        address intermediateToken = _intermediateToken;
        uint256 loanAmount = _pendingLoanAmount;
        uint256 endingLoanToken = _balanceOf(loanToken);
        uint256 requiredEnding = _startingLoanToken + _minimumProfit;
        if (endingLoanToken < requiredEnding) {
            revert InsufficientProfit(endingLoanToken, requiredEnding);
        }

        uint256 profit = endingLoanToken - _startingLoanToken;
        uint256 endingIntermediate = _balanceOf(intermediateToken);
        uint256 intermediateDust = endingIntermediate > _startingIntermediate
            ? endingIntermediate - _startingIntermediate
            : 0;

        phase = Phase.Idle;
        _pendingLoanAmount = 0;
        _startingLoanToken = 0;
        _startingIntermediate = 0;
        _minimumProfit = 0;
        _loanToken = address(0);
        _intermediateToken = address(0);
        _flashProvider = FlashProvider.Morpho;

        if (profit != 0) _safeTransfer(loanToken, owner, profit);
        if (intermediateDust != 0) _safeTransfer(intermediateToken, owner, intermediateDust);
        emit ArbitrageExecuted(
            loanAmount,
            loanToken,
            intermediateToken,
            stableAmountIn,
            profit,
            intermediateDust
        );
    }

    /// @dev Morpho calls this on msg.sender during flashLoan().
    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external {
        if (
            msg.sender != MORPHO ||
            phase != Phase.LoanRequested ||
            _flashProvider != FlashProvider.Morpho ||
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
        _runArbitrage(assets, 0, loanToken, intermediateToken, matcha, stable);
        _forceApprove(loanToken, MORPHO, assets);
    }

    /// @dev Aave v3 calls this during flashLoanSimple().
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata data
    ) external returns (bool) {
        if (
            msg.sender != AAVE_V3_POOL ||
            initiator != address(this) ||
            phase != Phase.LoanRequested ||
            _flashProvider != FlashProvider.AaveV3 ||
            asset != _loanToken ||
            amount != _pendingLoanAmount
        ) revert InvalidCallback();
        phase = Phase.InCallback;

        (
            address loanToken,
            address intermediateToken,
            MatchaRoute memory matcha,
            StableOrder memory stable
        ) = abi.decode(data, (address, address, MatchaRoute, StableOrder));
        _runArbitrage(
            amount,
            premium,
            loanToken,
            intermediateToken,
            matcha,
            stable
        );
        _forceApprove(loanToken, AAVE_V3_POOL, amount + premium);
        return true;
    }

    /// @dev Uniswap v4 calls this during unlock(). Its take/settle accounting is
    /// a fee-free atomic loan as long as the currency delta returns to zero.
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (
            msg.sender != UNISWAP_V4_POOL_MANAGER ||
            phase != Phase.LoanRequested ||
            _flashProvider != FlashProvider.UniswapV4
        ) revert InvalidCallback();
        phase = Phase.InCallback;

        (
            address loanToken,
            address intermediateToken,
            MatchaRoute memory matcha,
            StableOrder memory stable
        ) = abi.decode(data, (address, address, MatchaRoute, StableOrder));
        uint256 assets = _pendingLoanAmount;
        if (
            matcha.sellAmount != assets ||
            loanToken != _loanToken ||
            intermediateToken != _intermediateToken
        ) revert InvalidRoute();

        IUniswapV4PoolManager manager = IUniswapV4PoolManager(
            UNISWAP_V4_POOL_MANAGER
        );
        manager.take(loanToken, address(this), assets);
        _runArbitrage(assets, 0, loanToken, intermediateToken, matcha, stable);

        manager.sync(loanToken);
        _safeTransfer(loanToken, UNISWAP_V4_POOL_MANAGER, assets);
        if (manager.settle() != assets) revert InvalidCallback();
        return "";
    }

    function _runArbitrage(
        uint256 assets,
        uint256 providerFee,
        address loanToken,
        address intermediateToken,
        MatchaRoute memory matcha,
        StableOrder memory stable
    ) private {
        if (
            matcha.sellAmount != assets ||
            loanToken != _loanToken ||
            intermediateToken != _intermediateToken
        ) revert InvalidRoute();
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
        uint256 requiredLoanToken = _startingLoanToken +
            assets +
            providerFee +
            _minimumProfit;
        if (currentLoanToken < requiredLoanToken) {
            revert InsufficientProfit(currentLoanToken, requiredLoanToken);
        }
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

    function supportsFlashProvider(uint8 provider) external pure returns (bool) {
        return provider <= uint8(FlashProvider.AaveV3);
    }

    function _isSupportedToken(address token) private pure returns (bool) {
        return token == USDC || token == PYUSD || token == USDG || token == USDT;
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
