#!/usr/bin/env python3
"""USDP back-runner: buy USDP that was dumped below $1 into Ethereum pools and sell it to Stable.com 1:1.

After every block it reads all watched pools with one Multicall3 call, sizes a trade to each dip (Stable.com
needs at least 1,000 USDP per order), and executes through the existing flash-loan executor via USDPTestEngine,
so no capital is needed beyond gas.

Venues:
  - Curve FRAX/USDP, reached as USDC -> FRAX (FRAXBP) -> USDP through the Curve Router NG
  - Curve USDP/3Crv metapool (exchange_underlying USDC -> USDP)
  - Curve old PAX pool (exchange_underlying USDC -> USDP)
  - Curve crvUSD/USDP, reached as USDC -> crvUSD -> USDP through the Curve Router NG
  - Uniswap V4 USDP pools (detected on-chain; executed with the engine's aggregator quote)

Modes:
  - volume (default): the largest trade that still clears --min-profit-usd after gas (most USDP)
  - profit: the trade with the highest net profit

Actions: watch only (default), --dry-run (Stable.com order + on-chain pre-flight, never broadcasts),
--live --confirm-live LIVE (broadcasts).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from src.engines.usdp_arb_engine import (
    ArbEvaluation,
    DexQuote,
    STABLE_POOL_ADDRESS,
    USDC_ADDRESS,
    USDC_DECIMALS,
    USDP_ADDRESS,
    USDP_DECIMALS,
    USDPTestEngine,
    format_execution_report,
)

USDT_ADDRESS = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
FRAX_ADDRESS = "0x853d955aCEf822Db058eb8505911ED77F175b99e"
CRVUSD_ADDRESS = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
FRAXBP_POOL = "0xDcEF968d416a41Cdac0ED8702fAC8128A64241A2"
FRAX_USDP_POOL = "0xaE34574AC03A15cd58A92DC79De7B1A0800F1CE3"
CRVUSD_USDC_POOL = "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E"
CRVUSD_USDP_POOL = "0xCa978A0528116DDA3cbA9ACD3e68bc6191CA53D0"
USDP_3CRV_POOL = "0xc270b3B858c335B6BA5D5b10e2Da8a09976005ad"
PAX_POOL = "0x06364f10B501e868329afBc005b3492902d6C763"
CURVE_ROUTER_NG = "0x45312ea0eFf7E09C83CBE249fa1d7598c4C8cd4e"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
V4_QUOTER = "0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203"
STATE_VIEW = "0x7fFE42C4a5DEeA5b0feC41C94C136Cf115597227"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
V4_QUOTE_GAS = 3_000_000

STABLE_MIN_ORDER_USDP = 1_000
STABLE_MAX_ORDER_USDP = 1_000_000
USDP_PER_USDC_RAW = 10 ** (USDP_DECIMALS - USDC_DECIMALS)  # 1 USDC raw unit expressed in USDP raw units
LADDER_USDC = (1_000, 1_500, 2_000, 3_000, 4_000, 5_500, 7_000, 9_000, 12_000, 16_000, 20_000, 30_000, 45_000, 60_000)
REFINE_POINTS = 6
# Flash loan + Stable.com leg + approvals, added on top of each venue's swap gas.
EXECUTOR_OVERHEAD_GAS = 200_000


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _uint(result: bytes | None) -> int | None:
    return int.from_bytes(result[:32], "big") if result else None


@dataclass(frozen=True)
class Venue:
    """A pool (or Curve Router NG path) that sells USDP for USDC in a single executor call."""

    name: str
    target: str
    swap_gas: int
    route: tuple[str, ...] = ()
    swap_params: tuple[tuple[int, ...], ...] = ()
    underlying_ij: tuple[int, int] | None = None

    def quote_calldata(self, amount_raw: int) -> bytes:
        if self.underlying_ij is not None:
            i, j = self.underlying_ij
            return _selector("get_dy_underlying(int128,int128,uint256)") + encode(
                ["int128", "int128", "uint256"], [i, j, amount_raw]
            )
        return _selector("get_dy(address[11],uint256[5][5],uint256,address[5])") + encode(
            ["address[11]", "uint256[5][5]", "uint256", "address[5]"],
            [list(self.route), [list(p) for p in self.swap_params], amount_raw, [ZERO_ADDRESS] * 5],
        )

    def swap_calldata(self, amount_raw: int, min_out_raw: int, receiver: str) -> bytes:
        if self.underlying_ij is not None:
            i, j = self.underlying_ij
            return _selector("exchange_underlying(int128,int128,uint256,uint256)") + encode(
                ["int128", "int128", "uint256", "uint256"], [i, j, amount_raw, min_out_raw]
            )
        return _selector("exchange(address[11],uint256[5][5],uint256,uint256,address[5],address)") + encode(
            ["address[11]", "uint256[5][5]", "uint256", "uint256", "address[5]", "address"],
            [
                list(self.route),
                [list(p) for p in self.swap_params],
                amount_raw,
                min_out_raw,
                [ZERO_ADDRESS] * 5,
                to_checksum_address(receiver),
            ],
        )


def curve_router_venue(name: str, hops: Sequence[tuple[str, str, str, int, int]], swap_gas: int) -> Venue:
    """Build a Curve Router NG path. Each hop is (token_in, pool, token_out, i, j) on a plain stableswap pool."""
    route = [ZERO_ADDRESS] * 11
    params = [(0, 0, 0, 0, 0)] * 5
    route[0] = hops[0][0]
    for k, (_, pool, token_out, i, j) in enumerate(hops):
        route[2 * k + 1] = pool
        route[2 * k + 2] = token_out
        params[k] = (i, j, 1, 1, 2)  # swap_type 1 = exchange, pool_type 1 = stableswap, 2 coins
    return Venue(
        name=name,
        target=CURVE_ROUTER_NG,
        swap_gas=swap_gas,
        route=tuple(to_checksum_address(a) for a in route),
        swap_params=tuple(params),
    )


DEFAULT_VENUES: tuple[Venue, ...] = (
    curve_router_venue(
        "curve-frax",
        [(USDC_ADDRESS, FRAXBP_POOL, FRAX_ADDRESS, 1, 0), (FRAX_ADDRESS, FRAX_USDP_POOL, USDP_ADDRESS, 0, 1)],
        swap_gas=300_000,
    ),
    Venue(name="curve-3crv", target=USDP_3CRV_POOL, swap_gas=350_000, underlying_ij=(2, 0)),
    Venue(name="curve-pax", target=PAX_POOL, swap_gas=450_000, underlying_ij=(1, 3)),
    curve_router_venue(
        "curve-crvusd",
        [(USDC_ADDRESS, CRVUSD_USDC_POOL, CRVUSD_ADDRESS, 0, 1), (CRVUSD_ADDRESS, CRVUSD_USDP_POOL, USDP_ADDRESS, 1, 0)],
        swap_gas=300_000,
    ),
)


@dataclass(frozen=True)
class V4Pool:
    """A Uniswap V4 USDP pool. USDP sorts as currency0 against USDC and USDT."""

    name: str
    quote_token: str
    fee: int
    tick_spacing: int
    hooks: str = ZERO_ADDRESS
    swap_gas: int = 450_000

    @property
    def key(self) -> tuple[str, str, int, int, str]:
        return (USDP_ADDRESS, self.quote_token, self.fee, self.tick_spacing, self.hooks)

    @property
    def pool_id(self) -> bytes:
        return keccak(encode(["address", "address", "uint24", "int24", "address"], list(self.key)))

    def liquidity_calldata(self) -> bytes:
        return _selector("getLiquidity(bytes32)") + encode(["bytes32"], [self.pool_id])

    def quote_calldata(self, amount_raw: int) -> bytes:
        return _selector("quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))") + encode(
            ["((address,address,uint24,int24,address),bool,uint128,bytes)"], [(self.key, False, amount_raw, b"")]
        )


# The V4 market maker moves between these pools every few days; empty pools simply fail to quote.
DEFAULT_V4_POOLS: tuple[V4Pool, ...] = (
    V4Pool("v4-usdt-0.10%", USDT_ADDRESS, 1000, 10),
    V4Pool("v4-usdt-0.0075%", USDT_ADDRESS, 75, 1),
    V4Pool("v4-usdc-0.0075%", USDC_ADDRESS, 75, 1),
    V4Pool("v4-usdt-0.0085%", USDT_ADDRESS, 85, 1),
)


@dataclass
class Opportunity:
    venue: str
    block: int
    amount_in_raw: int
    usdp_out_raw: int
    gas_usd: Decimal
    ladder: list[tuple[int, int]] = field(default_factory=list)

    @property
    def amount_in_usdc(self) -> Decimal:
        return Decimal(self.amount_in_raw) / Decimal(10**USDC_DECIMALS)

    @property
    def usdp_out(self) -> Decimal:
        return Decimal(self.usdp_out_raw) / Decimal(10**USDP_DECIMALS)

    @property
    def gross_profit_usd(self) -> Decimal:
        return self.usdp_out - self.amount_in_usdc

    @property
    def net_profit_usd(self) -> Decimal:
        return self.gross_profit_usd - self.gas_usd


def choose_size(
    ladder: Iterable[tuple[int, int | None]],
    mode: str,
    min_out_raw: int,
    max_out_raw: int,
    min_profit_raw: int,
) -> tuple[int, int] | None:
    """Pick (usdc_in_raw, usdp_out_raw) from quoted points.

    Profit is valued with USDP at exactly 1 USDC (the Stable.com exit) and measured in USDC raw units.
    volume = largest input that still makes min_profit_raw; profit = highest profit.
    """
    ok = []
    for amount_in, out in ladder:
        if out is None or out < min_out_raw or out > max_out_raw:
            continue
        profit = out // USDP_PER_USDC_RAW - amount_in
        if profit >= min_profit_raw:
            ok.append((amount_in, out, profit))
    if not ok:
        return None
    if mode == "profit":
        best = max(ok, key=lambda p: (p[2], -p[0]))
    elif mode == "volume":
        best = max(ok, key=lambda p: p[0])
    else:
        raise ValueError(f"unknown mode {mode!r}")
    return best[0], best[1]


def refine_sizes(ladder_in: Sequence[int], chosen_in: int, mode: str, points: int = REFINE_POINTS) -> list[int]:
    """Sizes between the chosen ladder point and its neighbours, to sharpen the coarse ladder pick."""
    sizes = sorted(ladder_in)
    idx = sizes.index(chosen_in)
    lo = sizes[idx - 1] if (mode == "profit" and idx > 0) else chosen_in
    hi = sizes[idx + 1] if idx + 1 < len(sizes) else chosen_in
    if hi <= lo:
        return []
    step = (hi - lo) / (points + 1)
    return sorted({int(lo + step * k) for k in range(1, points + 1)} - set(sizes))


class USDPBackrunner:
    def __init__(
        self,
        engine: USDPTestEngine | None = None,
        mode: str = "volume",
        min_profit_usd: Decimal = Decimal("0.50"),
        buffer_bps: int = 1,
        venues: Sequence[Venue] = DEFAULT_VENUES,
        v4_pools: Sequence[V4Pool] = DEFAULT_V4_POOLS,
        ladder_usdc: Sequence[int] = LADDER_USDC,
        aggregator: str | None = None,
    ):
        if mode not in ("volume", "profit"):
            raise ValueError("mode must be 'volume' or 'profit'")
        self.engine = engine or USDPTestEngine()
        self.mode = mode
        self.min_profit_usd = min_profit_usd
        self.buffer_bps = buffer_bps
        self.venues = {v.name: v for v in venues}
        self.v4_pools = {p.name: p for p in v4_pools}
        self.ladder_raw = [int(Decimal(a) * Decimal(10**USDC_DECIMALS)) for a in ladder_usdc]
        self.aggregator = aggregator or os.getenv("ETH_USDP_AGGREGATOR") or "matcha"
        self._w3 = None
        self._gas_cache: tuple[float, Decimal, Decimal] | None = None

    # ---- chain access -------------------------------------------------------------------------------
    @property
    def w3(self) -> Any:
        if self._w3 is None:
            self._w3 = self.engine.get_working_web3()
        return self._w3

    def multicall(self, calls: Sequence[tuple[str, bytes]], block: int | str = "latest") -> list[bytes | None]:
        """Run calls through Multicall3.aggregate3 with allowFailure; failed calls come back as None."""
        payload = _selector("aggregate3((address,bool,bytes)[])") + encode(
            ["(address,bool,bytes)[]"], [[(to_checksum_address(t), True, d) for t, d in calls]]
        )
        raw = self.w3.eth.call({"to": to_checksum_address(MULTICALL3), "data": payload}, block_identifier=block)
        results = decode(["(bool,bytes)[]"], bytes(raw))[0]
        return [data if ok and len(data) >= 32 else None for ok, data in results]

    def gas_usd(self, gas_units: int) -> Decimal:
        now = time.time()
        if self._gas_cache is None or now - self._gas_cache[0] > 60:
            self._gas_cache = (now, self.engine.get_gas_price_gwei(), self.engine.get_eth_usd())
        _, gwei, eth_usd = self._gas_cache
        return Decimal(gas_units) * gwei / Decimal(10**9) * eth_usd

    # ---- scanning -----------------------------------------------------------------------------------
    def _v4_ladder(self, pool: V4Pool, sizes: Sequence[int], block: int | str) -> list[tuple[int, int | None]]:
        """Quote a V4 pool one capped eth_call per size, stopping at the first failure.

        A V4 quote that runs past the pool's liquidity walks the whole tick range and can burn hundreds of millions
        of gas, so these never share a multicall with the other venues. USDT and USDC both have 6 decimals; quoting
        USDT 1:1 slightly understates the dip when USDT trades under USDC, and the aggregator re-quotes before sending.
        """
        out: list[tuple[int, int | None]] = []
        for size in sizes:
            try:
                raw = self.w3.eth.call(
                    {"to": to_checksum_address(V4_QUOTER), "data": pool.quote_calldata(size), "gas": V4_QUOTE_GAS},
                    block_identifier=block,
                )
            except Exception:
                break
            out.append((size, _uint(bytes(raw))))
        return out

    def _quote_ladder(self, name: str, sizes: Sequence[int], block: int | str) -> list[tuple[int, int | None]]:
        if name in self.v4_pools:
            return self._v4_ladder(self.v4_pools[name], sizes, block)
        venue = self.venues[name]
        res = self.multicall([(venue.target, venue.quote_calldata(s)) for s in sizes], block)
        return [(s, _uint(r)) for s, r in zip(sizes, res)]

    def scan(self, block: int | str = "latest") -> list[Opportunity]:
        """Quote every venue on the ladder at `block` and return sized opportunities, best first."""
        if block == "latest":
            block = int(self.w3.eth.block_number)
        curve = list(self.venues)
        steps = len(self.ladder_raw)
        head = [(self.venues[n].target, self.venues[n].quote_calldata(s)) for n in curve for s in self.ladder_raw]
        head.append((USDC_ADDRESS, _selector("balanceOf(address)") + encode(["address"], [STABLE_POOL_ADDRESS])))
        head += [(STATE_VIEW, pool.liquidity_calldata()) for pool in self.v4_pools.values()]
        results = self.multicall(head, block)

        ladders: dict[str, list[tuple[int, int | None]]] = {}
        for k, name in enumerate(curve):
            ladders[name] = [(s, _uint(r)) for s, r in zip(self.ladder_raw, results[k * steps:(k + 1) * steps])]
        stable_usdc = _uint(results[len(curve) * steps]) or 0
        for (name, pool), liquidity in zip(self.v4_pools.items(), results[len(curve) * steps + 1:]):
            if not _uint(liquidity):
                continue  # nothing to buy at the current price
            probe = self._v4_ladder(pool, self.ladder_raw[:1], block)
            if not probe or probe[0][1] is None or probe[0][1] // USDP_PER_USDC_RAW <= probe[0][0]:
                continue
            ladders[name] = probe + self._v4_ladder(pool, self.ladder_raw[1:], block)

        max_out = min(stable_usdc * USDP_PER_USDC_RAW, STABLE_MAX_ORDER_USDP * 10**USDP_DECIMALS)
        # The Stable.com order is the swap output minus the buffer, and that is what has to clear the minimum.
        min_out = -(-STABLE_MIN_ORDER_USDP * 10**USDP_DECIMALS * 10_000 // (10_000 - self.buffer_bps))
        opportunities = []
        for name in ladders:
            gas_units = (self.venues[name].swap_gas if name in self.venues else self.v4_pools[name].swap_gas)
            gas_units += EXECUTOR_OVERHEAD_GAS
            gas_usd = self.gas_usd(gas_units)
            min_profit_raw = int((self.min_profit_usd + gas_usd) * Decimal(10**USDC_DECIMALS))
            pick = choose_size(ladders[name], self.mode, min_out, max_out, min_profit_raw)
            if pick is None:
                continue
            extra = refine_sizes(self.ladder_raw, pick[0], self.mode)
            if extra:
                ladders[name] += self._quote_ladder(name, extra, block)
                pick = choose_size(ladders[name], self.mode, min_out, max_out, min_profit_raw) or pick
            opportunities.append(
                Opportunity(
                    venue=name,
                    block=int(block),
                    amount_in_raw=pick[0],
                    usdp_out_raw=pick[1],
                    gas_usd=gas_usd,
                    ladder=sorted((s, o) for s, o in ladders[name] if o is not None),
                )
            )
        return sorted(opportunities, key=lambda o: o.net_profit_usd, reverse=True)

    # ---- execution ----------------------------------------------------------------------------------
    def build_evaluation(self, opp: Opportunity) -> ArbEvaluation:
        """Turn an opportunity into the engine's ArbEvaluation (swap calldata + Stable.com quote)."""
        if opp.venue in self.v4_pools:
            # V4 trades are routed by the engine's aggregator chain (meta.matcha first) at the detected size.
            return self.engine.evaluate_arbitrage(amount_usdc=opp.amount_in_usdc, aggregator=self.aggregator)
        venue = self.venues[opp.venue]
        min_out = opp.usdp_out_raw * (10_000 - self.buffer_bps) // 10_000
        data = venue.swap_calldata(opp.amount_in_raw, min_out, self.engine.executor)
        dex_quote = DexQuote(
            aggregator=f"onchain:{venue.name}",
            target=to_checksum_address(venue.target),
            allowance_target=to_checksum_address(venue.target),
            sell_amount_raw=opp.amount_in_raw,
            buy_amount_raw=opp.usdp_out_raw,
            buy_amount_human=opp.usdp_out,
            data="0x" + data.hex(),
            value=0,
            gas_estimate=venue.swap_gas,
        )
        stable_quote = self.engine.quote_stable(opp.usdp_out)
        gross = stable_quote.amount_out_human - opp.amount_in_usdc
        net = gross - opp.gas_usd - stable_quote.native_fee_usd
        return ArbEvaluation(
            loan_amount_usdc=opp.amount_in_usdc,
            dex_quote=dex_quote,
            stable_quote=stable_quote,
            gross_profit_usdc=gross,
            gas_estimate_usd=opp.gas_usd,
            net_profit_usd=net,
            is_profitable=net >= self.min_profit_usd,
        )

    def execute(self, opp: Opportunity, live: bool, **exec_kwargs: Any) -> Any:
        """Re-quote, then dry-run or broadcast through the flash-loan executor. None if the re-quote fell short."""
        evaluation = self.build_evaluation(opp)
        if evaluation.net_profit_usd < self.min_profit_usd:
            return None
        return self.engine.execute_live_arbitrage(
            evaluation=evaluation,
            min_profit_usdc=self.min_profit_usd,
            leg1_buffer_bps=self.buffer_bps,
            dry_run=not live,
            **exec_kwargs,
        )


def format_opportunity(opp: Opportunity) -> str:
    avg = opp.amount_in_usdc / opp.usdp_out if opp.usdp_out else Decimal(0)
    return (
        f"[block {opp.block}] {opp.venue:<17} buy {opp.usdp_out:>11,.2f} USDP for {opp.amount_in_usdc:>10,.2f} USDC "
        f"(avg {avg:.5f}) | gross +${opp.gross_profit_usd:,.2f} gas ${opp.gas_usd:,.2f} net +${opp.net_profit_usd:,.2f}"
    )


def _opportunity_json(opp: Opportunity) -> dict[str, Any]:
    return {
        "venue": opp.venue,
        "block": opp.block,
        "usdcIn": str(opp.amount_in_usdc),
        "usdpOut": str(opp.usdp_out),
        "grossProfitUsd": str(opp.gross_profit_usd),
        "gasUsd": str(opp.gas_usd),
        "netProfitUsd": str(opp.net_profit_usd),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="USDP back-runner: buy dumped USDP below $1, sell it to Stable.com 1:1.")
    parser.add_argument("--mode", choices=("volume", "profit"), default=os.getenv("ETH_USDP_BACKRUN_MODE", "volume"),
                        help="volume = most USDP that still clears --min-profit-usd; profit = highest net profit")
    parser.add_argument("--min-profit-usd", type=Decimal, default=Decimal(os.getenv("ETH_USDP_BACKRUN_MIN_PROFIT_USD", "0.50")),
                        help="Minimum net profit per trade after gas (USD)")
    parser.add_argument("--buffer-bps", type=int, default=int(os.getenv("ETH_USDP_BACKRUN_BUFFER_BPS", "1")),
                        help="USDP held back from the Stable.com order to absorb rounding (bps)")
    parser.add_argument("--venues", default="", help="Comma-separated venue names to watch (default: all)")
    parser.add_argument("--no-v4", action="store_true", help="Skip Uniswap V4 pools")
    parser.add_argument("--block", type=int, default=None, help="Scan one historical block (needs an archive RPC) and exit")
    parser.add_argument("--once", action="store_true", help="Scan the latest block once and exit")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between new-block checks")
    parser.add_argument("--dry-run", action="store_true", help="Create the Stable.com order and run the on-chain pre-flight, never broadcast")
    parser.add_argument("--live", action="store_true", help="Broadcast trades (requires --confirm-live LIVE)")
    parser.add_argument("--confirm-live", default="", help="Must be LIVE to broadcast")
    parser.add_argument("--executor", default=None, help="Executor contract override")
    parser.add_argument("--gas-multiplier", type=Decimal, default=Decimal("1.15"))
    parser.add_argument("--max-fee-gwei", type=Decimal, default=None)
    parser.add_argument("--priority-fee-gwei", type=Decimal, default=Decimal("0.05"))
    parser.add_argument("--receipt-timeout", type=int, default=60)
    parser.add_argument("--json", action="store_true", help="Print opportunities as JSON lines")
    args = parser.parse_args(argv)

    if args.live and args.confirm_live.strip().upper() != "LIVE":
        print("Refusing to broadcast: pass --confirm-live LIVE together with --live.", file=sys.stderr)
        return 2

    engine = USDPTestEngine(executor=args.executor)
    venues = DEFAULT_VENUES
    v4_pools = () if args.no_v4 else DEFAULT_V4_POOLS
    if args.venues:
        wanted = {v.strip() for v in args.venues.split(",") if v.strip()}
        venues = tuple(v for v in venues if v.name in wanted)
        v4_pools = tuple(p for p in v4_pools if p.name in wanted)
    bot = USDPBackrunner(engine=engine, mode=args.mode, min_profit_usd=args.min_profit_usd,
                         buffer_bps=args.buffer_bps, venues=venues, v4_pools=v4_pools)
    action = "live" if args.live else ("dry-run" if args.dry_run else "watch")
    exec_kwargs = dict(gas_multiplier=args.gas_multiplier, max_fee_gwei=args.max_fee_gwei,
                       priority_fee_gwei=args.priority_fee_gwei, receipt_timeout=args.receipt_timeout)

    def handle(block: int | str) -> list[Opportunity]:
        opps = bot.scan(block)
        for opp in opps:
            print(json.dumps(_opportunity_json(opp)) if args.json else format_opportunity(opp), flush=True)
        if action != "watch" and opps and args.block is None:
            best = opps[0]
            result = bot.execute(best, live=(action == "live"), **exec_kwargs)
            if result is None:
                print(f"  skipped {best.venue}: re-quote no longer clears min profit", flush=True)
            else:
                print(format_execution_report(result), flush=True)
        return opps

    if args.block is not None:
        if not handle(args.block):
            print(f"[block {args.block}] no USDP below $1 that clears the minimums")
        return 0
    if args.once:
        if not handle("latest"):
            print("no USDP below $1 that clears the minimums right now")
        return 0

    print(f"USDP back-runner: action={action} mode={args.mode} min_profit=${args.min_profit_usd} "
          f"venues={','.join(list(bot.venues) + list(bot.v4_pools))}", flush=True)
    last_block = None
    try:
        while True:
            try:
                current = int(bot.w3.eth.block_number)
                if current != last_block:
                    last_block = current
                    handle(current)
            except Exception as exc:
                print(f"[!] scan error: {exc}", file=sys.stderr, flush=True)
                bot._w3 = None  # reconnect through the engine's RPC fallback list next round
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
