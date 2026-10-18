// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// Swap executor injected into the local EVM at a fixed address. It is never
// deployed. The engine mints it token balances and approvals (pinned storage
// overrides), then each simulation calls one entry point, which swaps against
// a real pool and returns (amountOut, amountSpent, gasUsed). Amounts are
// balance deltas, so they do not depend on a venue's return values. gasUsed
// covers only the pool interaction, as in an aggregator executor's leg.

struct V4Key {
    address currency0;
    address currency1;
    uint24 fee;
    int24 tickSpacing;
    address hooks;
}

struct V4SwapParams {
    bool zeroForOne;
    int256 amountSpecified;
    uint160 sqrtPriceLimitX96;
}

struct InfKey {
    address currency0;
    address currency1;
    address hooks;
    address poolManager;
    uint24 fee;
    bytes32 parameters;
}

interface IV4PoolManager {
    function unlock(bytes calldata data) external returns (bytes memory);
    function swap(V4Key memory key, V4SwapParams memory params, bytes calldata hookData) external returns (int256);
    function sync(address currency) external;
    function settle() external payable returns (uint256);
    function take(address currency, address to, uint256 amount) external;
}

interface IInfVault {
    function lock(bytes calldata data) external returns (bytes memory);
    function sync(address currency) external;
    function settle() external payable returns (uint256);
    function take(address currency, address to, uint256 amount) external;
}

interface IInfCLPoolManager {
    function swap(InfKey memory key, V4SwapParams memory params, bytes calldata hookData) external returns (int256);
    function getSlot0(bytes32 id) external view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee);
}

interface IExtsload {
    function extsload(bytes32 slot) external view returns (bytes32);
}

contract Helper {
    uint160 internal constant MIN_SQRT_RATIO_PLUS_ONE = 4295128740;
    uint160 internal constant MAX_SQRT_RATIO_MINUS_ONE = 1461446703485210103287273052203988822378723970341;

    // Price limit `limitBps` (in sqrt-price space, so the price moves about twice that) away
    // from the current price, or the protocol bound when 0. It stops a swap that a pool
    // cannot absorb near market price from walking every initialized tick to the bound;
    // such a swap then spends less than its input and the engine treats it as over capacity.
    function _limit(uint160 sqrtP, bool zeroForOne, uint16 limitBps) internal pure returns (uint160) {
        if (limitBps == 0 || sqrtP == 0) return zeroForOne ? MIN_SQRT_RATIO_PLUS_ONE : MAX_SQRT_RATIO_MINUS_ONE;
        uint256 l = zeroForOne ? uint256(sqrtP) * (10000 - limitBps) / 10000 : uint256(sqrtP) * (10000 + limitBps) / 10000;
        if (l <= MIN_SQRT_RATIO_PLUS_ONE) return MIN_SQRT_RATIO_PLUS_ONE;
        if (l >= MAX_SQRT_RATIO_MINUS_ONE) return MAX_SQRT_RATIO_MINUS_ONE;
        return uint160(l);
    }

    function _balance(address token) internal view returns (uint256 b) {
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeWithSelector(0x70a08231, address(this)));
        require(ok && ret.length >= 32, "balanceOf");
        b = abi.decode(ret, (uint256));
    }

    // USDT on Ethereum returns nothing from transfer/approve.
    function _transfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSelector(0xa9059cbb, to, amount));
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "transfer");
    }

    function approve(address token, address spender) external {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSelector(0x095ea7b3, spender, type(uint256).max));
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "approve");
    }

    function _result(address tokenIn, address tokenOut, uint256 in0, uint256 out0, uint256 gas0)
        internal
        view
        returns (uint256 amountOut, uint256 spent, uint256 gasUsed)
    {
        gasUsed = gas0 - gasleft();
        uint256 in1 = _balance(tokenIn);
        uint256 out1 = _balance(tokenOut);
        require(in1 <= in0 && out1 >= out0, "direction");
        spent = in0 - in1;
        amountOut = out1 - out0;
    }

    // Approval or pre-transfer venues: Curve, Balancer v2 Vault, Fluid DEX, DODO, v2 routers.
    // mode 0: call only (the helper approved the spender in setup). mode 1: transfer amountIn to `target` first.
    function callSwap(address tokenIn, address tokenOut, uint256 amountIn, address target, bytes calldata data, uint8 mode)
        external
        returns (uint256, uint256, uint256)
    {
        uint256 in0 = _balance(tokenIn);
        uint256 out0 = _balance(tokenOut);
        uint256 gas0 = gasleft();
        if (mode == 1) _transfer(tokenIn, target, amountIn);
        (bool ok, bytes memory ret) = target.call(data);
        if (!ok) {
            assembly {
                revert(add(ret, 32), mload(ret))
            }
        }
        return _result(tokenIn, tokenOut, in0, out0, gas0);
    }

    // Uniswap v3 and its forks (PancakeSwap v3, Sushi v3, Algebra/QuickSwap v3): the same swap ABI.
    function v3Swap(address pool, bool zeroForOne, uint256 amountIn, address tokenIn, address tokenOut, uint16 limitBps)
        external
        returns (uint256, uint256, uint256)
    {
        uint160 sqrtP;
        if (limitBps != 0) {
            // slot0() (Uniswap, Pancake, Sushi) and globalState() (Algebra) both start with the sqrt price.
            (bool okS, bytes memory s0) = pool.staticcall(abi.encodeWithSelector(0x3850c7bd));
            if (!okS || s0.length < 32) (okS, s0) = pool.staticcall(abi.encodeWithSelector(0xe76c01e4));
            if (okS && s0.length >= 32) sqrtP = uint160(uint256(bytes32(s0)));
        }
        uint160 limit = _limit(sqrtP, zeroForOne, limitBps);
        uint256 in0 = _balance(tokenIn);
        uint256 out0 = _balance(tokenOut);
        uint256 gas0 = gasleft();
        (bool ok, bytes memory ret) = pool.call(
            abi.encodeWithSelector(
                0x128acb08,
                address(this),
                zeroForOne,
                int256(amountIn),
                limit,
                abi.encode(tokenIn)
            )
        );
        if (!ok) {
            assembly {
                revert(add(ret, 32), mload(ret))
            }
        }
        return _result(tokenIn, tokenOut, in0, out0, gas0);
    }

    function _v3Pay(int256 amount0Delta, int256 amount1Delta, bytes calldata data) internal {
        address tokenIn = abi.decode(data, (address));
        uint256 owed = uint256(amount0Delta > 0 ? amount0Delta : amount1Delta);
        _transfer(tokenIn, msg.sender, owed);
    }

    // Maverick v2: pool.swap(recipient, (amount, tokenAIn, exactOutput, tickLimit), data), paid in
    // maverickV2SwapCallback. Price = 1.0001^(tick * tickSpacing); the limit stops ~2*limitBps away.
    function mavSwap(address pool, bool tokenAIn, uint256 amountIn, address tokenIn, address tokenOut, uint16 limitBps)
        external
        returns (uint256, uint256, uint256)
    {
        int32 tickLimit = tokenAIn ? type(int32).max : type(int32).min;
        if (limitBps != 0) {
            (bool okS, bytes memory st) = pool.staticcall(abi.encodeWithSignature("getState()"));
            (bool okT, bytes memory ts) = pool.staticcall(abi.encodeWithSignature("tickSpacing()"));
            if (okS && okT && st.length >= 192 && ts.length >= 32) {
                int256 active = abi.decode(_word(st, 5), (int256));
                uint256 spacing = abi.decode(ts, (uint256));
                int256 k = int256((2 * uint256(limitBps) + spacing - 1) / (spacing == 0 ? 1 : spacing));
                tickLimit = int32(tokenAIn ? active + k : active - k);
            }
        }
        uint256 in0 = _balance(tokenIn);
        uint256 out0 = _balance(tokenOut);
        uint256 gas0 = gasleft();
        (bool ok, bytes memory ret) = pool.call(
            abi.encodeWithSignature(
                "swap(address,(uint256,bool,bool,int32),bytes)", address(this), amountIn, tokenAIn, false, tickLimit, abi.encode(tokenIn)
            )
        );
        if (!ok) {
            assembly {
                revert(add(ret, 32), mload(ret))
            }
        }
        return _result(tokenIn, tokenOut, in0, out0, gas0);
    }

    function _word(bytes memory b, uint256 i) internal pure returns (bytes memory w) {
        w = new bytes(32);
        for (uint256 j = 0; j < 32; j++) w[j] = b[i * 32 + j];
    }

    function maverickV2SwapCallback(address tokenIn, uint256 amountIn, uint256, bytes calldata) external {
        _transfer(tokenIn, msg.sender, amountIn);
    }

    function uniswapV3SwapCallback(int256 a0, int256 a1, bytes calldata data) external { _v3Pay(a0, a1, data); }
    function pancakeV3SwapCallback(int256 a0, int256 a1, bytes calldata data) external { _v3Pay(a0, a1, data); }
    function algebraSwapCallback(int256 a0, int256 a1, bytes calldata data) external { _v3Pay(a0, a1, data); }
    function swapCallback(int256 a0, int256 a1, bytes calldata data) external { _v3Pay(a0, a1, data); }

    // Uniswap v4: PoolManager.unlock -> unlockCallback -> swap, sync, transfer, settle, take.
    function v4Swap(address manager, V4Key calldata key, bool zeroForOne, uint256 amountIn, uint16 limitBps)
        external
        returns (uint256, uint256, uint256)
    {
        // PoolManager keeps pools in a mapping at slot 6; slot0's low 160 bits are the sqrt price.
        bytes32 id = keccak256(abi.encode(key));
        uint160 sqrtP = uint160(uint256(IExtsload(manager).extsload(keccak256(abi.encode(id, uint256(6))))));
        uint160 limit = _limit(sqrtP, zeroForOne, limitBps);
        address tokenIn = zeroForOne ? key.currency0 : key.currency1;
        address tokenOut = zeroForOne ? key.currency1 : key.currency0;
        uint256 in0 = _balance(tokenIn);
        uint256 out0 = _balance(tokenOut);
        uint256 gas0 = gasleft();
        IV4PoolManager(manager).unlock(abi.encode(manager, key, zeroForOne, amountIn, limit));
        return _result(tokenIn, tokenOut, in0, out0, gas0);
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        (address manager, V4Key memory key, bool zeroForOne, uint256 amountIn, uint160 limit) =
            abi.decode(data, (address, V4Key, bool, uint256, uint160));
        int256 delta = IV4PoolManager(manager).swap(key, V4SwapParams(zeroForOne, -int256(amountIn), limit), "");
        _settleTake(manager, key.currency0, key.currency1, zeroForOne, delta, false);
        return "";
    }

    // PancakeSwap Infinity CL: Vault.lock -> lockAcquired -> CLPoolManager.swap, then vault sync, transfer, settle, take.
    function infSwap(address vault, InfKey calldata key, bool zeroForOne, uint256 amountIn, uint16 limitBps)
        external
        returns (uint256, uint256, uint256)
    {
        uint160 sqrtP;
        if (limitBps != 0) (sqrtP,,,) = IInfCLPoolManager(key.poolManager).getSlot0(keccak256(abi.encode(key)));
        uint160 limit = _limit(sqrtP, zeroForOne, limitBps);
        address tokenIn = zeroForOne ? key.currency0 : key.currency1;
        address tokenOut = zeroForOne ? key.currency1 : key.currency0;
        uint256 in0 = _balance(tokenIn);
        uint256 out0 = _balance(tokenOut);
        uint256 gas0 = gasleft();
        IInfVault(vault).lock(abi.encode(vault, key, zeroForOne, amountIn, limit));
        return _result(tokenIn, tokenOut, in0, out0, gas0);
    }

    function lockAcquired(bytes calldata data) external returns (bytes memory) {
        (address vault, InfKey memory key, bool zeroForOne, uint256 amountIn, uint160 limit) =
            abi.decode(data, (address, InfKey, bool, uint256, uint160));
        int256 delta = IInfCLPoolManager(key.poolManager).swap(key, V4SwapParams(zeroForOne, -int256(amountIn), limit), "");
        _settleTake(vault, key.currency0, key.currency1, zeroForOne, delta, true);
        return "";
    }

    // BalanceDelta packs amount0 in the upper and amount1 in the lower 128 bits; negative means owed by us.
    function _settleTake(address manager, address c0, address c1, bool zeroForOne, int256 delta, bool inf) internal {
        int128 d0 = int128(delta >> 128);
        int128 d1 = int128(delta);
        (address tokenIn, address tokenOut, int128 dIn, int128 dOut) =
            zeroForOne ? (c0, c1, d0, d1) : (c1, c0, d1, d0);
        if (dIn < 0) {
            if (inf) IInfVault(manager).sync(tokenIn);
            else IV4PoolManager(manager).sync(tokenIn);
            _transfer(tokenIn, manager, uint256(uint128(-dIn)));
            if (inf) IInfVault(manager).settle();
            else IV4PoolManager(manager).settle();
        }
        if (dOut > 0) {
            if (inf) IInfVault(manager).take(tokenOut, address(this), uint256(uint128(dOut)));
            else IV4PoolManager(manager).take(tokenOut, address(this), uint256(uint128(dOut)));
        }
    }
}
