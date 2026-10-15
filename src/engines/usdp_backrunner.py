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

Float mode (--float-usdc N): dips too small for a 1,000 USDP Stable.com order are bought from the operator wallet
with up to N USDC of its own capital, on the same Curve routes. The USDP is held, and once the wallet has at least
1,000 USDP it is sold to Stable.com 1:1 for USDC (or the next --float-sell-assets payout token when Stable.com is
short of USDC). Inventory, cost and PnL are kept in a JSON state file. Uniswap V4 dips are not bought in float mode.

Actions: watch only (default), --dry-run (Stable.com order + on-chain pre-flight, never broadcasts; float trades
are paper-traded, pre-flighted with a simulated wallet balance where the real one is short),
--live --confirm-live LIVE (broadcasts).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Sequence
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from src.engines.usdp_arb_engine import (
    ArbEvaluation,
    DexQuote,
    STABLE_BASE_URL,
    STABLE_CHAIN_ID,
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

USDG_ADDRESS = "0xe343167631d89B6Ffc58B88d6b7fB0228795491D"
PYUSD_ADDRESS = "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8"
# Stable.com pays any of these 1:1 for USDP; all have 6 decimals like USDC.
STABLE_PAYOUT_TOKENS = {"USDC": USDC_ADDRESS, "USDG": USDG_ADDRESS, "PYUSD": PYUSD_ADDRESS, "USDT": USDT_ADDRESS}
FLOAT_LADDER_USDC = (100, 200, 300, 400, 550, 700, 850)
# Wallet -> Stable.com singleChainSwap. A float buy is charged its share, per 1,000 USDP, of this gas plus Stable.com's
# native execution fee per order (~$0.40 on 2026-10-15; replaced by the fee each sell quote reports).
STABLE_SELL_GAS = 150_000
STABLE_FEE_USD = Decimal("0.40")
# (balance mapping slot, allowance mapping slot) per token, so a dry-run pre-flight can hand the wallet a simulated
# balance and approval through eth_call state overrides.
TOKEN_STORAGE_SLOTS = {USDC_ADDRESS.lower(): (9, 10), USDP_ADDRESS.lower(): (1, 3)}
SINGLE_CHAIN_SWAP = "singleChainSwap((uint256,address,address,uint64,address,uint64,uint256),bytes,uint256)"
TRANSFER_TOPIC = keccak(text="Transfer(address,address,uint256)")
STABLE_HEADERS = {
    "origin": "https://stable.com",
    "referer": "https://stable.com/",
    "accept": "application/json",
    "content-type": "application/json",
}
FLOAT_HISTORY = 200
SELL_RETRY_SECONDS = 300


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _uint(result: bytes | None) -> int | None:
    return int.from_bytes(result[:32], "big") if result else None


def _balance_call(token: str, owner: str) -> tuple[str, bytes]:
    return token, _selector("balanceOf(address)") + encode(["address"], [to_checksum_address(owner)])


def _allowance_call(token: str, owner: str, spender: str) -> tuple[str, bytes]:
    return token, _selector("allowance(address,address)") + encode(
        ["address", "address"], [to_checksum_address(owner), to_checksum_address(spender)]
    )


def _approve_data(spender: str, amount: int) -> bytes:
    return _selector("approve(address,uint256)") + encode(["address", "uint256"], [to_checksum_address(spender), amount])


def token_overrides(token: str, owner: str, spender: str, balance: int, allowance: int) -> dict[str, Any]:
    """eth_call state override giving `owner` a token balance and an allowance for `spender`."""
    bal_slot, allow_slot = TOKEN_STORAGE_SLOTS[token.lower()]
    owner, spender = to_checksum_address(owner), to_checksum_address(spender)
    bal_key = keccak(encode(["address", "uint256"], [owner, bal_slot]))
    allow_key = keccak(encode(["address", "bytes32"], [spender, keccak(encode(["address", "uint256"], [owner, allow_slot]))]))
    return {
        to_checksum_address(token): {
            "stateDiff": {
                "0x" + bal_key.hex(): "0x" + balance.to_bytes(32, "big").hex(),
                "0x" + allow_key.hex(): "0x" + allowance.to_bytes(32, "big").hex(),
            }
        }
    }


def received_from_logs(receipt: Any, token: str, owner: str) -> int:
    """Sum of `token` Transfer events to `owner` in a receipt."""
    owner_word = bytes(12) + bytes.fromhex(owner[2:])
    total = 0
    for log in receipt["logs"]:
        topics = [bytes(t) for t in log["topics"]]
        if (str(log["address"]).lower() == token.lower() and len(topics) == 3
                and topics[0] == TRANSFER_TOPIC and topics[2] == owner_word):
            total += int.from_bytes(bytes(log["data"]), "big")
    return total


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
    kind: str = "flash"  # flash = flash-loan executor + Stable.com in one tx; float = bought with the wallet float

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


def choose_payout(assets: Sequence[str], room_raw: dict[str, int], usdp_raw: int) -> tuple[str, int] | None:
    """Pick the Stable.com payout token for selling `usdp_raw` USDP.

    room_raw is each token's balance in the Stable.com pool (6 decimals). The first token in preference order that
    can pay the whole order wins; otherwise the token with the most room takes a partial order, if that still
    reaches Stable.com's minimum. Returns (token symbol, USDP raw amount to sell).
    """
    for asset in assets:
        if room_raw.get(asset, 0) * USDP_PER_USDC_RAW >= usdp_raw:
            return asset, usdp_raw
    best = max(assets, key=lambda a: room_raw.get(a, 0))
    partial = room_raw.get(best, 0) * USDP_PER_USDC_RAW
    if partial >= STABLE_MIN_ORDER_USDP * 10**USDP_DECIMALS:
        return best, partial
    return None


@dataclass
class FloatInventory:
    """USDP bought with the float and not yet sold to Stable.com, with running totals. Saved as JSON."""

    usdp_raw: int = 0
    cost_raw: int = 0  # USDC paid for usdp_raw
    gas_usd: Decimal = Decimal(0)  # buy gas spent on usdp_raw, realized when it is sold
    realized_pnl_usd: Decimal = Decimal(0)
    bought_usdp_raw: int = 0
    sold_usdp_raw: int = 0
    buys: int = 0
    sells: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def usdp(self) -> Decimal:
        return Decimal(self.usdp_raw) / Decimal(10**USDP_DECIMALS)

    @property
    def cost_usdc(self) -> Decimal:
        return Decimal(self.cost_raw) / Decimal(10**USDC_DECIMALS)

    @property
    def unrealized_pnl_usd(self) -> Decimal:
        return self.usdp - self.cost_usdc - self.gas_usd

    def _log(self, entry: dict[str, Any]) -> None:
        entry = {k: str(v.quantize(Decimal("0.000001"))) if isinstance(v, Decimal) else v for k, v in entry.items()}
        self.history = (self.history + [entry])[-FLOAT_HISTORY:]

    def record_buy(self, usdc_in_raw: int, usdp_out_raw: int, gas_usd: Decimal, **meta: Any) -> None:
        self.usdp_raw += usdp_out_raw
        self.cost_raw += usdc_in_raw
        self.gas_usd += gas_usd
        self.bought_usdp_raw += usdp_out_raw
        self.buys += 1
        self._log({"action": "buy", "time": int(time.time()), "usdcIn": usdc_in_raw, "usdpOut": usdp_out_raw,
                   "gasUsd": gas_usd, **meta})

    def record_sell(self, usdp_in_raw: int, proceeds_raw: int, gas_usd: Decimal, fee_usd: Decimal, **meta: Any) -> Decimal:
        """Book a Stable.com sale and return its realized PnL (proceeds - cost - buy gas - sell gas - fee).

        USDP the bot did not buy itself (already in the wallet) is booked at cost 1:1, so it adds no PnL.
        """
        tracked = min(usdp_in_raw, self.usdp_raw)
        cost = self.cost_raw * tracked // self.usdp_raw if self.usdp_raw else 0
        buy_gas = self.gas_usd * tracked / self.usdp_raw if self.usdp_raw else Decimal(0)
        untracked_cost = (usdp_in_raw - tracked) // USDP_PER_USDC_RAW
        pnl = Decimal(proceeds_raw - cost - untracked_cost) / Decimal(10**USDC_DECIMALS) - buy_gas - gas_usd - fee_usd
        self.usdp_raw -= tracked
        self.cost_raw -= cost
        self.gas_usd -= buy_gas
        self.realized_pnl_usd += pnl
        self.sold_usdp_raw += usdp_in_raw
        self.sells += 1
        self._log({"action": "sell", "time": int(time.time()), "usdpIn": usdp_in_raw, "proceeds": proceeds_raw,
                   "gasUsd": gas_usd, "feeUsd": fee_usd, "pnlUsd": pnl, **meta})
        return pnl

    def record_cost(self, gas_usd: Decimal, action: str, **meta: Any) -> None:
        """Gas that buys no USDP (an approval, or a transaction that reverted), realized straight away."""
        self.realized_pnl_usd -= gas_usd
        self._log({"action": action, "time": int(time.time()), "gasUsd": gas_usd, **meta})

    def to_json(self) -> dict[str, Any]:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FloatInventory":
        inv = cls()
        for name, default in asdict(inv).items():
            if name in data:
                setattr(inv, name, type(default)(data[name]) if not isinstance(default, list) else list(data[name]))
        return inv

    @classmethod
    def load(cls, path: Path | None) -> "FloatInventory":
        if path is None or not path.exists():
            return cls()
        return cls.from_json(json.loads(path.read_text()))

    def save(self, path: Path | None) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2))
        tmp.replace(path)


@dataclass
class FloatResult:
    action: str  # buy | sell
    mode: str  # dry-run | live
    target: str  # venue for a buy, payout token for a sell
    usdp_raw: int
    usdc_raw: int  # paid for a buy, received for a sell (6 decimals)
    success: bool = True
    gas_usd: Decimal = Decimal(0)
    fee_usd: Decimal = Decimal(0)
    pnl_usd: Decimal | None = None
    virtual: bool = False  # the pre-flight gave the wallet a simulated balance/approval (state override)
    tx_hashes: list[str] = field(default_factory=list)
    error: str | None = None

    def fail(self, error: str) -> "FloatResult":
        self.success, self.error = False, error
        return self


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
        float_usdc: Decimal = Decimal(0),
        float_min_profit_usd: Decimal = Decimal("0.10"),
        float_wallet: str | None = None,
        float_sell_assets: Sequence[str] = ("USDC",),
        float_state_path: str | Path | None = None,
        paper: bool = True,
        float_ladder_usdc: Sequence[int] = FLOAT_LADDER_USDC,
    ):
        """float_usdc > 0 turns on float mode. paper=True (watch / dry-run) trades a simulated float and inventory;
        paper=False sizes buys against the wallet's real USDC and sells the wallet's real USDP balance."""
        if mode not in ("volume", "profit"):
            raise ValueError("mode must be 'volume' or 'profit'")
        unknown = [a for a in float_sell_assets if a not in STABLE_PAYOUT_TOKENS]
        if unknown or not float_sell_assets:
            raise ValueError(f"float sell assets must be from {', '.join(STABLE_PAYOUT_TOKENS)}")
        self.engine = engine or USDPTestEngine()
        self.mode = mode
        self.min_profit_usd = min_profit_usd
        self.buffer_bps = buffer_bps
        self.venues = {v.name: v for v in venues}
        self.v4_pools = {p.name: p for p in v4_pools}
        self.float_cap_raw = int(Decimal(float_usdc) * Decimal(10**USDC_DECIMALS))
        if self.float_enabled:
            ladder_usdc = sorted(set(ladder_usdc) | set(float_ladder_usdc))
        self.ladder_raw = [int(Decimal(a) * Decimal(10**USDC_DECIMALS)) for a in ladder_usdc]
        self.aggregator = aggregator or os.getenv("ETH_USDP_AGGREGATOR") or "matcha"
        self.float_min_profit_usd = float_min_profit_usd
        self.float_wallet = to_checksum_address(float_wallet) if float_wallet else None
        self.float_sell_assets = tuple(float_sell_assets)
        self.float_state_path = Path(float_state_path) if float_state_path else None
        self.paper = paper
        self.inventory = FloatInventory.load(self.float_state_path)
        self.wallet_usdc_raw: int | None = None  # read with every scan while float mode is on
        self.wallet_usdp_raw: int | None = None
        self._paper_buys: dict[str, tuple[int, int]] = {}
        self._sell_note: str | None = None
        self._sell_retry_at = 0.0
        self.stable_fee_usd = STABLE_FEE_USD
        self._w3 = None
        self._gas_cache: tuple[float, Decimal, Decimal] | None = None

    @property
    def float_enabled(self) -> bool:
        return self.float_cap_raw > 0

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
        head.append(_balance_call(USDC_ADDRESS, STABLE_POOL_ADDRESS))
        wallet_calls = []
        if self.float_enabled and self.float_wallet:
            wallet_calls = [_balance_call(USDC_ADDRESS, self.float_wallet), _balance_call(USDP_ADDRESS, self.float_wallet)]
        head += wallet_calls
        head += [(STATE_VIEW, pool.liquidity_calldata()) for pool in self.v4_pools.values()]
        results = self.multicall(head, block)

        ladders: dict[str, list[tuple[int, int | None]]] = {}
        for k, name in enumerate(curve):
            ladders[name] = [(s, _uint(r)) for s, r in zip(self.ladder_raw, results[k * steps:(k + 1) * steps])]
        base = len(curve) * steps
        stable_usdc = _uint(results[base]) or 0
        if wallet_calls:
            self.wallet_usdc_raw, self.wallet_usdp_raw = (_uint(r) or 0 for r in results[base + 1:base + 3])
        for (name, pool), liquidity in zip(self.v4_pools.items(), results[base + 1 + len(wallet_calls):]):
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
        budget = self.float_budget_raw() if self.float_enabled else 0
        for name in ladders:
            gas_units = (self.venues[name].swap_gas if name in self.venues else self.v4_pools[name].swap_gas)
            gas_units += EXECUTOR_OVERHEAD_GAS
            gas_usd = self.gas_usd(gas_units)
            min_profit_raw = int((self.min_profit_usd + gas_usd) * Decimal(10**USDC_DECIMALS))
            pick = choose_size(ladders[name], self.mode, min_out, max_out, min_profit_raw)
            if pick is None:
                if name in self.venues and budget >= min(self.ladder_raw):
                    float_opp = self._float_opportunity(name, ladders[name], int(block), budget)
                    if float_opp is not None:
                        opportunities.append(float_opp)
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

    def float_budget_raw(self) -> int:
        """USDC the float can still spend: the cap minus what the unsold USDP cost (and, live, the wallet's USDC)."""
        room = max(0, self.float_cap_raw - self.inventory.cost_raw)
        if not self.paper:
            room = min(room, self.wallet_usdc_raw or 0)
        return room

    def _float_opportunity(
        self, name: str, ladder: list[tuple[int, int | None]], block: int, budget: int
    ) -> Opportunity | None:
        """Size a dip that is too small for the flash path against the float budget.

        Costs are the wallet swap's gas plus this buy's share (per 1,000 USDP) of the later Stable.com sell: its gas
        and Stable.com's per-order fee. The share grows with size, so each ladder point is sized on the USDP left
        after it.
        """
        venue = self.venues[name]
        buy_gas = self.gas_usd(venue.swap_gas)
        sell_cost_per_usdp = (self.gas_usd(STABLE_SELL_GAS) + self.stable_fee_usd) / Decimal(STABLE_MIN_ORDER_USDP)
        min_profit_raw = int((self.float_min_profit_usd + buy_gas) * Decimal(10**USDC_DECIMALS))
        any_out = STABLE_MAX_ORDER_USDP * 10**USDP_DECIMALS

        def pick_from(points: list[tuple[int, int | None]]) -> tuple[int, int] | None:
            actual = {s: o for s, o in points if o is not None and s <= budget}
            kept = [(s, int(Decimal(o) * (1 - sell_cost_per_usdp))) for s, o in actual.items()]
            pick = choose_size(kept, self.mode, 1, any_out, min_profit_raw)
            return (pick[0], actual[pick[0]]) if pick else None

        pick = pick_from(ladder)
        if pick is None:
            return None
        extra = {s for s in refine_sizes(self.ladder_raw, pick[0], self.mode) if s <= budget}
        above = [s for s in self.ladder_raw if s > pick[0]]
        if above and pick[0] < budget < above[0]:
            extra.add(budget)  # the dip may run right up to the budget
        if extra:
            ladder = ladder + self._quote_ladder(name, sorted(extra), block)
            pick = pick_from(ladder) or pick
        sell_share = sell_cost_per_usdp * Decimal(pick[1]) / Decimal(10**USDP_DECIMALS)
        opp = Opportunity(
            venue=name,
            block=block,
            amount_in_raw=pick[0],
            usdp_out_raw=pick[1],
            gas_usd=buy_gas + sell_share,
            ladder=sorted((s, o) for s, o in ladder if o is not None),
            kind="float",
        )
        return opp if opp.net_profit_usd >= self.float_min_profit_usd else None

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

    # ---- float mode execution -----------------------------------------------------------------------
    def _wallet(self) -> str:
        if not self.float_wallet:
            raise RuntimeError("float mode needs a wallet: set ETH_OPERATOR_PRIVATE_KEY or ETH_OPERATOR_ADDRESS")
        return self.float_wallet

    def _send(
        self,
        tx: dict[str, Any],
        gas_multiplier: Decimal = Decimal("1.15"),
        max_fee_gwei: Decimal | None = None,
        priority_fee_gwei: Decimal = Decimal("0.05"),
        receipt_timeout: int = 60,
    ) -> tuple[str, Any, Decimal]:
        """Sign a wallet transaction with ETH_OPERATOR_PRIVATE_KEY, submit it the way the engine does (Flashbots
        unless ETH_ENABLE_FLASHBOTS=false) and wait for the receipt. Returns (tx hash, receipt, gas cost in USD)."""
        w3 = self.w3
        private_key = os.getenv("ETH_OPERATOR_PRIVATE_KEY", "").strip()
        account = w3.eth.account.from_key(private_key)
        if account.address.lower() != tx["from"].lower():
            raise RuntimeError(f"ETH_OPERATOR_PRIVATE_KEY signs for {account.address}, not the float wallet {tx['from']}")
        priority = int(w3.to_wei(priority_fee_gwei, "gwei"))
        if max_fee_gwei is not None:
            max_fee = int(w3.to_wei(max_fee_gwei, "gwei"))
        else:
            max_fee = int(w3.eth.get_block("latest")["baseFeePerGas"]) * 135 // 100 + priority
        signed = account.sign_transaction({
            **tx,
            "chainId": 1,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "gas": int(Decimal(w3.eth.estimate_gas(tx)) * gas_multiplier),
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "type": 2,
        })
        if os.getenv("ETH_ENABLE_FLASHBOTS", "true").strip().lower() not in ("false", "0", "no"):
            from src.engines.eth_flash_arb_pyusd_usdc import broadcast_flashbots_or_fallback

            tx_hash, _ = broadcast_flashbots_or_fallback(
                web3=w3, signed_tx=signed, private_key=private_key, rpc_url=self.engine.rpc_url,
                receipt_timeout=receipt_timeout,
            )
        else:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        tx_hex = tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex
        receipt = w3.eth.wait_for_transaction_receipt(tx_hex, timeout=receipt_timeout)
        gas_usd = Decimal(receipt["gasUsed"]) * Decimal(receipt["effectiveGasPrice"]) / Decimal(10**18) * self.engine.get_eth_usd()
        return tx_hex, receipt, gas_usd

    def _approve(self, token: str, spender: str, amount: int, result: FloatResult, **send_kwargs: Any) -> bool:
        """Live only: approve `spender`, adding the tx to `result` and booking its gas as a cost of its own (it serves
        later trades too). False if the approval reverted."""
        wallet = self._wallet()
        tx = {"from": wallet, "to": to_checksum_address(token), "value": 0, "data": "0x" + _approve_data(spender, amount).hex()}
        tx_hash, receipt, gas = self._send(tx, **send_kwargs)
        result.tx_hashes.append(tx_hash)
        ok = receipt["status"] == 1
        self.inventory.record_cost(gas, "approve" if ok else "reverted", tx=tx_hash, token=token, spender=spender)
        self.inventory.save(self.float_state_path)
        return ok

    def execute_float_buy(self, opp: Opportunity, live: bool, block: int | str = "latest", **send_kwargs: Any) -> FloatResult | None:
        """Buy a small dip with the wallet float.

        Dry-run pre-flights the swap from the wallet (simulating its USDC balance and approval where the real ones are
        short) and books it in the paper inventory. Live approves the venue if needed, sends the swap and books the
        USDP that arrived. Returns None when a paper buy would count the same, unchanged dip a second time.
        """
        mode = "live" if live else "dry-run"
        venue = self.venues[opp.venue]
        if not live and self._paper_buys.get(venue.name) == (opp.amount_in_raw, opp.usdp_out_raw):
            return None
        buy_gas = self.gas_usd(venue.swap_gas)
        result = FloatResult("buy", mode, venue.name, opp.usdp_out_raw, opp.amount_in_raw, gas_usd=buy_gas)
        try:
            wallet = self._wallet()
            # Never accept less USDP than the USDC paid, whatever the buffer.
            min_out = max(opp.usdp_out_raw * (10_000 - self.buffer_bps) // 10_000, opp.amount_in_raw * USDP_PER_USDC_RAW)
            tx = {"from": wallet, "to": to_checksum_address(venue.target), "value": 0,
                  "data": "0x" + venue.swap_calldata(opp.amount_in_raw, min_out, wallet).hex()}
            balance, allowance = (_uint(r) or 0 for r in self.multicall(
                [_balance_call(USDC_ADDRESS, wallet), _allowance_call(USDC_ADDRESS, wallet, venue.target)], block))
            overrides = None
            if live:
                if balance < opp.amount_in_raw:
                    return result.fail(f"wallet holds {Decimal(balance) / Decimal(10**6):,.2f} USDC, "
                                       f"the buy needs {opp.amount_in_usdc:,.2f}")
                if allowance < opp.amount_in_raw and not self._approve(
                        USDC_ADDRESS, venue.target, max(self.float_cap_raw, opp.amount_in_raw), result, **send_kwargs):
                    return result.fail("USDC approval reverted")
            elif balance < opp.amount_in_raw or allowance < opp.amount_in_raw:
                overrides = token_overrides(USDC_ADDRESS, wallet, venue.target,
                                            max(balance, opp.amount_in_raw), max(allowance, opp.amount_in_raw))
                result.virtual = True
            try:
                raw = bytes(self.w3.eth.call(tx, block, overrides))
            except Exception as exc:
                return result.fail(f"pre-flight reverted: {exc}")
            simulated = _uint(raw)  # None for pools whose exchange_underlying returns nothing (old PAX pool)
            if simulated is not None:
                result.usdp_raw = simulated
            if not live:
                self._paper_buys[venue.name] = (opp.amount_in_raw, opp.usdp_out_raw)
                self.inventory.record_buy(opp.amount_in_raw, result.usdp_raw, buy_gas, venue=venue.name,
                                          block=opp.block, paper=True)
                self.inventory.save(self.float_state_path)
                return result

            tx_hash, receipt, gas = self._send(tx, **send_kwargs)
            result.tx_hashes.append(tx_hash)
            result.gas_usd = gas
            if receipt["status"] != 1:
                self.inventory.record_cost(gas, "reverted", tx=tx_hash, venue=venue.name)
                self.inventory.save(self.float_state_path)
                return result.fail("swap reverted on-chain")
            result.usdp_raw = received_from_logs(receipt, USDP_ADDRESS, wallet)
            self.inventory.record_buy(opp.amount_in_raw, result.usdp_raw, result.gas_usd, venue=venue.name,
                                      block=opp.block, tx=tx_hash)
            self.inventory.save(self.float_state_path)
            self.wallet_usdp_raw = (self.wallet_usdp_raw or 0) + result.usdp_raw
            return result
        except Exception as exc:
            return result.fail(f"{exc} (a sent transaction may still be pending)" if result.tx_hashes else str(exc))

    def _hold(self, reason: str, mode: str, usdp_raw: int) -> FloatResult | None:
        """Report why held USDP is not being sold, once per distinct reason."""
        if reason == self._sell_note:
            return None
        self._sell_note = reason
        return FloatResult("sell", mode, "-", usdp_raw, 0).fail(f"holding: {reason}")

    def maybe_sell_float(self, live: bool, **send_kwargs: Any) -> FloatResult | None:
        """Sell the held USDP to Stable.com once it reaches the 1,000 USDP minimum order.

        The order is the held amount cut to Stable.com's 6 decimals; no buffer is needed because the amount is already
        in the wallet. Paper mode sells the paper inventory, live sells the wallet's whole USDP balance. Returns None
        when there is nothing to do. After a failed attempt the next one waits SELL_RETRY_SECONDS, so a persistent
        failure does not create a Stable.com order every block.
        """
        if time.time() < self._sell_retry_at:
            return None
        result = self._sell_float(live, **send_kwargs)
        if result is not None and not result.success and not (result.error or "").startswith("holding"):
            self._sell_retry_at = time.time() + SELL_RETRY_SECONDS
        return result

    def _sell_float(self, live: bool, **send_kwargs: Any) -> FloatResult | None:
        held = self.inventory.usdp_raw if self.paper else (self.wallet_usdp_raw or 0)
        amount_raw = min(held, STABLE_MAX_ORDER_USDP * 10**USDP_DECIMALS) // USDP_PER_USDC_RAW * USDP_PER_USDC_RAW
        if amount_raw < STABLE_MIN_ORDER_USDP * 10**USDP_DECIMALS:
            self._sell_note = None
            return None
        mode = "live" if live else "dry-run"
        result = FloatResult("sell", mode, "-", amount_raw, 0)
        try:
            wallet = self._wallet()
            tokens = [STABLE_PAYOUT_TOKENS[a] for a in self.float_sell_assets]
            res = self.multicall(
                [_balance_call(t, STABLE_POOL_ADDRESS) for t in tokens]
                + [_balance_call(USDP_ADDRESS, wallet), _allowance_call(USDP_ADDRESS, wallet, STABLE_POOL_ADDRESS),
                   (MULTICALL3, _selector("getEthBalance(address)") + encode(["address"], [wallet]))]
            )
            room = {a: _uint(r) or 0 for a, r in zip(self.float_sell_assets, res)}
            usdp_balance, usdp_allowance, eth_balance = (_uint(r) or 0 for r in res[len(tokens):])
            choice = choose_payout(self.float_sell_assets, room, amount_raw)
            if choice is None:
                return self._hold(f"Stable.com has under {STABLE_MIN_ORDER_USDP:,} {'/'.join(self.float_sell_assets)} "
                                  "left to pay out", mode, amount_raw)
            asset, amount_raw = choice
            token = STABLE_PAYOUT_TOKENS[asset]
            amount = Decimal(amount_raw) / Decimal(10**USDP_DECIMALS)
            result.target, result.usdp_raw = asset, amount_raw
            quote = self.stable_quote(amount, asset, wallet)
            out_raw = int(Decimal(str(quote.get("amountTo", "0"))) * Decimal(10**USDC_DECIMALS))
            if out_raw < amount_raw // USDP_PER_USDC_RAW:
                return self._hold(f"Stable.com quotes {Decimal(out_raw) / Decimal(10**6):,.6f} {asset} for "
                                  f"{amount:,.6f} USDP, below 1:1", mode, amount_raw)
            self._sell_note = None
            result.fee_usd = Decimal(str(quote.get("nativeFeeUsd") or "0"))
            if "nativeFeeUsd" in quote:
                self.stable_fee_usd = result.fee_usd
            if live:
                if usdp_balance < amount_raw:
                    return result.fail(f"wallet holds {Decimal(usdp_balance) / Decimal(10**18):,.6f} USDP, under the order")
                if usdp_allowance < amount_raw and not self._approve(
                        USDP_ADDRESS, STABLE_POOL_ADDRESS, max(amount_raw, self.float_cap_raw * USDP_PER_USDC_RAW),
                        result, **send_kwargs):
                    return result.fail("USDP approval reverted")
            order = self.stable_order(amount, asset, wallet)
            fee_native = int(order.get("executionFeeNative") or order.get("nativeFee") or 0)
            signature = bytes.fromhex(str(order["maintainerSignature"]).removeprefix("0x"))
            swap = (amount_raw, to_checksum_address(USDP_ADDRESS), to_checksum_address(token), 1, wallet,
                    int(order["deadline"]), int(order["nonce"]))
            data = _selector(SINGLE_CHAIN_SWAP) + encode(
                ["(uint256,address,address,uint64,address,uint64,uint256)", "bytes", "uint256"], [swap, signature, fee_native]
            )
            tx = {"from": wallet, "to": to_checksum_address(STABLE_POOL_ADDRESS), "value": fee_native, "data": "0x" + data.hex()}
            overrides = None
            if not live and (usdp_balance < amount_raw or usdp_allowance < amount_raw or eth_balance < fee_native):
                overrides = token_overrides(USDP_ADDRESS, wallet, STABLE_POOL_ADDRESS,
                                            max(usdp_balance, amount_raw), max(usdp_allowance, amount_raw))
                overrides[wallet] = {"balance": hex(max(eth_balance, fee_native))}
                result.virtual = True
            try:
                self.w3.eth.call(tx, "latest", overrides)
            except Exception as exc:
                return result.fail(f"pre-flight reverted: {exc}")
            if not live:
                result.usdc_raw, result.gas_usd = out_raw, self.gas_usd(STABLE_SELL_GAS)
                result.pnl_usd = self.inventory.record_sell(amount_raw, out_raw, result.gas_usd, result.fee_usd,
                                                            asset=asset, paper=True)
                self.inventory.save(self.float_state_path)
                return result

            tx_hash, receipt, gas = self._send(tx, **send_kwargs)
            result.tx_hashes.append(tx_hash)
            result.gas_usd = gas
            if receipt["status"] != 1:
                self.inventory.record_cost(gas, "reverted", tx=tx_hash, asset=asset)
                self.inventory.save(self.float_state_path)
                return result.fail("Stable.com swap reverted on-chain")
            result.usdc_raw = received_from_logs(receipt, token, wallet)
            result.pnl_usd = self.inventory.record_sell(amount_raw, result.usdc_raw, result.gas_usd, result.fee_usd,
                                                        asset=asset, tx=tx_hash)
            self.inventory.save(self.float_state_path)
            self.wallet_usdp_raw = max(0, (self.wallet_usdp_raw or 0) - amount_raw)
            return result
        except Exception as exc:
            return result.fail(f"{exc} (a sent transaction may still be pending)" if result.tx_hashes else str(exc))

    # ---- Stable.com orders for the float wallet -----------------------------------------------------
    def _stable_post(self, path: str, body: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self.engine.session.post(f"{STABLE_BASE_URL}{path}", json=body, headers=STABLE_HEADERS,
                                                timeout=self.engine.timeout)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                return resp.json()
            except Exception as exc:
                last = exc
                if attempt < attempts:
                    time.sleep(0.4 * attempt)
        raise RuntimeError(f"Stable.com {path} failed after {attempts} attempts: {last}")

    @staticmethod
    def _stable_body(amount: Decimal, asset: str, wallet: str) -> dict[str, Any]:
        return {
            "assetFrom": "USDP",
            "assetTo": asset,
            "chainFrom": STABLE_CHAIN_ID,
            "chainTo": STABLE_CHAIN_ID,
            "amountFrom": f"{amount:.6f}",
            "addressFrom": wallet,
            "addressTo": wallet,
            "gasLess": False,
        }

    def stable_quote(self, amount: Decimal, asset: str, wallet: str) -> dict[str, Any]:
        """Stable.com /swap/status for selling `amount` USDP from the wallet; returns the response's `asset` section."""
        return self._stable_post("/swap/status", self._stable_body(amount, asset, wallet)).get("asset") or {}

    def stable_order(self, amount: Decimal, asset: str, wallet: str) -> dict[str, Any]:
        """Create a signed Stable.com singleChain order for the wallet (nonce, deadline, maintainerSignature)."""
        body = {**self._stable_body(amount, asset, wallet), "amountTo": f"{amount:.6f}", "device": str(uuid.uuid4())}
        data = self._stable_post("/swap/create/singleChain", body).get("data") or {}
        if not data.get("maintainerSignature"):
            raise RuntimeError(f"Stable.com did not return a maintainerSignature: {data}")
        return data


def format_opportunity(opp: Opportunity) -> str:
    avg = opp.amount_in_usdc / opp.usdp_out if opp.usdp_out else Decimal(0)
    costs = "gas" if opp.kind == "flash" else "gas+sell share"
    return (
        f"[block {opp.block}] {opp.venue:<17} {opp.kind} buy {opp.usdp_out:>11,.2f} USDP for {opp.amount_in_usdc:>10,.2f} USDC "
        f"(avg {avg:.5f}) | gross +${opp.gross_profit_usd:,.2f} {costs} ${opp.gas_usd:,.2f} net +${opp.net_profit_usd:,.2f}"
    )


def format_float_inventory(inv: FloatInventory) -> str:
    return (
        f"float inventory {inv.usdp:,.2f} USDP (cost {inv.cost_usdc:,.2f} USDC, unrealized {inv.unrealized_pnl_usd:+,.2f} USD) | "
        f"realized {inv.realized_pnl_usd:+,.2f} USD over {inv.buys} buys / {inv.sells} sells"
    )


def format_float_result(res: FloatResult, inv: FloatInventory) -> str:
    usdp = Decimal(res.usdp_raw) / Decimal(10**USDP_DECIMALS)
    usdc = Decimal(res.usdc_raw) / Decimal(10**USDC_DECIMALS)
    if not res.success:
        line = f"  float {res.action} [{res.mode}] {res.target}: {res.error}"
    elif res.action == "buy":
        line = f"  float buy [{res.mode}] {res.target}: {usdc:,.2f} USDC -> {usdp:,.4f} USDP"
    else:
        line = f"  float sell [{res.mode}] {usdp:,.4f} USDP -> {usdc:,.4f} {res.target}, pnl {res.pnl_usd:+,.2f} USD"
    if res.success:
        line += f" | gas ${res.gas_usd:,.2f}" + (f" fee ${res.fee_usd:,.2f}" if res.fee_usd else "")
        line += " | pre-flight ok" + (" (simulated wallet balance)" if res.virtual else "")
    for tx_hash in res.tx_hashes:
        line += f"\n    https://etherscan.io/tx/{tx_hash}"
    return f"{line}\n  {format_float_inventory(inv)}"


def _opportunity_json(opp: Opportunity) -> dict[str, Any]:
    return {
        "venue": opp.venue,
        "kind": opp.kind,
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
    parser.add_argument("--float-usdc", type=Decimal, default=Decimal(os.getenv("ETH_USDP_BACKRUN_FLOAT_USDC", "0")),
                        help="Float mode: USDC of the operator wallet's own capital for dips under Stable.com's "
                             "1,000 USDP minimum (0 = off)")
    parser.add_argument("--float-min-profit-usd", type=Decimal,
                        default=Decimal(os.getenv("ETH_USDP_BACKRUN_FLOAT_MIN_PROFIT_USD", "0.10")),
                        help="Minimum net profit per float buy after its gas and its share of the Stable.com sell gas (USD)")
    parser.add_argument("--float-sell-assets", default=os.getenv("ETH_USDP_BACKRUN_FLOAT_SELL_ASSETS", "USDC"),
                        help="Stable.com payout tokens for float sells, in preference order (from USDC,USDG,PYUSD,USDT)")
    parser.add_argument("--float-state", default=None,
                        help="Float inventory file (default logs/usdp-float-live.json, or logs/usdp-float-paper.json "
                             "for --dry-run)")
    parser.add_argument("--float-status", action="store_true", help="Print the float inventory files and exit")
    args = parser.parse_args(argv)

    live_state = Path(args.float_state) if args.float_state else PROJECT_ROOT / "logs" / "usdp-float-live.json"
    paper_state = Path(args.float_state) if args.float_state else PROJECT_ROOT / "logs" / "usdp-float-paper.json"
    if args.float_status:
        files = [("float", live_state)] if args.float_state else [("live", live_state), ("paper", paper_state)]
        for label, path in files:
            inv = FloatInventory.load(path)
            print(f"{label} ({path}{'' if path.exists() else ', not created yet'}): {format_float_inventory(inv)}")
            for entry in inv.history[-5:]:
                print(f"    {json.dumps(entry)}")
        return 0

    if args.live and args.confirm_live.strip().upper() != "LIVE":
        print("Refusing to broadcast: pass --confirm-live LIVE together with --live.", file=sys.stderr)
        return 2

    action = "live" if args.live else ("dry-run" if args.dry_run else "watch")
    engine = USDPTestEngine(executor=args.executor)
    private_key = os.getenv("ETH_OPERATOR_PRIVATE_KEY", "").strip()
    wallet = None
    if args.float_usdc > 0:
        if private_key:
            from eth_account import Account

            wallet = Account.from_key(private_key).address
        else:
            wallet = engine.operator
        if action == "live" and not private_key:
            print("Float mode needs ETH_OPERATOR_PRIVATE_KEY to trade live.", file=sys.stderr)
            return 2
        if action == "dry-run" and not wallet:
            print("Float mode needs a wallet: set ETH_OPERATOR_PRIVATE_KEY or ETH_OPERATOR_ADDRESS.", file=sys.stderr)
            return 2
    venues = DEFAULT_VENUES
    v4_pools = () if args.no_v4 else DEFAULT_V4_POOLS
    if args.venues:
        wanted = {v.strip() for v in args.venues.split(",") if v.strip()}
        venues = tuple(v for v in venues if v.name in wanted)
        v4_pools = tuple(p for p in v4_pools if p.name in wanted)
    # A one-off historical check never touches the inventory files; watch mode only shows what float mode would buy.
    state_path = None
    if args.block is None and action != "watch":
        state_path = live_state if action == "live" else paper_state
    bot = USDPBackrunner(engine=engine, mode=args.mode, min_profit_usd=args.min_profit_usd,
                         buffer_bps=args.buffer_bps, venues=venues, v4_pools=v4_pools,
                         float_usdc=args.float_usdc, float_min_profit_usd=args.float_min_profit_usd, float_wallet=wallet,
                         float_sell_assets=[a.strip().upper() for a in args.float_sell_assets.split(",") if a.strip()],
                         float_state_path=state_path, paper=(action != "live"))
    live = action == "live"
    exec_kwargs = dict(gas_multiplier=args.gas_multiplier, max_fee_gwei=args.max_fee_gwei,
                       priority_fee_gwei=args.priority_fee_gwei, receipt_timeout=args.receipt_timeout)

    def handle(block: int | str) -> list[Opportunity]:
        opps = bot.scan(block)
        for opp in opps:
            print(json.dumps(_opportunity_json(opp)) if args.json else format_opportunity(opp), flush=True)
        if action == "watch":
            return opps
        historical = args.block is not None
        flash = next((o for o in opps if o.kind == "flash"), None)
        if flash is not None and not historical:
            result = bot.execute(flash, live=live, **exec_kwargs)
            if result is None:
                print(f"  skipped {flash.venue}: re-quote no longer clears min profit", flush=True)
            else:
                print(format_execution_report(result), flush=True)
        small = next((o for o in opps if o.kind == "float"), None)
        if small is not None:
            # Historical blocks pre-flight the buy at that block; the sell needs a fresh Stable.com order, so it is skipped.
            res = bot.execute_float_buy(small, live=live, block=block if historical else "latest", **exec_kwargs)
            if res is not None:
                print(format_float_result(res, bot.inventory), flush=True)
        if bot.float_enabled and not historical:
            res = bot.maybe_sell_float(live=live, **exec_kwargs)
            if res is not None:
                print(format_float_result(res, bot.inventory), flush=True)
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
    if bot.float_enabled:
        held = "the wallet's whole USDP balance is sold" if live else "paper USDP is sold"
        print(f"float: up to {args.float_usdc:,} USDC from {wallet or 'no wallet (watch only)'}, min profit "
              f"${args.float_min_profit_usd}, {held} to Stable.com for {'/'.join(bot.float_sell_assets)} once it reaches "
              f"{STABLE_MIN_ORDER_USDP:,} | {format_float_inventory(bot.inventory)}", flush=True)
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
