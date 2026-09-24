// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20Metadata {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function decimals() external view returns (uint8);
}

/**
 * @title StablePSM
 * @notice Peg Stability Module allowing 1:1 swaps between supported USD stablecoins
 * (USDT, USDC, PYUSD, USDG) with zero slippage and 0.02% protocol fee.
 */
contract StablePSM {
    address public owner;
    address public feeCollector;
    uint256 public feeBps = 2; // 0.02% (2 bps)
    bool public paused = false;

    // Supported stablecoin token addresses
    mapping(address => bool) public isSupported;
    mapping(address => uint8) public tokenDecimals;
    mapping(address => uint256) public maxReserveLimit;

    event StablecoinSwapped(
        address indexed user,
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        uint256 feePaid
    );
    event TokenWhitelisted(address indexed token, uint8 decimals, uint256 maxLimit);
    event FeeUpdated(uint256 newFeeBps);
    event PauseStateChanged(bool isPaused);

    modifier onlyOwner() {
        require(msg.sender == owner, "StablePSM: unauthorized");
        _;
    }

    modifier whenNotPaused() {
        require(!paused, "StablePSM: paused");
        _;
    }

    constructor(address _feeCollector) {
        owner = msg.sender;
        feeCollector = _feeCollector != address(0) ? _feeCollector : msg.sender;
    }

    function whitelistToken(address token, uint8 decimals, uint256 maxLimit) external onlyOwner {
        require(token != address(0), "Zero address");
        isSupported[token] = true;
        tokenDecimals[token] = decimals;
        maxReserveLimit[token] = maxLimit;
        emit TokenWhitelisted(token, decimals, maxLimit);
    }

    function setFeeBps(uint256 _feeBps) external onlyOwner {
        require(_feeBps <= 50, "Max fee is 0.5%");
        feeBps = _feeBps;
        emit FeeUpdated(_feeBps);
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit PauseStateChanged(_paused);
    }

    /**
     * @notice Swap tokenIn for tokenOut 1:1 on a dollar-parity basis
     */
    function swap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minAmountOut
    ) external whenNotPaused returns (uint256 amountOut) {
        require(isSupported[tokenIn] && isSupported[tokenOut], "Token not supported");
        require(tokenIn != tokenOut, "Identical tokens");
        require(amountIn > 0, "Zero amount");

        uint8 decIn = tokenDecimals[tokenIn];
        uint8 decOut = tokenDecimals[tokenOut];

        // Normalize amount to 18 decimals
        uint256 normalizedAmount = amountIn * (10 ** (18 - decIn));

        // Deduct protocol fee (e.g. 2 bps = 0.02%)
        uint256 feeNormalized = (normalizedAmount * feeBps) / 10000;
        uint256 netNormalized = normalizedAmount - feeNormalized;

        // Convert to tokenOut decimals
        amountOut = netNormalized / (10 ** (18 - decOut));
        require(amountOut >= minAmountOut, "Slippage limit violated");

        uint256 feeInTokenOut = feeNormalized / (10 ** (18 - decOut));

        require(
            IERC20Metadata(tokenOut).balanceOf(address(this)) >= amountOut + feeInTokenOut,
            "Insufficient pool liquidity"
        );

        // Pull tokenIn
        require(
            IERC20Metadata(tokenIn).transferFrom(msg.sender, address(this), amountIn),
            "Pull tokenIn failed"
        );

        // Send tokenOut
        require(
            IERC20Metadata(tokenOut).transfer(msg.sender, amountOut),
            "Send tokenOut failed"
        );

        // Send fee to fee collector
        if (feeInTokenOut > 0) {
            IERC20Metadata(tokenOut).transfer(feeCollector, feeInTokenOut);
        }

        emit StablecoinSwapped(msg.sender, tokenIn, tokenOut, amountIn, amountOut, feeInTokenOut);
        return amountOut;
    }
}
