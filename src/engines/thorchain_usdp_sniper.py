#!/usr/bin/env python3
"""THORChain Native Ethereum USDC -> USDP Sniper Engine.

Continuously quotes 10,000 USDC -> USDP on THORChain, monitors expected output,
and when 10,000 USDC yields minimum 10,010 USDP (+10 bps or user target):
1. Verifies USDC balance and allowance for the quoted THORChain router.
2. Approves the router if needed.
3. Constructs a protected swap memo enforcing an on-chain limit floor (>= 10,010 USDP).
4. Submits the depositWithExpiry transaction via Flashbots / private RPC.
5. Monitors the on-chain USDP arrival on Ethereum until confirmed.
6. Prints the execution summary and cleanly exits.

Usage:
  # Monitor / dry-run:
  python3 src/engines/thorchain_usdp_sniper.py --amount 10000 --min-usdp 10010 --poll-interval 10

  # Check single quote and exit:
  python3 src/engines/thorchain_usdp_sniper.py --once

  # Live execution (requires --live and --confirm-live EXECUTE_THOR_SNIPER):
  python3 src/engines/thorchain_usdp_sniper.py --live --confirm-live EXECUTE_THOR_SNIPER
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass

try:
    from web3 import Web3
except ImportError:
    Web3 = None

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("thorchain_usdp_sniper")

# Constants
DEFAULT_THORNODE_BASE = "https://gateway.liquify.com/chain/thorchain_api/thorchain"
DEFAULT_ETH_RPC = os.getenv("ETH_RPC_URL", "https://ethereum.publicnode.com").strip()
FALLBACK_ETH_RPCS = [
    "https://rpc.mevblocker.io",
    "https://eth.drpc.org",
    "https://1rpc.io/eth",
    "https://ethereum.publicnode.com",
]
USDC_ASSET = "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
USDP_ASSET = "ETH.USDP-0X8E870D67F660D95D5BE530380D0EC0BD388289E1"
ETH_ASSET = "ETH.ETH"
# gasUsed by past depositWithExpiry calls (0x3dfaf115..., 0xce981330...) was ~74,830.
DEPOSIT_GAS_UNITS = 75_000
PRIORITY_FEE_WEI = 2_000_000_000
# Resting order: one sub-swap every 20 blocks, 720 of them = 14,400 blocks, THORChain's
# STREAMINGSWAPMAXLENGTH (~24h). Native limit orders can't rest: STREAMINGLIMITSWAPMAXAGE
# is 10 blocks. Longer gaps force fewer, bigger slices and a higher liquidity fee.
RESTING_INTERVAL = 20
RESTING_QUANTITY = 720
USDC_CONTRACT = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDP_CONTRACT = "0x8E870D67F660D95d5be530380D0eC0bd388289E1"
THORCHAIN_SCALE = 100_000_000  # 1e8 for all THORNode assets
USDC_DECIMALS = 6
USDP_DECIMALS = 18

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function",
    },
]

ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "address payable", "name": "vault", "type": "address"},
            {"internalType": "address", "name": "asset", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "string", "name": "memo", "type": "string"},
            {"internalType": "uint256", "name": "expiration", "type": "uint256"},
        ],
        "name": "depositWithExpiry",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    }
]


class SniperError(RuntimeError):
    pass


@dataclass
class PoolSpot:
    spot_rate: Decimal
    usdc_depth: Decimal
    usdp_depth: Decimal
    # Raw THORNode balances (1e8 units), used to replay sub-swaps against the pools.
    usdc_rune: int = 0
    usdc_asset: int = 0
    usdp_rune: int = 0
    usdp_asset: int = 0
    eth_usd: Decimal | None = None


@dataclass
class FillPlan:
    deposit: Decimal  # USDC to send
    quantity: int  # streaming sub-swaps assumed for this deposit
    filled: Decimal  # USDC that swaps before the price limit stops the stream
    delivered: Decimal  # USDP received after the outbound fee
    refunded: Decimal  # unfilled USDC returned after the refund fee
    gas_usd: Decimal  # Ethereum gas for the deposit
    net_profit: Decimal  # delivered + refunded - deposit - gas_usd, valuing USDP = USDC = $1


@dataclass
class QuoteResult:
    input_usdc: Decimal
    expected_usdp: Decimal
    expected_raw_1e8: int
    fees_usdp: dict[str, Decimal]
    slippage_bps: int
    total_swap_seconds: int
    inbound_address: str
    router: str
    memo: str
    expiry: int
    raw: dict[str, Any]


def init_web3(primary_rpc: str | None = None) -> tuple[Web3, str]:
    """Connect to primary Ethereum RPC with automatic fallbacks."""
    if Web3 is None:
        raise SniperError("web3 package is missing in the current Python environment. Please run with the project venv (.venv/bin/python3)")

    candidates = []
    if primary_rpc:
        candidates.append(primary_rpc)
    for rpc in [DEFAULT_ETH_RPC] + FALLBACK_ETH_RPCS:
        if rpc and rpc not in candidates:
            candidates.append(rpc)

    last_exc = None
    for rpc in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5.0}))
            if w3.is_connected():
                return w3, rpc
        except Exception as exc:
            last_exc = exc
            continue
    raise SniperError(f"Failed to connect to any Ethereum RPC endpoint: {last_exc}")


def fetch_pool_spot_rate(thor_client: ThorClient) -> PoolSpot:
    """Read pool reserves and compute 1 USDC -> USDP spot rate."""
    data = thor_client.get("pools")
    if not isinstance(data, list):
        raise SniperError("Invalid pools response from THORNode")

    pools = {p.get("asset"): p for p in data if isinstance(p, dict)}
    usdc_pool = pools.get(USDC_ASSET)
    usdp_pool = pools.get(USDP_ASSET)

    if not usdc_pool or not usdp_pool:
        raise SniperError("USDC or USDP pool unavailable in THORNode response")

    Xr = Decimal(str(usdc_pool.get("balance_rune", "0")))
    Xa = Decimal(str(usdc_pool.get("balance_asset", "0")))
    Yr = Decimal(str(usdp_pool.get("balance_rune", "0")))
    Ya = Decimal(str(usdp_pool.get("balance_asset", "0")))

    if min(Xr, Xa, Yr, Ya) <= 0:
        raise SniperError("Non-positive balance in pools")

    # Spot rate = (Ya / Yr) / (Xa / Xr) = (Ya * Xr) / (Yr * Xa)
    spot_rate = (Ya * Xr) / (Yr * Xa)

    # ETH/USD via RUNE, for pricing deposit gas.
    eth_usd = None
    eth_pool = pools.get(ETH_ASSET)
    if eth_pool and int(eth_pool.get("balance_asset", 0)) > 0:
        eth_usd = Decimal(eth_pool["balance_rune"]) / Decimal(eth_pool["balance_asset"]) * Xa / Xr

    return PoolSpot(
        spot_rate=spot_rate,
        usdc_depth=Xa / Decimal(THORCHAIN_SCALE),
        usdp_depth=Ya / Decimal(THORCHAIN_SCALE),
        usdc_rune=int(Xr),
        usdc_asset=int(Xa),
        usdp_rune=int(Yr),
        usdp_asset=int(Ya),
        eth_usd=eth_usd,
    )


class ThorClient:
    def __init__(self, base_url: str = DEFAULT_THORNODE_BASE, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.last_quote_start = float("-inf")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + "/" + path.lstrip("/")
        if params:
            url += "?" + urlencode(params)
        for attempt in range(3):
            if path.startswith("quote/"):
                delay = 0.4 - (time.monotonic() - self.last_quote_start)
                if delay > 0:
                    time.sleep(delay)
                self.last_quote_start = time.monotonic()
            try:
                request = Request(
                    url,
                    headers={"User-Agent": "arbbot-thorchain-sniper/1.0", "Accept": "application/json"},
                )
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.load(response)
                if isinstance(data, dict) and data.get("error"):
                    raise SniperError(str(data["error"])[:300])
                return data
            except HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                detail = exc.read(1024).decode("utf-8", errors="replace")
                raise SniperError(f"THORNode HTTP {exc.code}: {detail[:300]}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise SniperError(f"THORNode connection failed: {type(exc).__name__}") from exc
            except (ValueError, InvalidOperation) as exc:
                raise SniperError("THORNode returned invalid JSON") from exc
        raise SniperError("THORNode request failed")


def parse_quote(response: dict[str, Any], input_amount: Decimal) -> QuoteResult:
    if not isinstance(response, dict):
        raise SniperError("Invalid quote response structure")
    if "expected_amount_out" not in response:
        raise SniperError(f"Missing expected_amount_out in quote: {response}")

    expected_raw = int(response["expected_amount_out"])
    expected_usdp = Decimal(expected_raw) / Decimal(THORCHAIN_SCALE)

    fees = response.get("fees", {})
    fees_usdp = {}
    for k in ("liquidity", "affiliate", "outbound", "total"):
        if k in fees:
            fees_usdp[k] = Decimal(int(fees[k])) / Decimal(THORCHAIN_SCALE)

    return QuoteResult(
        input_usdc=input_amount,
        expected_usdp=expected_usdp,
        expected_raw_1e8=expected_raw,
        fees_usdp=fees_usdp,
        slippage_bps=int(fees.get("slippage_bps", 0)),
        total_swap_seconds=int(response.get("total_swap_seconds", 0)),
        inbound_address=response.get("inbound_address", ""),
        router=response.get("router", ""),
        memo=response.get("memo", ""),
        expiry=int(response.get("expiry", 0)),
        raw=response,
    )


def missing_affiliate_thor_aliases(thor_client: ThorClient, affiliate: str) -> list[str]:
    """Return the affiliate THORNames that have no THOR alias.

    THORChain pays the affiliate fee to the name's THOR alias. Without one, the swap
    is refunded with "swap affiliate address is empty while affiliate basis points
    is non-zero".
    """
    missing = []
    for name in affiliate.split("/"):
        name = name.strip()
        if not name or name.lower().startswith("thor1"):
            continue
        try:
            data = thor_client.get(f"thorname/{name}")
        except SniperError as exc:
            logger.warning("THORName lookup for affiliate '%s' failed: %s", name, exc)
            missing.append(name)
            continue
        aliases = (data.get("aliases") or []) if isinstance(data, dict) else []
        if not any(a.get("chain") == "THOR" and a.get("address") for a in aliases):
            missing.append(name)
    return missing


def build_protected_memo(
    raw_memo: str,
    min_usdp_target: Decimal,
    input_amount: Decimal = Decimal("10000"),
    floor_mode: str = "2bps",
    buffer_bps: int = 2,
) -> str:
    """Enforce that the limit field in the THORChain swap memo protects capital.

    THORChain swap memo format:
      =:ASSET:DESTADDR:LIMIT/INTERVAL/QUANTITY:AFFILIATE:FEE

    floor_mode:
      - '2bps': Enforces at least +2 bps over input amount (LIMIT = input_amount * 1.0002).
                Sub-swaps execute whenever rate >= 1.0002 USDP/USDC, covering gas in and outbound.
      - '1to1': Enforces exact 1:1 parity (LIMIT = input_amount). Sub-swaps execute whenever
                rate >= 1.0000 USDP/USDC.
      - 'target': Enforces strict target profit floor (LIMIT = min_usdp_target).
    """
    if not raw_memo:
        raise SniperError("Cannot protect empty memo")
    parts = raw_memo.split(":")
    if len(parts) < 4:
        raise SniperError(f"Unexpected memo structure: {raw_memo}")

    sub_parts = parts[3].split("/")
    existing_limit = int(sub_parts[0]) if sub_parts[0] and sub_parts[0].isdigit() else 0
    sub_parts[0] = str(protected_limit(existing_limit, min_usdp_target, input_amount, floor_mode, buffer_bps))
    parts[3] = "/".join(sub_parts)
    return ":".join(parts)


def protected_limit(
    existing_limit: int,
    min_usdp_target: Decimal,
    input_amount: Decimal,
    floor_mode: str = "2bps",
    buffer_bps: int = 2,
) -> int:
    """The memo LIMIT (1e8 units) build_protected_memo enforces for a floor mode."""
    if floor_mode in ("2bps", "gas_buffer", "plus2bps"):
        multiplier = Decimal(1) + Decimal(buffer_bps) / Decimal(10000)
        return int(input_amount * multiplier * THORCHAIN_SCALE)
    if floor_mode == "1to1":
        return int(input_amount * THORCHAIN_SCALE)
    return max(existing_limit, int(min_usdp_target * THORCHAIN_SCALE))


def memo_stream_fields(memo: str) -> tuple[int, int, int]:
    """(LIMIT, INTERVAL, QUANTITY) from a swap memo; missing fields are 0."""
    parts = memo.split(":")
    fields = parts[3].split("/") if len(parts) > 3 else []
    nums = [int(f) if f.isdigit() else 0 for f in fields[:3]]
    limit, interval, quantity = nums + [0] * (3 - len(nums))
    return limit, interval, quantity


def clp_leg(X: int, x: int, Y: int, min_slip_bps: int) -> int:
    """Output of one pool leg, as THORNode's GetSwapCalc computes it (1e8 units)."""
    if x <= 0:
        return 0
    slip_bps = round(x * 10000 / (x + X))
    if min_slip_bps > slip_bps:
        fee = min_slip_bps * x * Y // 10000 // (x + X)
        return max(0, x * Y // (x + X) - fee)
    return x * X * Y // ((x + X) ** 2)


def simulate_streaming_fill(
    pools: PoolSpot,
    deposit: int,
    quantity: int,
    trade_target: int,
    min_slip_bps: int,
    affiliate_bps: int,
    outbound_fee: int,
) -> tuple[int, int]:
    """Replay a rapid streaming USDC -> RUNE -> USDP swap against a pool snapshot.

    Follows THORNode's StreamingSwap.NextSize sizing and the per-sub-swap price-limit
    check (emit minus that sub-swap's share of the outbound fee must reach its share
    of the remaining target). Assumes nobody trades the pools back between sub-swaps,
    so each fill pushes the USDP price against the next one; with a ~60k USDP pool
    that is what stopped 0xce981330... after 11 of 1,637 sub-swaps.

    Returns (USDC filled, USDP emitted before the outbound fee), in 1e8 units.
    """
    Xa, Xr = pools.usdc_asset, pools.usdc_rune
    Yr, Ya = pools.usdp_rune, pools.usdp_asset
    quantity = max(1, quantity)
    fee_share = outbound_fee // quantity
    filled = emitted = 0
    for count in range(quantity):
        size = deposit // quantity
        if filled + size > deposit or count + 1 >= quantity:
            size = deposit - filled
        if size <= 0:
            break
        target = size * max(0, trade_target - emitted) // (deposit - filled)
        x = size - size * affiliate_bps // 10000
        rune = clp_leg(Xa, x, Xr, min_slip_bps)
        emit = clp_leg(Yr, rune, Ya, min_slip_bps)
        if emit - fee_share < target:
            # A skipped sub-swap leaves the pools as they were, so with nobody
            # trading them back every later sub-swap misses the limit too.
            break
        Xa, Xr = Xa + x, Xr - rune
        Yr, Ya = Yr + rune, Ya - emit
        filled += size
        emitted += emit
    return filled, emitted


def plan_deposit(
    pools: PoolSpot,
    candidates: list[Decimal],
    sub_swap_size: Decimal,
    trade_target_for: Callable[[Decimal], int],
    min_slip_bps: int,
    affiliate_bps: int,
    usdp_outbound_fee: Decimal,
    usdc_refund_fee: Decimal,
    gas_usd: Decimal,
    quantity_for: Callable[[Decimal], int] | None = None,
) -> FillPlan:
    """Pick the deposit, among candidates, with the best modeled net profit.

    trade_target_for(deposit) gives the memo limit (1e8 units) for a deposit size.
    quantity_for(deposit) gives its streaming quantity; by default the quote's
    sub-swap size is kept, as THORNode's auto-streaming does.
    """
    scale = Decimal(THORCHAIN_SCALE)
    outbound_1e8 = int(usdp_outbound_fee * scale)
    best = None
    for deposit in candidates:
        if deposit <= 0:
            continue
        if quantity_for is not None:
            quantity = quantity_for(deposit)
        else:
            quantity = max(1, int((deposit / sub_swap_size).to_integral_value())) if sub_swap_size > 0 else 1
        filled_1e8, emitted_1e8 = simulate_streaming_fill(
            pools,
            int(deposit * scale),
            quantity,
            trade_target_for(deposit),
            min_slip_bps,
            affiliate_bps,
            outbound_1e8,
        )
        filled = Decimal(filled_1e8) / scale
        delivered = max(Decimal(0), Decimal(emitted_1e8) / scale - usdp_outbound_fee) if filled_1e8 else Decimal(0)
        unfilled = deposit - filled
        refunded = max(Decimal(0), unfilled - usdc_refund_fee) if unfilled > 0 else Decimal(0)
        plan = FillPlan(
            deposit=deposit,
            quantity=quantity,
            filled=filled,
            delivered=delivered,
            refunded=refunded,
            gas_usd=gas_usd,
            net_profit=delivered + refunded - deposit - gas_usd,
        )
        if best is None or plan.net_profit > best.net_profit:
            best = plan
    if best is None:
        raise SniperError("No deposit candidates to plan")
    return best


def deposit_candidates(max_amount: Decimal, min_amount: Decimal, count: int = 60) -> list[Decimal]:
    """Whole-USDC deposit sizes spread geometrically from min_amount to max_amount."""
    min_amount = max(Decimal(1), min(min_amount, max_amount))
    if count < 2 or max_amount <= min_amount:
        return [max_amount]
    ratio = (max_amount / min_amount) ** (Decimal(1) / Decimal(count - 1))
    sizes = {max_amount}
    size = min_amount
    for _ in range(count):
        sizes.add(min(max_amount, size.to_integral_value(rounding="ROUND_FLOOR")))
        size *= ratio
    return sorted(s for s in sizes if s > 0)


def plan_for_quote(
    pools: PoolSpot,
    quote: QuoteResult,
    max_amount: Decimal,
    target_rate: Decimal,
    floor_mode: str,
    buffer_bps: int,
    affiliate_bps: int,
    usdc_refund_fee: Decimal,
    gas_usd: Decimal,
    exact: bool = False,
) -> FillPlan:
    """Size a deposit from a THORNode quote and a pool snapshot.

    THORNode's expected_amount_out assumes the pools are traded back between
    sub-swaps; this replays them against the snapshot instead. With exact=True
    only the quoted amount and its streaming quantity are evaluated.
    """
    existing_limit, _, quantity = memo_stream_fields(quote.memo)
    quantity = max(1, quantity)
    sub_swap_size = quote.input_usdc / quantity

    def trade_target_for(deposit: Decimal) -> int:
        scaled_existing = int(existing_limit * deposit / quote.input_usdc)
        return protected_limit(scaled_existing, deposit * target_rate, deposit, floor_mode, buffer_bps)

    return plan_deposit(
        pools,
        [quote.input_usdc] if exact else deposit_candidates(max_amount, sub_swap_size),
        sub_swap_size,
        trade_target_for,
        quote_fee_tier_bps(quote),
        affiliate_bps,
        quote.fees_usdp.get("outbound", Decimal(0)),
        usdc_refund_fee,
        gas_usd,
        quantity_for=(lambda _deposit: quantity) if exact else None,
    )


def slice_fill_rate(
    pools: PoolSpot,
    quote: QuoteResult,
    target_rate: Decimal,
    floor_mode: str,
    buffer_bps: int,
    affiliate_bps: int,
) -> Decimal | None:
    """Pool rate (USDP per USDC) at which the quote's first sub-swap clears its limit.

    For a resting order the pool has time to be traded back between slices, so this is
    roughly the rate each slice needs when its turn comes. None if not reachable.
    """
    existing_limit, _, quantity = memo_stream_fields(quote.memo)
    quantity = max(1, quantity)
    deposit = int(quote.input_usdc * THORCHAIN_SCALE)
    target = protected_limit(existing_limit, quote.input_usdc * target_rate, quote.input_usdc, floor_mode, buffer_bps)
    outbound = int(quote.fees_usdp.get("outbound", Decimal(0)) * THORCHAIN_SCALE)
    tier = quote_fee_tier_bps(quote)

    def first_slice_fills(m: float) -> bool:
        # Move the USDP pool's price by m along its constant-product curve, as arbitrage would.
        r = math.sqrt(m)
        moved = replace(pools, usdp_asset=int(pools.usdp_asset * r), usdp_rune=int(pools.usdp_rune / r))
        return simulate_streaming_fill(moved, deposit, quantity, target, tier, affiliate_bps, outbound)[0] > 0

    lo, hi = 0.5, 2.0
    if not first_slice_fills(hi):
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if first_slice_fills(mid) else (mid, hi)
    return pools.spot_rate * Decimal(repr(hi))


def describe_plan(plan: FillPlan) -> str:
    if plan.filled == 0:
        return f"No deposit size covers the fixed fees at this pool state (best case {plan.net_profit:+.2f} USD)"
    return (
        f"Best deposit {plan.deposit:,.0f} USDC: fills {plan.filled:,.2f} before the limit -> "
        f"{plan.delivered:,.2f} USDP + {plan.refunded:,.2f} USDC refund, gas ${plan.gas_usd:.2f}, "
        f"modeled net {plan.net_profit:+.2f} USD"
    )


def quote_fee_tier_bps(quote: QuoteResult) -> int:
    """Per-leg minimum slip fee implied by a quote's liquidity fee (two legs)."""
    liquidity = quote.fees_usdp.get("liquidity", Decimal(0))
    gross = quote.expected_usdp + liquidity + quote.fees_usdp.get("outbound", Decimal(0)) + quote.fees_usdp.get("affiliate", Decimal(0))
    if gross <= 0:
        return 0
    return int((liquidity / (2 * gross) * 10000).to_integral_value())


def execute_deposit(
    w3: Web3,
    operator_account: Any,
    router_address: str,
    vault_address: str,
    usdc_amount: Decimal,
    memo: str,
    expiry: int,
    allow_public_fallback: bool = True,
) -> str:
    """Approve router if necessary, build depositWithExpiry, and broadcast via Flashbots / private RPC."""
    router_checksum = Web3.to_checksum_address(router_address)
    vault_checksum = Web3.to_checksum_address(vault_address)
    usdc_checksum = Web3.to_checksum_address(USDC_CONTRACT)

    usdc_contract = w3.eth.contract(address=usdc_checksum, abi=ERC20_ABI)
    router_contract = w3.eth.contract(address=router_checksum, abi=ROUTER_ABI)

    amount_raw_usdc = int(usdc_amount * 10**USDC_DECIMALS)

    # 1. Check & handle USDC allowance
    allowance = usdc_contract.functions.allowance(operator_account.address, router_checksum).call()
    logger.info(
        "Checking USDC allowance for Router %s: Current=%s USDC, Required=%s USDC",
        router_checksum,
        allowance / 10**USDC_DECIMALS,
        usdc_amount,
    )
    if allowance < amount_raw_usdc:
        logger.info("Approving THORChain router to spend %s USDC...", usdc_amount)
        nonce = w3.eth.get_transaction_count(operator_account.address, "latest")
        base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 20_000_000_000)
        priority_fee = 2_000_000_000  # 2 Gwei

        approve_tx = usdc_contract.functions.approve(
            router_checksum,
            amount_raw_usdc * 10,  # Approve 10x or max
        ).build_transaction({
            "from": operator_account.address,
            "nonce": nonce,
            "maxFeePerGas": int(base_fee * 1.5) + priority_fee,
            "maxPriorityFeePerGas": priority_fee,
            "gas": 60_000,
            "chainId": 1,
        })
        signed_approve = w3.eth.account.sign_transaction(approve_tx, operator_account.key)
        approve_hash = Web3.to_hex(w3.eth.send_raw_transaction(signed_approve.raw_transaction))
        logger.info("Broadcasted USDC approval tx: %s. Waiting for receipt...", approve_hash)
        receipt = w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)
        if receipt.get("status") != 1:
            raise SniperError(f"USDC approval transaction reverted: {approve_hash}")
        logger.info("USDC approval confirmed in block %s", receipt.get("blockNumber"))

    # 2. Build depositWithExpiry transaction
    nonce = w3.eth.get_transaction_count(operator_account.address, "latest")
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 20_000_000_000)
    priority_fee = 2_000_000_000

    logger.info("Building depositWithExpiry transaction...")
    logger.info("  Vault: %s", vault_checksum)
    logger.info("  Asset: %s", usdc_checksum)
    logger.info("  Amount: %s atomic units (%s USDC)", amount_raw_usdc, usdc_amount)
    logger.info("  Memo: %s", memo)
    logger.info("  Expiration: %s (%s UTC)", expiry, datetime.fromtimestamp(expiry, timezone.utc).isoformat())

    # Estimate gas or use safe budget
    try:
        gas_est = router_contract.functions.depositWithExpiry(
            vault_checksum,
            usdc_checksum,
            amount_raw_usdc,
            memo,
            expiry,
        ).estimate_gas({"from": operator_account.address, "value": 0})
        gas_limit = int(gas_est * 1.25)
    except Exception as exc:
        logger.warning("Gas estimation failed (%s); defaulting to 150,000 gas limit", exc)
        gas_limit = 150_000

    deposit_tx = router_contract.functions.depositWithExpiry(
        vault_checksum,
        usdc_checksum,
        amount_raw_usdc,
        memo,
        expiry,
    ).build_transaction({
        "from": operator_account.address,
        "nonce": nonce,
        "maxFeePerGas": int(base_fee * 1.5) + priority_fee,
        "maxPriorityFeePerGas": priority_fee,
        "gas": gas_limit,
        "value": 0,
        "chainId": 1,
    })

    signed_deposit = w3.eth.account.sign_transaction(deposit_tx, operator_account.key)

    # 3. Broadcast using Flashbots / standard RPC
    tx_hash = broadcast_transaction(w3, signed_deposit, allow_public_fallback=allow_public_fallback)
    logger.info("🚀 Deposit transaction broadcasted! Hash: %s", tx_hash)
    logger.info("Waiting for deposit confirmation on Ethereum...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.get("status") != 1:
        raise SniperError(f"Deposit transaction reverted on Ethereum! Hash: {tx_hash}")

    logger.info("✅ Deposit confirmed in block %s! Gas used: %s", receipt.get("blockNumber"), receipt.get("gasUsed"))
    return tx_hash


def broadcast_transaction(w3: Web3, signed_tx: Any, allow_public_fallback: bool = True) -> str:
    """Submit via Flashbots Protect or standard RPC."""
    # HexBytes >= 1.0 drops the 0x prefix from .hex(); Web3.to_hex always keeps it.
    raw_tx_bytes = signed_tx.raw_transaction
    raw_tx_hex = Web3.to_hex(raw_tx_bytes)
    tx_hash = Web3.to_hex(signed_tx.hash)

    flashbots_rpc = os.getenv("ETH_FLASHBOTS_RPC", "https://rpc.flashbots.net/fast").strip()
    enable_flashbots = os.getenv("ETH_ENABLE_FLASHBOTS", "true").strip().lower() not in ("false", "0", "no")

    if enable_flashbots and flashbots_rpc:
        try:
            logger.info("Submitting transaction via Flashbots Protect: %s", flashbots_rpc)
            req_data = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_sendRawTransaction",
                "params": [raw_tx_hex],
            }).encode("utf-8")
            req = Request(
                flashbots_rpc,
                data=req_data,
                headers={"Content-Type": "application/json", "User-Agent": "arbbot-flashbots/1.0"},
            )
            with urlopen(req, timeout=10.0) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
            if not resp_json.get("error") and str(resp_json.get("result", "")).lower() == tx_hash.lower():
                logger.info("[Flashbots Protect] Accepted: %s", resp_json["result"])
                return tx_hash
            if "error" in resp_json:
                logger.warning("[Flashbots Protect] RPC error: %s", resp_json["error"])
        except Exception as exc:
            logger.warning("[Flashbots Protect] Submission error: %s", exc)

    if not allow_public_fallback and enable_flashbots:
        raise SniperError("Flashbots submission failed and allow_public_fallback is false")

    logger.info("Broadcasting transaction via configured Ethereum RPC...")
    sent_hash = w3.eth.send_raw_transaction(raw_tx_bytes)
    return Web3.to_hex(sent_hash)


def monitor_outbound(
    w3: Web3,
    operator_address: str,
    initial_usdp_balance: int,
    min_usdp_target: Decimal,
    deposit_tx_hash: str,
    thor_client: ThorClient,
    poll_interval: float = 10.0,
    max_wait_seconds: float = 7200.0,
) -> Decimal:
    """Poll Ethereum operator USDP balance and THORNode status until USDP arrives."""
    usdp_checksum = Web3.to_checksum_address(USDP_CONTRACT)
    usdp_contract = w3.eth.contract(address=usdp_checksum, abi=ERC20_ABI)
    target_increase_atomic = int(min_usdp_target * 10**USDP_DECIMALS)

    start_time = time.monotonic()
    last_status_check = 0.0
    logger.info("Initiating outbound tracking for operator %s...", operator_address)
    logger.info("Target USDP net gain: >= %s USDP", min_usdp_target)

    clean_tx_hash = deposit_tx_hash.lower()
    if clean_tx_hash.startswith("0x"):
        clean_tx_hash = clean_tx_hash[2:]

    while time.monotonic() - start_time < max_wait_seconds:
        # 1. Check on-chain USDP balance directly
        try:
            curr_balance = usdp_contract.functions.balanceOf(operator_address).call()
            diff = curr_balance - initial_usdp_balance
            if diff >= target_increase_atomic:
                received_usdp = Decimal(diff) / Decimal(10**USDP_DECIMALS)
                logger.info("🎉 USDP OUTBOUND CONFIRMED ON ETHEREUM!")
                logger.info("  Received: %s USDP (net +%s USDP)", curr_balance / 10**18, received_usdp)
                return received_usdp
        except Exception as exc:
            logger.warning("Error querying USDP balance: %s", exc)

        # 2. Check THORNode status every 30s
        if time.monotonic() - last_status_check > 30.0:
            last_status_check = time.monotonic()
            try:
                status_data = thor_client.get(f"tx/status/{clean_tx_hash}")
                stages = status_data.get("stages", {})
                logger.info("THORChain Status Stages: %s", json.dumps(stages))
                if stages.get("swap_finalised", {}).get("completed") and stages.get("outbound_signed", {}).get("completed"):
                    # A partial fill delivers less than the target; wait for what THORChain
                    # actually sent instead of polling until the timeout.
                    usdp_out_1e8 = sum(
                        int(coin.get("amount", 0))
                        for out in status_data.get("out_txs") or []
                        for coin in out.get("coins") or []
                        if coin.get("asset") == USDP_ASSET
                    )
                    if usdp_out_1e8 == 0:
                        logger.info("THORChain finished the swap without delivering USDP (deposit refunded).")
                        return Decimal(0)
                    target_increase_atomic = min(target_increase_atomic, usdp_out_1e8 * 10 ** (USDP_DECIMALS - 8))
            except Exception as exc:
                logger.debug("THORNode tx status query: %s", exc)

        elapsed = int(time.monotonic() - start_time)
        logger.info("Waiting for USDP outbound... (%ss elapsed). Checking again in %ss...", elapsed, poll_interval)
        time.sleep(poll_interval)

    raise SniperError(f"Outbound monitoring timed out after {max_wait_seconds}s!")


def run_sniper(args: argparse.Namespace) -> int:
    if args.min_usdp is None:
        args.min_usdp = (args.amount * (Decimal(10000) + args.min_profit_bps) / Decimal(10000)).quantize(Decimal("0.0001"))
    # Deposits get resized to what the pool can absorb, so keep the target as a rate.
    target_rate = args.min_usdp / args.amount

    if args.resting:
        # Polls until the rate lets slices fill, places one long stream, then exits;
        # the inspector tracks the ~24h stream, so don't block on delivery.
        args.no_monitor = True
        args.interval = args.interval or RESTING_INTERVAL
        args.quantity = args.quantity or RESTING_QUANTITY

    logger.info("================================================================")
    logger.info("           THORChain USDC -> USDP Sniper Engine                ")
    logger.info("================================================================")
    logger.info("Input Amount:       %s USDC", args.amount)
    logger.info("Min Trigger Output: %s USDP (+%.1f bps)", args.min_usdp, float((args.min_usdp - args.amount) / args.amount * 10000))
    logger.info("Poll Interval:      %s seconds", args.poll_interval)
    logger.info("Slippage Tolerance: %s bps", args.slippage_bps)
    logger.info("Affiliate:          %s (%s bps)", args.affiliate, args.affiliate_bps)
    logger.info("Mode:               %s", "LIVE EXECUTION" if args.live else "SIMULATION / DRY-RUN")
    if args.resting:
        logger.info("Order Type:         RESTING (1 sub-swap every %s blocks x %s)", args.interval, args.quantity)
    logger.info("================================================================")

    thor_client = ThorClient(base_url=args.base_url)

    # Initialize Web3 with fallback resilience
    w3, active_rpc = init_web3(args.rpc_url)
    logger.info("Connected to Ethereum RPC: %s", active_rpc)

    # Operator setup
    pk = os.getenv("ETH_OPERATOR_PRIVATE_KEY", "").strip()
    if not pk:
        logger.error("ETH_OPERATOR_PRIVATE_KEY not found in .env")
        return 1

    operator_account = w3.eth.account.from_key(pk)
    destination_address = args.destination or operator_account.address
    logger.info("Operator Address:    %s", operator_account.address)
    logger.info("Destination Address: %s", destination_address)

    # Check operator balances
    eth_bal = w3.eth.get_balance(operator_account.address)
    logger.info("Operator ETH:        %s ETH", w3.from_wei(eth_bal, "ether"))

    usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_CONTRACT), abi=ERC20_ABI)
    usdc_bal = usdc_contract.functions.balanceOf(operator_account.address).call() / 10**USDC_DECIMALS
    logger.info("Operator USDC:       %s USDC", usdc_bal)

    usdp_contract = w3.eth.contract(address=Web3.to_checksum_address(USDP_CONTRACT), abi=ERC20_ABI)
    initial_usdp_balance = usdp_contract.functions.balanceOf(operator_account.address).call()
    logger.info("Operator USDP:       %s USDP", initial_usdp_balance / 10**USDP_DECIMALS)

    if args.live:
        if args.confirm_live != "EXECUTE_THOR_SNIPER":
            logger.error("LIVE mode requires --confirm-live EXECUTE_THOR_SNIPER for safety.")
            return 1
        if usdc_bal < float(args.amount):
            spendable = Decimal(str(usdc_bal)).to_integral_value(rounding="ROUND_FLOOR")
            # Only the capacity planner treats --amount as a ceiling; elsewhere it is the deposit.
            if args.no_capacity_check or args.resting or spendable < 1:
                logger.error(
                    "Insufficient USDC balance (Have %s USDC, Need %s USDC) for live execution!",
                    usdc_bal,
                    args.amount,
                )
                return 1
            # --amount is only the largest deposit the fill-capacity check may choose.
            logger.warning("USDC balance %s is below --amount %s; capping deposits at %s USDC.", usdc_bal, args.amount, spendable)
            args.amount = spendable
            args.min_usdp = args.amount * target_rate
        if eth_bal < w3.to_wei(0.003, "ether"):
            logger.warning("Low ETH balance for gas! (Current: %s ETH)", w3.from_wei(eth_bal, "ether"))

    # Prepare quote parameters
    amount_raw_1e8 = int(args.amount * THORCHAIN_SCALE)
    params = {
        "from_asset": USDC_ASSET,
        "to_asset": USDP_ASSET,
        "amount": amount_raw_1e8,
        "destination": destination_address,
        "streaming_interval": args.interval,
        "streaming_quantity": args.quantity,
        "liquidity_tolerance_bps": args.slippage_bps,
    }
    affiliate_bps = 0
    if args.affiliate and args.affiliate.lower() not in ("none", "direct"):
        params["affiliate"] = args.affiliate
        params["affiliate_bps"] = args.affiliate_bps
        affiliate_bps = args.affiliate_bps

        if args.affiliate_bps > 0:
            missing = missing_affiliate_thor_aliases(thor_client, args.affiliate)
            if missing:
                log = logger.error if args.live else logger.warning
                log(
                    "Affiliate %s has no THOR alias, so THORChain would refund the swap "
                    "(affiliate address empty). Register one with ~:NAME:THOR:thor1... or use --affiliate-bps 0.",
                    ", ".join(missing),
                )
                if args.live:
                    return 1

    cost_cache: dict[str, Any] = {"at": float("-inf"), "refund_fee": None, "gas_eth": None}

    def current_costs(pools: PoolSpot) -> tuple[Decimal, Decimal]:
        """(USDC refund outbound fee, deposit gas in USD), refreshed at most once a minute."""
        if time.monotonic() - cost_cache["at"] > 60:
            try:
                for entry in thor_client.get("outbound_fees"):
                    if entry.get("asset") == USDC_ASSET:
                        cost_cache["refund_fee"] = Decimal(entry["outbound_fee"]) / Decimal(THORCHAIN_SCALE)
            except Exception as exc:
                logger.warning("Outbound fee lookup failed: %s", exc)
            try:
                base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
                cost_cache["gas_eth"] = Decimal(DEPOSIT_GAS_UNITS * (base_fee + PRIORITY_FEE_WEI)) / Decimal(10**18)
            except Exception as exc:
                logger.warning("Base fee lookup failed: %s", exc)
            cost_cache["at"] = time.monotonic()
        # Unknown costs are priced high on purpose, so they can only block a trade.
        refund_fee = cost_cache["refund_fee"] if cost_cache["refund_fee"] is not None else Decimal("2.5")
        if cost_cache["gas_eth"] is not None and pools.eth_usd:
            gas_usd = cost_cache["gas_eth"] * pools.eth_usd
        else:
            gas_usd = Decimal("2")
        return refund_fee, gas_usd

    # Main monitoring loop with dual spot-correlation & direct snipe engine
    logger.info("Starting live rate monitoring (Amount: %s USDC, Target: %s USDP)...", args.amount, args.min_usdp)
    poll_count = 0
    estimated_fee_spread = Decimal("9.40") * (args.amount / Decimal("10000"))
    rate_limited_until = 0.0
    last_wait_log = float("-inf")

    while True:
        poll_count += 1
        now = time.monotonic()

        # Step 1: Query spot pool rate (1 USDC -> USDP via RUNE CLP pools)
        spot_info = None
        predicted_out = None
        try:
            spot_info = fetch_pool_spot_rate(thor_client)
            gross = args.amount * spot_info.spot_rate
            predicted_out = gross - estimated_fee_spread
        except Exception as exc:
            logger.debug("Spot rate check error: %s", exc)

        # Step 2: Rate limit check & correlation probe
        is_rate_limited = (now < rate_limited_until)
        spot_target = (args.min_usdp + estimated_fee_spread) / args.amount if spot_info else Decimal("1.0020")

        should_fetch_quote = not is_rate_limited
        if is_rate_limited and predicted_out is not None:
            # If in rate-limit cooldown, only probe direct quote when spot rate predicts target is in reach
            if predicted_out >= (args.min_usdp - Decimal("0.5")):
                should_fetch_quote = True
                logger.info("🎯 Spot correlation triggered quote probe! Spot=%s, Predicted=%s USDP", spot_info.spot_rate, predicted_out)

        if not should_fetch_quote and spot_info is not None:
            diff_from_spot_target = spot_info.spot_rate - spot_target
            logger.info(
                "Rate-Limit Backoff | Spot: %s (Target: %s | %+.6f) | Est Output: %s USDP | Waiting...",
                spot_info.spot_rate.quantize(Decimal("0.000001")),
                spot_target.quantize(Decimal("0.000001")),
                diff_from_spot_target,
                predicted_out.quantize(Decimal("0.01")),
            )
            time.sleep(args.poll_interval)
            continue

        # Step 3: Direct Quote & Execution Check
        try:
            quote_raw = thor_client.get("quote/swap", params)
            quote = parse_quote(quote_raw, args.amount)

            # Calibrate dynamic fee spread from live quote
            if spot_info is not None and spot_info.spot_rate > 0:
                actual_spread = (args.amount * spot_info.spot_rate) - quote.expected_usdp
                if actual_spread > 0:
                    estimated_fee_spread = actual_spread

            yield_bps = (quote.expected_usdp - args.amount) / args.amount * 10000
            diff_from_target = quote.expected_usdp - args.min_usdp

            spot_prefix = f"Spot: {spot_info.spot_rate:.6f} | " if spot_info else ""
            status_str = (
                f"Quote #{poll_count}: {spot_prefix}{args.amount:,.2f} USDC -> {quote.expected_usdp:,.4f} USDP "
                f"({yield_bps:+.1f} bps | {diff_from_target:+.2f} USDP vs target | Slip: {args.slippage_bps} bps) "
                f"| Est Time: {quote.total_swap_seconds}s"
            )

            floor_mode_val = getattr(args, "floor_mode", "2bps")
            buffer_bps_val = getattr(args, "floor_buffer_bps", 2)

            # Decide what, if anything, to send: (USDC amount, its quote, min USDP target).
            trade = None
            if args.resting:
                # Place only while the rate lets slices fill: an order placed into a low rate
                # can sit unfilled and still pay the outbound and refund fees.
                _, interval, quantity = memo_stream_fields(quote.memo)
                fill_rate = None
                if spot_info is not None:
                    fill_rate = slice_fill_rate(
                        spot_info, quote, target_rate, floor_mode_val, buffer_bps_val, affiliate_bps
                    )
                ready = fill_rate is not None and spot_info.spot_rate >= fill_rate
                if ready or time.monotonic() - last_wait_log >= 60:
                    last_wait_log = time.monotonic()
                    logger.info(
                        "%s | Resting order: %s slices of %.2f USDC, one every %s blocks (~%.1f h)",
                        status_str,
                        quantity,
                        args.amount / max(1, quantity),
                        interval,
                        quote.total_swap_seconds / 3600,
                    )
                    if fill_rate is None:
                        logger.info("Resting order waiting: no pool snapshot or reachable fill rate yet.")
                    elif ready:
                        logger.info(
                            "Pool rate %.5f is at or above the slice fill rate %.5f USDP/USDC.",
                            spot_info.spot_rate,
                            fill_rate,
                        )
                    else:
                        logger.info(
                            "Resting order waiting: pool rate %.5f is below the slice fill rate %.5f USDP/USDC.",
                            spot_info.spot_rate,
                            fill_rate,
                        )
                if ready:
                    trade = (args.amount, quote, args.min_usdp)
            elif args.no_capacity_check:
                if quote.expected_usdp >= args.min_usdp:
                    trade = (args.amount, quote, args.min_usdp)
                else:
                    logger.info(status_str)
            elif spot_info is None:
                logger.warning("%s | No pool snapshot, so fill capacity is unknown; not sending.", status_str)
            else:
                refund_fee, gas_usd = current_costs(spot_info)
                plan_args = dict(
                    target_rate=target_rate,
                    floor_mode=floor_mode_val,
                    buffer_bps=buffer_bps_val,
                    affiliate_bps=affiliate_bps,
                    usdc_refund_fee=refund_fee,
                    gas_usd=gas_usd,
                )
                plan = plan_for_quote(spot_info, quote, args.amount, **plan_args)
                logger.info("%s | %s", status_str, describe_plan(plan))
                if plan.net_profit >= args.min_net_profit and plan.deposit != args.amount:
                    # The memo's streaming quantity, vault and expiry depend on the amount: re-quote it.
                    sized_params = dict(params, amount=int(plan.deposit * THORCHAIN_SCALE))
                    sized_quote = parse_quote(thor_client.get("quote/swap", sized_params), plan.deposit)
                    plan = plan_for_quote(spot_info, sized_quote, plan.deposit, exact=True, **plan_args)
                    logger.info("Re-quoted sized deposit: %s", describe_plan(plan))
                    quote = sized_quote
                if plan.net_profit >= args.min_net_profit:
                    trade = (plan.deposit, quote, plan.deposit * target_rate)
                else:
                    logger.info(
                        "Not sending: modeled net %+.2f USD is below --min-net-profit %s USD.",
                        plan.net_profit,
                        args.min_net_profit,
                    )

            if trade is not None:
                trade_amount, quote, trade_min_usdp = trade
                label = "Placing resting order." if args.resting else "🎯 TARGET TRIGGERED!"
                logger.info("%s Depositing %s USDC (min %s USDP).", label, trade_amount, trade_min_usdp)

                # Construct protected memo with protocol limit floor
                protected_memo = build_protected_memo(
                    quote.memo,
                    min_usdp_target=trade_min_usdp,
                    input_amount=trade_amount,
                    floor_mode=floor_mode_val,
                    buffer_bps=buffer_bps_val,
                )
                logger.info("Floor Mode:     %s (limit buffer: +%s bps)", floor_mode_val, buffer_bps_val if floor_mode_val == "2bps" else ("0" if floor_mode_val == "1to1" else "target"))
                logger.info("Original Memo:  %s", quote.memo)
                logger.info("Protected Memo: %s", protected_memo)

                if args.live:
                    # A stop request (SIGTERM) must not land between signing and broadcast;
                    # the webapp's cancel checks whether the process actually exited.
                    previous_sigterm = signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    try:
                        # The wallet can change during a long wait; a short balance reverts on-chain and wastes gas.
                        usdc_now = Decimal(usdc_contract.functions.balanceOf(operator_account.address).call()) / 10**USDC_DECIMALS
                        if usdc_now < trade_amount:
                            logger.error("USDC balance is now %s, below the %s USDC deposit; not sending.", usdc_now, trade_amount)
                            return 1
                        logger.info("Initiating live on-chain deposit execution...")
                        deposit_tx = execute_deposit(
                            w3=w3,
                            operator_account=operator_account,
                            router_address=quote.router,
                            vault_address=quote.inbound_address,
                            usdc_amount=trade_amount,
                            memo=protected_memo,
                            expiry=quote.expiry,
                        )
                    finally:
                        signal.signal(signal.SIGTERM, previous_sigterm)

                    if args.no_monitor:
                        logger.info("Deposit Tx:    %s", deposit_tx)
                        logger.info("--no-monitor specified; not waiting for USDP delivery.")
                        return 0

                    # Monitor outbound and wait for confirmation
                    received = monitor_outbound(
                        w3=w3,
                        operator_address=operator_account.address,
                        initial_usdp_balance=initial_usdp_balance,
                        min_usdp_target=trade_min_usdp,
                        deposit_tx_hash=deposit_tx,
                        thor_client=thor_client,
                        poll_interval=10.0,
                    )

                    logger.info("================================================================")
                    logger.info("               SNIPER EXECUTION COMPLETED!                      ")
                    logger.info("================================================================")
                    logger.info("Deposit Tx:    %s", deposit_tx)
                    logger.info("USDC Deposit:  %s USDC", trade_amount)
                    logger.info("USDP Received: %s USDP (any unfilled USDC is refunded separately)", received)
                    logger.info("================================================================")
                    return 0
                else:
                    logger.info("[DRY-RUN / SIMULATION] Trigger condition satisfied!")
                    logger.info("Would call depositWithExpiry on Router %s", quote.router)
                    logger.info("  Vault:  %s", quote.inbound_address)
                    logger.info("  Asset:  %s (USDC)", USDC_CONTRACT)
                    logger.info("  Amount: %s atomic units (%s USDC)", int(trade_amount * 10**6), trade_amount)
                    logger.info("  Memo:   %s", protected_memo)
                    logger.info("  Expiry: %s", quote.expiry)
                    if args.once or args.resting:
                        return 0

        except SniperError as exc:
            if "429" in str(exc):
                logger.warning("THORNode returned HTTP 429 rate limit. Switching to pool spot correlation tracking for 15s...")
                rate_limited_until = time.monotonic() + 15.0
            else:
                logger.warning("Quote check error: %s", exc)
        except Exception as exc:
            logger.error("Unexpected error during polling: %s", exc, exc_info=True)

        if args.once:
            logger.info("--once specified; exiting.")
            return 0

        time.sleep(args.poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="THORChain USDC -> USDP Sniper Engine")
    parser.add_argument(
        "--amount",
        type=Decimal,
        default=Decimal(os.getenv("THORCHAIN_SNIPER_AMOUNT", "10000")),
        help="USDC input amount (default: 10000, or THORCHAIN_SNIPER_AMOUNT env)",
    )
    parser.add_argument(
        "--min-profit-bps",
        type=Decimal,
        default=Decimal(os.getenv("THORCHAIN_SNIPER_MIN_PROFIT_BPS", "2")),
        help="Minimum profit in basis points over input amount (default: 2 bps = +0.02%% to cover gas in & out, or THORCHAIN_SNIPER_MIN_PROFIT_BPS env)",
    )
    parser.add_argument(
        "--min-usdp",
        type=Decimal,
        default=None,
        help="Explicit minimum USDP required to execute (default: computed from --amount and --min-profit-bps)",
    )
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Quote polling interval in seconds (default: 3)")
    parser.add_argument(
        "--slippage-bps",
        type=int,
        default=int(os.getenv("THORCHAIN_SNIPER_SLIPPAGE_BPS", "1")),
        help="Slippage tolerance in bps for quote (default: 1 bp = 0.01%%, lowest possible)",
    )
    parser.add_argument(
        "--affiliate",
        type=str,
        default=os.getenv("THORCHAIN_AFFILIATE", "symbiosis"),
        help="Affiliate name or address (default: THORCHAIN_AFFILIATE env or symbiosis)",
    )
    parser.add_argument(
        "--affiliate-bps",
        type=int,
        default=int(os.getenv("THORCHAIN_AFFILIATE_BPS", "1")),
        help="Affiliate fee bps (default: THORCHAIN_AFFILIATE_BPS env or 1)",
    )
    parser.add_argument(
        "--floor-mode",
        type=str,
        choices=["2bps", "1to1", "target"],
        default=os.getenv("THORCHAIN_FLOOR_MODE", "2bps"),
        help="Floor protection mode: '2bps' (at least +2 bps over input to cover gas in & out), '1to1' (exact 1:1 parity), or 'target' (strict target profit floor)",
    )
    parser.add_argument(
        "--floor-buffer-bps",
        type=int,
        default=int(os.getenv("THORCHAIN_FLOOR_BUFFER_BPS", "2")),
        help="Floor buffer in basis points over input amount to cover gas costs (default: 2 bps)",
    )
    parser.add_argument("--interval", type=int, default=0, help="Streaming swap interval (default: 0 = auto)")
    parser.add_argument("--quantity", type=int, default=0, help="Streaming swap quantity (default: 0 = auto)")
    parser.add_argument("--destination", type=str, default=None, help="Destination Ethereum address (default: operator address)")
    parser.add_argument("--base-url", type=str, default=DEFAULT_THORNODE_BASE, help="THORNode API base URL")
    parser.add_argument("--rpc-url", type=str, default=None, help="Ethereum RPC URL (default: ETH_RPC_URL)")
    parser.add_argument("--live", action="store_true", help="Enable live on-chain execution")
    parser.add_argument("--confirm-live", type=str, default=None, help="Confirmation string required for live mode (EXECUTE_THOR_SNIPER)")
    parser.add_argument("--once", action="store_true", help="Quote once and exit")
    parser.add_argument(
        "--min-net-profit",
        type=Decimal,
        default=Decimal(os.getenv("THORCHAIN_SNIPER_MIN_NET_PROFIT", "1")),
        help="Only deposit when the modeled profit after outbound fee, refund fee and gas is at least this many USD (default: 1)",
    )
    parser.add_argument(
        "--resting",
        action="store_true",
        help=f"Place one resting order now: the full --amount streams one sub-swap every "
        f"{RESTING_INTERVAL} blocks x {RESTING_QUANTITY} (~24h) under the floor, filling whenever "
        "the rate allows. Skips the fill-capacity trigger; exits once the deposit confirms",
    )
    parser.add_argument(
        "--no-capacity-check",
        action="store_true",
        help="Trigger on THORNode's quote alone and deposit the full --amount. That quote assumes "
        "the pools are traded back between sub-swaps, so it overstates how much fills",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Exit once the deposit confirms instead of waiting for USDP delivery",
    )

    args = parser.parse_args()
    return run_sniper(args)


if __name__ == "__main__":
    sys.exit(main())
