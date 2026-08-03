// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20Multichain {
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address recipient, uint256 amount) external returns (bool);
}

interface IMorphoMultichain {
    function flashLoan(address token, uint256 assets, bytes calldata data) external;
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

interface IStableMultichain {
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

/// @notice Atomic USDC -> USDT -> USDC executor for Polygon and BNB Chain.
/// @dev ProviderKind.Morpho uses Morpho's zero-fee callback. ProviderKind.AaveV3
///      repays the exact premium supplied by Aave's flashLoanSimple callback.
contract MultichainMatchaStableArb {
    enum ProviderKind {
        Morpho,
        AaveV3
    }

    enum Phase {
        Idle,
        LoanRequested,
        InCallback
    }

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

    uint256 public immutable expectedChainId;
    ProviderKind public immutable providerKind;
    address public immutable flashLender;
    address public immutable loanToken;
    address public immutable intermediateToken;
    address public immutable stablePool;

    address public owner;
    Phase public phase;

    uint256 private _pendingLoanAmount;
    uint256 private _startingLoanToken;
    uint256 private _startingIntermediate;
    uint256 private _minimumProfit;
    uint256 private _flashFee;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event ArbitrageExecuted(
        uint256 loanAmount,
        uint256 flashFee,
        uint256 stableAmountIn,
        uint256 profit,
        uint256 intermediateDust
    );

    error NotOwner();
    error WrongChain();
    error InvalidAddress();
    error InvalidAmount();
    error InvalidRoute();
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

    constructor(
        address initialOwner,
        uint256 chainId_,
        ProviderKind providerKind_,
        address flashLender_,
        address loanToken_,
        address intermediateToken_,
        address stablePool_
    ) {
        if (
            initialOwner == address(0) ||
            flashLender_ == address(0) ||
            loanToken_ == address(0) ||
            intermediateToken_ == address(0) ||
            stablePool_ == address(0) ||
            chainId_ == 0
        ) revert InvalidAddress();
        owner = initialOwner;
        expectedChainId = chainId_;
        providerKind = providerKind_;
        flashLender = flashLender_;
        loanToken = loanToken_;
        intermediateToken = intermediateToken_;
        stablePool = stablePool_;
        emit OwnershipTransferred(address(0), initialOwner);
    }

    receive() external payable {}

    function executeArbitrage(
        uint256 loanAmount,
        MatchaRoute calldata matcha,
        StableOrder calldata stable,
        uint256 minProfit
    ) external payable onlyOwner onlyIdle {
        if (block.chainid != expectedChainId) revert WrongChain();
        if (loanAmount == 0 || stable.amountIn == 0) revert InvalidAmount();
        if (
            matcha.target.code.length == 0 ||
            matcha.allowanceTarget.code.length == 0 ||
            matcha.data.length < 4 ||
            matcha.sellAmount != loanAmount ||
            stable.deadline <= block.timestamp
        ) revert InvalidRoute();
        if (msg.value != matcha.value + stable.executionFeeNative) {
            revert InvalidNativeValue();
        }

        _pendingLoanAmount = loanAmount;
        _startingLoanToken = _balanceOf(loanToken);
        _startingIntermediate = _balanceOf(intermediateToken);
        _minimumProfit = minProfit;
        _flashFee = 0;
        phase = Phase.LoanRequested;

        bytes memory callbackData = abi.encode(matcha, stable);
        if (providerKind == ProviderKind.Morpho) {
            IMorphoMultichain(flashLender).flashLoan(
                loanToken,
                loanAmount,
                callbackData
            );
        } else {
            IAaveV3Pool(flashLender).flashLoanSimple(
                address(this),
                loanToken,
                loanAmount,
                callbackData,
                0
            );
        }

        if (phase != Phase.InCallback) revert InvalidCallback();
        phase = Phase.Idle;
        _forceApprove(loanToken, flashLender, 0);

        uint256 endingLoanToken = _balanceOf(loanToken);
        uint256 requiredEnding = _startingLoanToken + minProfit;
        if (endingLoanToken < requiredEnding) {
            revert InsufficientProfit(endingLoanToken, requiredEnding);
        }
        uint256 profit = endingLoanToken - _startingLoanToken;
        uint256 endingIntermediate = _balanceOf(intermediateToken);
        uint256 dust = endingIntermediate > _startingIntermediate
            ? endingIntermediate - _startingIntermediate
            : 0;

        uint256 recordedFee = _flashFee;
        uint256 recordedStableAmount = stable.amountIn;
        _pendingLoanAmount = 0;
        _startingLoanToken = 0;
        _startingIntermediate = 0;
        _minimumProfit = 0;
        _flashFee = 0;

        if (profit != 0) _safeTransfer(loanToken, owner, profit);
        if (dust != 0) _safeTransfer(intermediateToken, owner, dust);
        emit ArbitrageExecuted(
            loanAmount,
            recordedFee,
            recordedStableAmount,
            profit,
            dust
        );
    }

    /// @dev Morpho callback. Morpho pulls the approved principal afterward.
    function onMorphoFlashLoan(uint256 assets, bytes calldata data) external {
        if (
            providerKind != ProviderKind.Morpho ||
            msg.sender != flashLender ||
            phase != Phase.LoanRequested ||
            assets != _pendingLoanAmount
        ) revert InvalidCallback();
        _runRoute(assets, 0, data);
    }

    /// @dev Aave V3 flashLoanSimple callback.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (
            providerKind != ProviderKind.AaveV3 ||
            msg.sender != flashLender ||
            initiator != address(this) ||
            asset != loanToken ||
            phase != Phase.LoanRequested ||
            amount != _pendingLoanAmount
        ) revert InvalidCallback();
        _runRoute(amount, premium, params);
        return true;
    }

    function _runRoute(
        uint256 assets,
        uint256 providerFee,
        bytes calldata data
    ) private {
        phase = Phase.InCallback;
        _flashFee = providerFee;
        (MatchaRoute memory matcha, StableOrder memory stable) = abi.decode(
            data,
            (MatchaRoute, StableOrder)
        );
        if (matcha.sellAmount != assets || stable.deadline <= block.timestamp) {
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

        _forceApprove(intermediateToken, stablePool, stable.amountIn);
        IStableMultichain.SwapLocal memory stableParams = IStableMultichain.SwapLocal({
            amountIn: stable.amountIn,
            tokenIn: intermediateToken,
            tokenOut: loanToken,
            chainId: uint64(block.chainid),
            recipient: address(this),
            deadline: stable.deadline,
            nonce: stable.nonce
        });
        IStableMultichain(stablePool).singleChainSwap{
            value: stable.executionFeeNative
        }(stableParams, stable.maintainerSignature, stable.executionFeeNative);
        _forceApprove(intermediateToken, stablePool, 0);

        uint256 repayment = assets + providerFee;
        uint256 requiredLoanToken =
            _startingLoanToken + repayment + _minimumProfit;
        uint256 currentLoanToken = _balanceOf(loanToken);
        if (currentLoanToken < requiredLoanToken) {
            revert InsufficientProfit(currentLoanToken, requiredLoanToken);
        }
        _forceApprove(loanToken, flashLender, repayment);
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

    function supportsLoanToken(address token) external view returns (bool) {
        return token == loanToken;
    }

    function _balanceOf(address token) private view returns (uint256 balance) {
        (bool ok, bytes memory result) = token.staticcall(
            abi.encodeCall(IERC20Multichain.balanceOf, (address(this)))
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
            abi.encodeCall(IERC20Multichain.allowance, (address(this), spender))
        );
        if (!ok || result.length < 32) revert TokenCallFailed(token);
        value = abi.decode(result, (uint256));
    }

    function _tryApprove(address token, address spender, uint256 amount) private returns (bool) {
        (bool ok, bytes memory result) = token.call(
            abi.encodeCall(IERC20Multichain.approve, (spender, amount))
        );
        return ok && (result.length == 0 || (result.length >= 32 && abi.decode(result, (bool))));
    }

    function _safeTransfer(address token, address recipient, uint256 amount) private {
        (bool ok, bytes memory result) = token.call(
            abi.encodeCall(IERC20Multichain.transfer, (recipient, amount))
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
