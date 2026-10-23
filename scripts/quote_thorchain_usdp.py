#!/usr/bin/env python3
"""Read-only native Ethereum USDC -> Paxos USDP quotes (Python stdlib only).

No .env, keys, wallet connection, approval, signing, or broadcast code is used.
THORNode's expected_amount_out ALREADY deducts its liquidity, affiliate and
outbound fees. Slippage is a scenario, not another fee to deduct twice.

Examples:
    python3 scripts/quote_thorchain_usdp.py
    python3 scripts/quote_thorchain_usdp.py --max-seconds 600
    python3 scripts/quote_thorchain_usdp.py --affiliate symbiosis --affiliate-bps 1
    python3 scripts/quote_thorchain_usdp.py --gas-cost-usdc 2 --slippage-bps 10
    python3 scripts/quote_thorchain_usdp.py --json --output /tmp/usdp-quotes.json

Gas is priced using live Ethereum RPC eth_gasPrice, not THORNode's gas hint.
Swap gas units remain an assumption (150k). With --owner, USDC allowance for
the quoted router is checked: sufficient allowance means zero approval gas.
Without an owner, a 50k approval budget is conservatively assumed unless you
override it. The script never submits an approval or a swap transaction.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

BASE_URL = "https://gateway.liquify.com/chain/thorchain_api/thorchain"
RPC_URL = "https://ethereum.publicnode.com"
USDC = "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
USDP = "ETH.USDP-0X8E870D67F660D95D5BE530380D0EC0BD388289E1"
USDC_CONTRACT = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
APPROVAL_GAS_BUDGET = 50000
SCALE = 100_000_000  # THORNode units, for ALL quote assets.
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
NAME_RE = re.compile(r"[A-Za-z0-9_+\-]{1,30}\Z")


class QuoteError(RuntimeError):
    pass


def decimal_arg(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a finite non-negative number") from exc
    if not result.is_finite() or result < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return result


def amount_raw(amount: Decimal) -> int:
    if not amount.is_finite() or amount <= 0:
        raise QuoteError("USDC amount must be positive and finite")
    with localcontext() as ctx:
        ctx.prec = max(50, len(amount.as_tuple().digits) + abs(amount.as_tuple().exponent) + 10)
        if amount * 1_000_000 != (amount * 1_000_000).to_integral_value():
            raise QuoteError("Ethereum USDC supports at most six decimal places")
        return int(amount * SCALE)


def uint(value: Any, label: str) -> int:
    # Reject floats, booleans, signs and scientific notation from integer fields.
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise QuoteError(f"invalid or missing {label}")
    return int(value)


def units(raw: int) -> Decimal:
    return Decimal(raw) / SCALE


def number(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


class ThorClient:
    def __init__(self, base_url: str, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.last_quote_start = float("-inf")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + "/" + path.lstrip("/")
        if params:
            url += "?" + urlencode(params)
        for attempt in range(3):
            if path.startswith("quote/"):
                delay = 1.1 - (time.monotonic() - self.last_quote_start)
                if delay > 0:
                    time.sleep(delay)
                self.last_quote_start = time.monotonic()
            try:
                request = Request(url, headers={"User-Agent": "arbbot-usdp-quoter/1.0", "Accept": "application/json"})
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.load(response, parse_float=Decimal)
                if isinstance(data, dict) and data.get("error"):
                    raise QuoteError(str(data["error"])[:300])
                return data
            except HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.1 * (attempt + 1))
                    continue
                # Server errors can include useful minimum-size or price-limit errors.
                detail = exc.read(1024).decode("utf-8", errors="replace")
                raise QuoteError(f"THORNode HTTP {exc.code}: {detail[:300]}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    time.sleep(1.1 * (attempt + 1))
                    continue
                raise QuoteError(f"THORNode connection failed: {type(exc).__name__}") from exc
            except (ValueError, InvalidOperation) as exc:
                raise QuoteError("THORNode returned invalid JSON") from exc
        raise QuoteError("THORNode request failed")


class EthereumRpc:
    """Read-only mainnet RPC; allowance cache lives for one quote comparison."""

    def __init__(self, url: str, timeout: float = 15):
        self.url = url
        self.timeout = timeout
        self.mainnet_checked = False
        self.request_id = 0
        self.allowances: dict[tuple[str, str], int] = {}

    def call(self, method: str, params: list[Any]) -> Any:
        # Deliberately restrict the transport to read-only methods.
        if method not in ("eth_chainId", "eth_gasPrice", "eth_call"):
            raise QuoteError("RPC method is not permitted by this read-only quoter")
        self.request_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}).encode()
        request = Request(self.url, data=payload, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "arbbot-usdp-quoter/1.0",
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise QuoteError(f"Ethereum RPC {method} HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise QuoteError(f"Ethereum RPC {method} failed: {type(exc).__name__}") from exc
        if not isinstance(data, dict) or data.get("id") != self.request_id or data.get("error") or "result" not in data:
            raise QuoteError(f"Ethereum RPC {method} returned an invalid or error response")
        return data["result"]

    def check_mainnet(self) -> None:
        if not self.mainnet_checked:
            if self.call("eth_chainId", []) != "0x1":
                raise QuoteError("allowance and gas RPC must be Ethereum mainnet (chain ID 1)")
            self.mainnet_checked = True

    def gas_price_gwei(self) -> Decimal:
        self.check_mainnet()
        value = self.call("eth_gasPrice", [])
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
            raise QuoteError("invalid eth_gasPrice result")
        return Decimal(int(value, 16)) / Decimal(1_000_000_000)

    def allowance(self, owner: str, spender: str) -> int:
        if not ADDRESS_RE.fullmatch(owner) or not ADDRESS_RE.fullmatch(spender):
            raise QuoteError("allowance requires valid owner and router addresses")
        self.check_mainnet()
        key = (owner.lower(), spender.lower())
        if key not in self.allowances:
            calldata = "0xdd62ed3e" + owner[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
            result = self.call("eth_call", [{"to": USDC_CONTRACT, "data": calldata}, "latest"])
            if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
                raise QuoteError("invalid USDC allowance response")
            self.allowances[key] = int(result, 16)
        return self.allowances[key]


def approval_budget(response: dict[str, Any], args: argparse.Namespace, rpc: EthereumRpc | None) -> dict[str, Any]:
    """Budget approval only if needed, or conservatively when it is unknown."""
    if args.approval_gas_units is not None:
        return {"gas_units": args.approval_gas_units, "status": "user_override", "allowance_usdc_raw": None}
    if not args.owner:
        return {"gas_units": APPROVAL_GAS_BUDGET, "status": "unknown_no_owner", "allowance_usdc_raw": None}
    try:
        router = response.get("router", "")
        if rpc is None:
            raise QuoteError("allowance RPC unavailable")
        allowance = rpc.allowance(args.owner, router)
        required = amount_raw(args.amount) // 100  # THORNode 1e8 -> USDC 1e6.
        sufficient = allowance >= required
        return {
            "gas_units": 0 if sufficient else APPROVAL_GAS_BUDGET,
            "status": "sufficient" if sufficient else "insufficient",
            "allowance_usdc_raw": str(allowance), "required_usdc_raw": str(required),
            "router": router,
        }
    except QuoteError as exc:
        return {"gas_units": APPROVAL_GAS_BUDGET, "status": "unknown_rpc_error", "allowance_usdc_raw": None, "error": str(exc)}


def eth_usdc_from_pools(pools: list[dict[str, Any]]) -> Decimal:
    by_asset = {p["asset"].upper(): p for p in pools}
    eth, usdc = by_asset.get("ETH.ETH"), by_asset.get(USDC)
    if not eth or not usdc:
        raise QuoteError("ETH and USDC pools needed for the gas conversion are missing")
    # All balances here use THORChain 1e8 units, so scales cancel.
    er = uint(eth.get("balance_rune"), "ETH RUNE balance")
    ea = uint(eth.get("balance_asset"), "ETH balance")
    ur = uint(usdc.get("balance_rune"), "USDC RUNE balance")
    ua = uint(usdc.get("balance_asset"), "USDC balance")
    if min(er, ea, ur, ua) == 0:
        raise QuoteError("cannot value gas from an empty pool")
    return Decimal(er) * ua / (Decimal(ea) * ur)


def validate_pools(pools: Any, mimir: dict[str, Any]) -> None:
    if not isinstance(pools, list) or any(not isinstance(p, dict) or "asset" not in p for p in pools):
        raise QuoteError("invalid pool response")
    by_asset = {p["asset"].upper(): p for p in pools}
    for asset in (USDC, USDP):
        pool = by_asset.get(asset)
        if not pool or pool.get("status") != "Available" or pool.get("trading_halted") is True:
            raise QuoteError(f"pool unavailable or trading halted: {asset}")
    for key in ("HALTTRADING", "HALTETHTRADING", "HALTETHCHAIN", "HALTETHSIGNING", "HALTSIGNING"):
        if int(mimir.get(key, 0)) > 0:
            raise QuoteError(f"network reports {key}={mimir[key]}; refusing a best-route recommendation")


def quote_params(args: argparse.Namespace, affiliate: str | None, quantity: int) -> dict[str, Any]:
    params: dict[str, Any] = {
        "from_asset": USDC,
        "to_asset": USDP,
        "amount": amount_raw(args.amount),
        "streaming_interval": args.interval,
        "streaming_quantity": quantity,
        "liquidity_tolerance_bps": args.slippage_bps,
    }
    if affiliate:
        params.update(affiliate=affiliate, affiliate_bps=args.affiliate_bps)
    if args.destination:
        params["destination"] = args.destination
    return params


def memo_target(memo: Any) -> int | None:
    if not memo:
        return None
    fields = str(memo).split(":")
    if len(fields) < 4:
        raise QuoteError("malformed quote memo")
    target = fields[3].split("/", 1)[0]
    if not target:
        return None
    try:
        parsed = Decimal(target)
    except InvalidOperation as exc:
        raise QuoteError("invalid quote memo trade target") from exc
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise QuoteError("invalid quote memo trade target")
    return int(parsed)


def summarize_quote(
    response: dict[str, Any], params: dict[str, Any], args: argparse.Namespace,
    eth_usdc: Decimal | None, rpc_gas_gwei: Decimal | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise QuoteError("invalid swap quote response")
    expected_raw = uint(response.get("expected_amount_out"), "expected_amount_out")
    if expected_raw == 0:
        raise QuoteError("quote has zero output")
    fees = response.get("fees")
    if not isinstance(fees, dict) or fees.get("asset", "").upper() != USDP:
        raise QuoteError("fee asset must be native Ethereum Paxos USDP")
    fee_raw = {k: uint(fees.get(k), f"fees.{k}") for k in ("liquidity", "affiliate", "outbound", "total")}
    if sum(fee_raw[k] for k in ("liquidity", "affiliate", "outbound")) != fee_raw["total"]:
        raise QuoteError("quote fee components do not add up to total")
    if params.get("affiliate_bps", 0) and fee_raw["affiliate"] == 0:
        raise QuoteError("requested affiliate was not charged; discounted route not validated")
    expiry = uint(response.get("expiry"), "expiry")
    if expiry <= time.time():
        raise QuoteError("quote is already expired")
    minimum_input = response.get("recommended_min_amount_in")
    if minimum_input not in (None, "") and params["amount"] < uint(minimum_input, "recommended_min_amount_in"):
        raise QuoteError("input is below the network's recommended minimum")

    expected = units(expected_raw)
    # This is the documented liquidity_tolerance_bps quote-target formula. It is
    # a scenario for a full fill, NOT a guarantee of native tokens delivered.
    stress_raw = expected_raw * (10000 - args.slippage_bps) // 10000
    stress = units(stress_raw)
    target = memo_target(response.get("memo"))
    warnings: list[str] = []
    if target is not None and abs(target - stress_raw) > 1:
        warnings.append("server memo target differs from the modeled slippage scenario; inspect raw quote")

    approval = approval or approval_budget(response, args, None)
    gas_gwei = args.gas_price_gwei if args.gas_price_gwei is not None else rpc_gas_gwei
    gas_cost = args.gas_cost_usdc
    gas_source = "user_total_override" if gas_cost is not None else "rpc_gas_price"
    if args.gas_price_gwei is not None and gas_cost is None:
        gas_source = "user_gas_price_override"
    if gas_cost is None:
        if gas_gwei is None:
            gas_source = "rpc_unavailable"
            warnings.append("RPC gas price unavailable; gas/net are unknown. No THORNode gas-price fallback was used.")
        elif eth_usdc is None:
            gas_source = "conversion_unavailable"
            warnings.append("ETH/USDC conversion unavailable; gas/net are unknown")
        else:
            gas_cost = Decimal(args.swap_gas_units + approval["gas_units"]) * gas_gwei * Decimal("0.000000001") * eth_usdc
            warnings.append("gas units are a budget, not eth_estimateGas; gas price and actual gas used may change before submission")
    if approval["status"].startswith("unknown") and args.gas_cost_usdc is None:
        warnings.append("approval allowance unknown; conservatively budgeting approval gas (use --owner to check it)")

    total_input = args.amount + gas_cost if gas_cost is not None else None
    # USDC gas cannot be subtracted directly from USDP token delivery. Report a
    # separately labeled USDC parity valuation and an exit break-even ratio.
    result = {
        "profile": params.get("affiliate", "direct") + (f"/q{params['streaming_quantity']}" if params["streaming_quantity"] else "/auto"),
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_usdc": number(args.amount),
        "expected_received_usdp": number(expected),
        "fees_usdp": {k: number(units(v)) for k, v in fee_raw.items()},
        "protocol_reported_slip_bps": fees.get("slippage_bps"),
        "requested_slippage_bps": args.slippage_bps,
        "slippage_scenario_received_usdp": number(stress),
        "server_memo_trade_target_usdp": number(units(target)) if target is not None else None,
        "sender_gas_cost_usdc": number(gas_cost),
        "sender_gas_model": {
            "source": gas_source,
            "swap_gas_units_assumed": args.swap_gas_units if gas_source != "user_total_override" else None,
            "approval_gas_units_assumed": approval["gas_units"] if gas_source != "user_total_override" else None,
            "approval": approval,
            "gas_price_gwei": number(gas_gwei),
            "thorchain_recommended_gas_rate_unused": response.get("recommended_gas_rate"),
            "eth_price_usdc": number(eth_usdc),
        },
        "total_input_cost_usdc_estimated": number(total_input),
        "expected_parity_surplus_usdc_estimated": number(expected - total_input) if total_input is not None else None,
        "slippage_scenario_parity_surplus_usdc_estimated": number(stress - total_input) if total_input is not None else None,
        "expected_break_even_exit_usdc_per_usdp": number(total_input / expected) if total_input is not None else None,
        "slippage_scenario_break_even_exit_usdc_per_usdp": number(total_input / stress) if stress and total_input is not None else None,
        "estimated_seconds": uint(response.get("total_swap_seconds"), "total_swap_seconds"),
        "max_streaming_quantity": uint(response.get("max_streaming_quantity"), "max_streaming_quantity"),
        "expiry_unix": expiry,
        "request": params,
        "raw_quote": response,
        "warnings": warnings,
    }
    return result


def choose_best(rows: list[dict[str, Any]], max_seconds: int | None, now: float | None = None) -> dict[str, Any] | None:
    current_time = time.time() if now is None else now
    eligible = [r for r in rows if r["expiry_unix"] > current_time
                and (max_seconds is None or r["estimated_seconds"] <= max_seconds)
                and r["slippage_scenario_break_even_exit_usdc_per_usdp"] is not None]
    return min(eligible, key=lambda r: Decimal(r["slippage_scenario_break_even_exit_usdc_per_usdp"])) if eligible else None


def collect(args: argparse.Namespace, client: ThorClient, rpc: EthereumRpc | None = None) -> dict[str, Any]:
    mimir = client.get("mimir")
    pools = client.get("pools")
    if not isinstance(mimir, dict):
        raise QuoteError("invalid Mimir response")
    validate_pools(pools, mimir)
    gas_warning = None
    eth_usdc = args.eth_usdc
    if eth_usdc is None:
        try:
            eth_usdc = eth_usdc_from_pools(pools)
        except QuoteError as exc:
            if args.gas_cost_usdc is None:
                raise
            gas_warning = str(exc)
    if getattr(args, "all_routes", False) or (args.affiliate and args.affiliate.lower() == "all"):
        affiliates = [None]
        if int(mimir.get("L1DYNAMICFEEENABLED", 0)) == 1:
            affiliates.extend(name for name in ("ss", "ej", "symbiosis")
                              if int(mimir.get("DYNAMICFEE-WHITELIST-" + name.upper(), 0)) == 1)
    elif args.affiliate is not None:
        affiliates = [None] if args.affiliate.lower() in ("none", "direct") else [args.affiliate]
    else:
        affiliates = ["symbiosis"]
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    rpc_gas_gwei = None
    rpc_gas_observed_at = None
    if (args.gas_cost_usdc is None and args.gas_price_gwei is None) or (args.owner and args.approval_gas_units is None):
        rpc = rpc or EthereumRpc(args.rpc_url, args.timeout)
    if args.gas_cost_usdc is None and args.gas_price_gwei is None:
        try:
            rpc_gas_gwei = rpc.gas_price_gwei() if rpc else None
            rpc_gas_observed_at = datetime.now(timezone.utc).isoformat()
        except QuoteError as exc:
            errors.append({"profile": "rpc_gas_price", "error": str(exc)})

    def fetch(affiliate: str | None, quantity: int) -> None:
        profile = affiliate or "direct"
        print(f"Quoting {profile}, quantity={quantity or 'auto'}...", file=sys.stderr)
        params = quote_params(args, affiliate, quantity)
        try:
            response = client.get("quote/swap", params)
            if not isinstance(response, dict):
                raise QuoteError("invalid swap quote response")
            approval = approval_budget(response, args, rpc)
            row = summarize_quote(response, params, args, eth_usdc, rpc_gas_gwei, approval)
            row["dynamic_affiliate_enrolled"] = bool(affiliate and int(mimir.get("DYNAMICFEE-WHITELIST-" + affiliate.upper(), 0)) == 1)
            rows.append(row)
        except QuoteError as exc:
            errors.append({"profile": f"{profile}/q{quantity}", "error": str(exc)})

    for affiliate in affiliates:
        fetch(affiliate, args.quantity)
    # Compare the best automatic affiliate with the direct route's sub-swap
    # budget too. Fewer sub-swaps can buy time at the expense of price impact.
    direct = next((r for r in rows if not r["request"].get("affiliate")), None)
    discounted = [r for r in rows if r["request"].get("affiliate")]
    if not args.no_fast_comparison and args.quantity == 0 and direct and discounted:
        best_affiliate = choose_best(discounted, None)
        count = direct["max_streaming_quantity"]
        if best_affiliate and 0 < count < best_affiliate["max_streaming_quantity"]:
            fetch(best_affiliate["request"]["affiliate"], count)
    best = choose_best(rows, args.max_seconds)
    notices = [
        "Read-only indicative quotes, not signed transactions or guaranteed fills.",
        "Expected USDP already includes quoted liquidity, affiliate and outbound deductions.",
        "Slippage scenario assumes a full fill; partial fills/refunds and changing outbound costs can change delivery.",
        "Parity surplus assumes 1 USDP can ultimately realize 1 USDC; Stable.com conversion and exit sale were not quoted.",
        "Rapid-streaming ETAs are best-case estimates, not deadlines.",
    ]
    if gas_warning:
        notices.append(gas_warning)
    if len(rows) > 1:
        notices.append("Routes were requested sequentially; changing pool state also affects their comparison.")
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "quote_endpoint": client.base_url,
        "input_usdc": number(args.amount),
        "slippage_bps": args.slippage_bps,
        "rpc_gas_price_gwei": number(rpc_gas_gwei),
        "rpc_gas_observed_at_utc": rpc_gas_observed_at,
        "eth_price_usdc_pool_implied": number(eth_usdc),
        "max_seconds_filter": args.max_seconds,
        "best_profile": best["profile"] if best else None,
        "ranking": "lowest estimated slippage-scenario break-even exit USDC per USDP, within ETA filter",
        "routes": rows,
        "errors": errors,
        "network_fee_settings": {k: v for k, v in mimir.items() if any(part in k.upper() for part in ("SLIPMINBPS", "DYNAMICFEE", "ADVSWAPQUEUE"))},
        "notices": notices,
    }


def pretty(value: str | None, places: int = 4, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    return format(Decimal(value), f"{'+' if signed else ''},.{places}f")


def print_report(report: dict[str, Any]) -> None:
    print(f"\n{pretty(report['input_usdc'], 2)} USDC -> Ethereum Paxos USDP")
    print(f"Slippage scenario: {report['slippage_bps']} bps; fees already included in output.")
    print(f"{'Route':<20} {'Fee USDP':>10} {'Expected USDP':>15} {'Slip-case USDP':>15} {'Gas USDC*':>11} {'ETA':>8}")
    for row in report["routes"]:
        eta = f"{row['estimated_seconds'] // 60}m{row['estimated_seconds'] % 60:02d}s"
        print(f"{row['profile']:<20} {pretty(row['fees_usdp']['total']):>10} "
              f"{pretty(row['expected_received_usdp']):>15} {pretty(row['slippage_scenario_received_usdp']):>15} "
              f"{pretty(row['sender_gas_cost_usdc']):>11} {eta:>8}")
    best = next((r for r in report["routes"] if r["profile"] == report["best_profile"]), None)
    if best:
        print(f"\nBest within ETA filter: {best['profile']}")
        fee = best["fees_usdp"]
        print(f"Embedded fees: liquidity {pretty(fee['liquidity'])} + affiliate {pretty(fee['affiliate'])} "
              f"+ outbound {pretty(fee['outbound'])} = {pretty(fee['total'])} USDP")
        print(f"Expected parity surplus after estimated sender gas*: {pretty(best['expected_parity_surplus_usdc_estimated'], signed=True)} USDC")
        print(f"Slippage-case parity surplus after estimated gas*:  {pretty(best['slippage_scenario_parity_surplus_usdc_estimated'], signed=True)} USDC")
        print(f"Slippage-case break-even exit: {pretty(best['slippage_scenario_break_even_exit_usdc_per_usdp'], 8)} USDC per USDP")
        gas = best["sender_gas_model"]
        if gas["source"] != "user_total_override":
            print(f"*Gas assumption: {gas['swap_gas_units_assumed']} swap + {gas['approval_gas_units_assumed']} approval units, "
                  f"{gas['gas_price_gwei']} gwei, ETH={pretty(gas['eth_price_usdc'], 2)} USDC. Actual cost can differ.")
            print(f" Gas price source: {gas['source']}; approval allowance: {gas['approval']['status']}.")
        else:
            print("*Sender gas uses your --gas-cost-usdc override.")
    else:
        print("\nNo fully costed, unexpired quote meets the requested ETA filter (check RPC gas availability).")
    for error in report["errors"]:
        print(f"Unavailable: {error['profile']}: {error['error']}")
    print("Partial fills/refunds can change delivery. This does not quote Stable.com or the exit sale.")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--amount", type=decimal_arg, default=Decimal("10000"), help="input USDC, default 10000")
    p.add_argument("--slippage-bps", type=int, default=10, help="full-fill output haircut / liquidity tolerance, 1..9999; default 10 (0.1%%)")
    p.add_argument("--affiliate", default="symbiosis", help="quote this THORName (default: symbiosis); 'direct' means zero affiliate; 'all' compares all routes")
    p.add_argument("--all-routes", action="store_true", help="compare all enrolled affiliate routes (direct, ss, ej, symbiosis)")
    p.add_argument("--affiliate-bps", type=int, default=1, help="fee paid to selected affiliate, default 1 bp")
    p.add_argument("--quantity", type=int, default=0, help="sub-swap count; 0 requests the network maximum (default)")
    p.add_argument("--interval", type=int, default=2, help="THORChain blocks between sub-swaps; default 2; 0 requests rapid streaming")
    p.add_argument("--max-seconds", type=int, help="ETA ceiling when selecting best; does not guarantee completion time")
    p.add_argument("--no-fast-comparison", action="store_true", help="skip the extra fixed-count comparison")
    p.add_argument("--gas-cost-usdc", type=decimal_arg, help="override total sender-side gas cost in USDC")
    p.add_argument("--swap-gas-units", type=int, default=150000, help="assumed router deposit gas, default 150000")
    p.add_argument("--approval-gas-units", type=int, default=None, help="override approval budget; default checks --owner allowance, or assumes 50000 if unknown")
    p.add_argument("--gas-price-gwei", type=decimal_arg, help="override live RPC eth_gasPrice")
    p.add_argument("--owner", help="public Ethereum address holding USDC; enables allowance checks for each quoted router")
    p.add_argument("--rpc-url", default=RPC_URL, help="Ethereum mainnet RPC for live gas and read-only USDC allowance checks")
    p.add_argument("--eth-usdc", type=decimal_arg, help="override pool-implied ETH price for gas conversion")
    p.add_argument("--destination", help="optional public Ethereum recipient; enables server memo target reporting")
    p.add_argument("--base-url", default=BASE_URL, help="public THORNode API base URL")
    p.add_argument("--timeout", type=float, default=15, help="per-request socket timeout in seconds")
    p.add_argument("--json", action="store_true", help="print the complete report as JSON")
    p.add_argument("--output", type=Path, help="save the complete JSON report to this file")
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    try:
        amount_raw(args.amount)
        if not 0 < args.slippage_bps < 10000 or not 0 <= args.affiliate_bps <= 1000:
            raise QuoteError("slippage must be 1..9999 bps (0 disables the protocol limit); affiliate fee must be 0..1000 bps")
        if min(args.quantity, args.interval, args.swap_gas_units, args.approval_gas_units or 0) < 0:
            raise QuoteError("streaming and gas counts cannot be negative")
        if args.max_seconds is not None and args.max_seconds <= 0:
            raise QuoteError("--max-seconds must be positive")
        if not 0 < args.timeout <= 60:
            raise QuoteError("--timeout must be between 0 and 60 seconds")
        if args.eth_usdc is not None and args.eth_usdc <= 0:
            raise QuoteError("--eth-usdc must be positive")
        if args.destination and (not ADDRESS_RE.fullmatch(args.destination) or int(args.destination, 16) == 0):
            raise QuoteError("--destination must be a nonzero public Ethereum address")
        if args.owner and (not ADDRESS_RE.fullmatch(args.owner) or int(args.owner, 16) == 0):
            raise QuoteError("--owner must be a nonzero public Ethereum address")
        if args.affiliate and args.affiliate.lower() not in ("direct", "none", "all") and not NAME_RE.fullmatch(args.affiliate):
            raise QuoteError("--affiliate must be one THORName")
        parsed_url = urlsplit(args.base_url)
        if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise QuoteError("--base-url must be an HTTPS API URL without credentials, query, or fragment")
        parsed_rpc = urlsplit(args.rpc_url)
        if (parsed_rpc.scheme != "https" and not (parsed_rpc.scheme == "http" and parsed_rpc.hostname in ("localhost", "127.0.0.1", "::1"))) or not parsed_rpc.hostname or parsed_rpc.username or parsed_rpc.password or parsed_rpc.fragment:
            raise QuoteError("--rpc-url must use HTTPS (or localhost HTTP), without embedded user/password or fragment")
        report = collect(args, ThorClient(args.base_url, args.timeout))
        encoded = json.dumps(report, indent=2, default=str) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded)
        if args.json:
            print(encoded, end="")
        else:
            print_report(report)
        return 0 if report["best_profile"] else 2
    except (QuoteError, OSError) as exc:
        print(f"Quote failed: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
