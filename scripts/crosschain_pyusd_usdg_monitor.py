#!/usr/bin/env python3
"""Quote-only monitor for an Ethereum -> Solana -> Ethereum stablecoin cycle.

Route inspected:

    PYUSD (Ethereum)
      -> USDG (Solana, Stable.com cross-chain quote)
      -> USDG (Ethereum, canonical LayerZero OFT at 1:1 token units)
      -> PYUSD (Ethereum, MetaMatcha quote)

This program never requests Stable.com orders, never requests LayerZero transfer
calldata, never signs, and never broadcasts.  It is deliberately a monitoring
and decision-support tool because the three legs are not atomic and bridge delay
can leave inventory exposed to price and liquidity changes.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable
from urllib.parse import urlencode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # Reported with the other dependencies in main().
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env", override=True)

try:
    from solders.pubkey import Pubkey
except ImportError:  # Allows --help to work before dependencies are installed.
    Pubkey = None  # type: ignore[assignment]

from src.engines.eth_flash_arb_pyusd_usdc import (
    ArbError,
    BINANCE_ETH_PRICE_URL,
    HttpJsonClient,
    MATCHA_AGGREGATORS,
    MatchaClient,
    PYUSD,
    USDG,
    amount_to_raw,
    first_key,
    parse_binance_eth_usdc_price,
    raw_to_amount,
    select_best_matcha_quote,
)


DECIMALS = 6
ETHEREUM_STABLE_CHAIN_ID = "101"
SOLANA_STABLE_CHAIN_ID = "102"
SOLANA_USDG_MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
STABLE_SOLANA_PROGRAM = "2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
LAYERZERO_OFT_API = "https://metadata.layerzero-api.com/v1/metadata/experiment/ofts"
ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class MonitorError(RuntimeError):
    """A provider or validation failure that is safe to retry."""


@dataclass(frozen=True)
class StableCrosschainQuote:
    amount_in_raw: int
    amount_out_raw: int
    reported_capacity: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    native_fee_usd: Decimal
    execution_fee_usd: Decimal
    token_fee_raw: int

    @property
    def known_fee_usd(self) -> Decimal:
        return self.native_fee_usd + self.execution_fee_usd


@dataclass(frozen=True)
class OftDeployment:
    solana_address: str
    ethereum_adapter: str
    ethereum_token: str
    shared_decimals: int


@dataclass(frozen=True)
class RouteSnapshot:
    observed_at: str
    state: str
    reason: str
    solana_usdg_reserve: str
    input_pyusd: str
    stable_output_usdg: str | None = None
    stable_reported_capacity: str | None = None
    bridged_usdg: str | None = None
    matcha_output_pyusd: str | None = None
    matcha_aggregator: str | None = None
    gross_profit_pyusd: str | None = None
    stable_known_fee_usd: str | None = None
    matcha_gas_fee_usd: str | None = None
    bridge_fee_usd: str | None = None
    estimated_net_pyusd: str | None = None


def decimal_value(value: Any, label: str, *, allow_zero: bool = True) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MonitorError(f"invalid {label}: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise MonitorError(f"invalid {label}: {value!r}")
    return parsed


def optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def decimal_to_raw_floor(value: Any, label: str) -> int:
    parsed = decimal_value(value, label)
    return int(
        (parsed * (Decimal(10) ** DECIMALS)).to_integral_value(rounding=ROUND_DOWN)
    )


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001')):f}"


def raw_decimal(value: int) -> Decimal:
    return Decimal(value) / (Decimal(10) ** DECIMALS)


def validate_public_addresses(ethereum: str | None, solana: str | None) -> tuple[str, str]:
    if not ethereum or not ETH_ADDRESS_RE.fullmatch(ethereum):
        raise MonitorError(
            "set --eth-address or CROSSCHAIN_ETH_ADDRESS to a public Ethereum address"
        )
    if not solana:
        raise MonitorError(
            "set --solana-address or CROSSCHAIN_SOLANA_ADDRESS to a public Solana address"
        )
    if Pubkey is None:
        raise MonitorError("solders is required; install requirements.txt")
    try:
        Pubkey.from_string(solana)
    except Exception as exc:
        raise MonitorError("the Solana public address is invalid") from exc
    return ethereum, solana


def stable_pool_usdg_ata() -> str:
    if Pubkey is None:
        raise MonitorError("solders is required; install requirements.txt")
    mint = Pubkey.from_string(SOLANA_USDG_MINT)
    stable_program = Pubkey.from_string(STABLE_SOLANA_PROGRAM)
    token_program = Pubkey.from_string(TOKEN_2022_PROGRAM)
    associated_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM)
    pool, _ = Pubkey.find_program_address([b"pool", bytes(mint)], stable_program)
    pool_ata, _ = Pubkey.find_program_address(
        [bytes(pool), bytes(token_program), bytes(mint)], associated_program
    )
    return str(pool_ata)


def solana_usdg_reserve(http: HttpJsonClient, rpc_url: str) -> Decimal:
    response = http.post(
        rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountBalance",
            "params": [stable_pool_usdg_ata(), {"commitment": "confirmed"}],
        },
    )
    error = response.get("error") if isinstance(response, dict) else None
    if error:
        raise MonitorError(f"Solana RPC rejected the reserve query: {error}")
    amount = first_key(response, ("amount",))
    decimals = first_key(response, ("decimals",))
    try:
        raw = int(amount)
        precision = int(decimals)
    except (TypeError, ValueError) as exc:
        raise MonitorError("Solana RPC omitted the USDG token balance") from exc
    if raw < 0 or precision < 0:
        raise MonitorError("Solana RPC returned an invalid USDG token balance")
    return Decimal(raw) / (Decimal(10) ** precision)


def stable_headers() -> dict[str, str]:
    return {
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://stable.com",
        "referer": "https://stable.com/",
    }


def build_stable_quote_payload(
    ethereum_address: str,
    solana_address: str,
    amount_raw: int,
) -> dict[str, Any]:
    return {
        "chainFrom": ETHEREUM_STABLE_CHAIN_ID,
        "assetFrom": "PYUSD",
        "chainTo": SOLANA_STABLE_CHAIN_ID,
        "assetTo": "USDG",
        "amountFrom": raw_to_amount(amount_raw),
        "addressFrom": ethereum_address,
        "addressTo": solana_address,
        "gasLess": False,
    }


def stable_crosschain_quote(
    http: HttpJsonClient,
    base_url: str,
    ethereum_address: str,
    solana_address: str,
    amount_raw: int,
) -> StableCrosschainQuote:
    response = http.post(
        f"{base_url.rstrip('/')}/swap/status",
        build_stable_quote_payload(ethereum_address, solana_address, amount_raw),
        headers=stable_headers(),
    )
    returned_input = first_key(response, ("amountFrom", "amountIn"))
    returned_output = first_key(response, ("amountTo", "amountOut", "outputAmount"))
    if returned_output is None:
        raise MonitorError("Stable.com status response omitted the output amount")
    if returned_input is not None and decimal_to_raw_floor(returned_input, "Stable input") != amount_raw:
        raise MonitorError("Stable.com changed the requested PYUSD input amount")

    token_fee = first_key(response, ("tokenFee", "protocolFee"))
    return StableCrosschainQuote(
        amount_in_raw=amount_raw,
        amount_out_raw=decimal_to_raw_floor(returned_output, "Stable output"),
        reported_capacity=optional_decimal(
            first_key(response, ("available", "capacity", "liquidity", "balance"))
        ),
        minimum=optional_decimal(first_key(response, ("min", "minimum"))),
        maximum=optional_decimal(first_key(response, ("max", "maximum"))),
        native_fee_usd=optional_decimal(first_key(response, ("nativeFeeUsd",)))
        or Decimal(0),
        execution_fee_usd=optional_decimal(first_key(response, ("executionFeeUSD",)))
        or Decimal(0),
        token_fee_raw=(
            decimal_to_raw_floor(token_fee, "Stable token fee")
            if token_fee is not None
            else 0
        ),
    )


def discover_usdg_oft(http: HttpJsonClient, base_url: str) -> OftDeployment:
    query = urlencode({"symbols": "USDG", "chainNames": "ethereum,solana"})
    response = http.get(f"{base_url.rstrip('/')}/list?{query}")
    candidates = response.get("USDG") if isinstance(response, dict) else None
    if not isinstance(candidates, list):
        raise MonitorError("LayerZero metadata did not list USDG")

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        deployments = candidate.get("deployments")
        if not isinstance(deployments, dict):
            continue
        solana = deployments.get("solana")
        ethereum = deployments.get("ethereum")
        if not isinstance(solana, dict) or not isinstance(ethereum, dict):
            continue
        ethereum_token = ethereum.get("innerTokenAddress") or ethereum.get("innerToken")
        if (
            str(solana.get("address")) == SOLANA_USDG_MINT
            and isinstance(ethereum_token, str)
            and ethereum_token.lower() == USDG.lower()
        ):
            try:
                shared_decimals = int(candidate.get("sharedDecimals"))
            except (TypeError, ValueError) as exc:
                raise MonitorError("LayerZero USDG metadata omitted shared decimals") from exc
            if shared_decimals != DECIMALS:
                raise MonitorError(
                    f"LayerZero USDG uses unexpected shared decimals: {shared_decimals}"
                )
            adapter = ethereum.get("address")
            if not isinstance(adapter, str) or not ETH_ADDRESS_RE.fullmatch(adapter):
                raise MonitorError("LayerZero USDG metadata omitted the Ethereum adapter")
            return OftDeployment(
                solana_address=SOLANA_USDG_MINT,
                ethereum_adapter=adapter,
                ethereum_token=ethereum_token,
                shared_decimals=shared_decimals,
            )
    raise MonitorError(
        "LayerZero metadata has no canonical USDG Solana -> Ethereum deployment"
    )


def selected_matcha_quote(
    http: HttpJsonClient,
    ethereum_address: str,
    amount_raw: int,
    slippage_bps: int,
    aggregators: Iterable[str],
    base_url: str,
):
    client = MatchaClient(
        http,
        base_url,
        quote_provider=os.environ.get("ETH_ARB_QUOTE_PROVIDER", "auto"),
        zero_ex_api_key=os.environ.get("ETH_ARB_ZERO_EX_API_KEY")
        or os.environ.get("ZERO_EX_API_KEY"),
        zero_ex_base_url=os.environ.get("ETH_ARB_ZERO_EX_BASE_URL", "https://api.0x.org"),
    )
    responses = client.quotes(
        ethereum_address,
        amount_raw,
        slippage_bps,
        aggregators,
        USDG,
        PYUSD,
    )
    return select_best_matcha_quote(responses, amount_raw)


def eth_usd_price(http: HttpJsonClient, override: str | None) -> Decimal:
    if override:
        return decimal_value(override, "ETH/USD price", allow_zero=False)
    payload = http.get(os.environ.get("ETH_ARB_ETH_PRICE_URL", BINANCE_ETH_PRICE_URL))
    return parse_binance_eth_usdc_price(payload)


def evaluate_route(
    *,
    reserve: Decimal,
    reserve_floor: Decimal,
    amount_in_raw: int,
    stable: StableCrosschainQuote,
    matcha_output_raw: int,
    matcha_gas: int | None,
    matcha_gas_price: int | None,
    eth_price: Decimal,
    bridge_fee_usd: Decimal | None,
    min_net_profit: Decimal,
) -> tuple[str, str, Decimal, Decimal, Decimal | None]:
    amount_in = raw_decimal(amount_in_raw)
    stable_output = raw_decimal(stable.amount_out_raw)
    gross = raw_decimal(matcha_output_raw - amount_in_raw)
    matcha_gas_fee = Decimal(0)
    if matcha_gas is not None and matcha_gas_price is not None:
        matcha_gas_fee = (
            Decimal(matcha_gas) * Decimal(matcha_gas_price) * eth_price / Decimal(10**18)
        )
    known_fees = stable.known_fee_usd + matcha_gas_fee
    estimated_net = (
        gross - known_fees - bridge_fee_usd if bridge_fee_usd is not None else None
    )

    if reserve - stable_output < reserve_floor:
        return (
            "WAIT_RESERVE",
            f"the quote would leave only {money(reserve - stable_output)} USDG in the Stable.com Solana pool",
            gross,
            matcha_gas_fee,
            estimated_net,
        )
    if stable.minimum is not None and amount_in < stable.minimum:
        return (
            "WAIT_MINIMUM",
            f"Stable.com minimum is {stable.minimum} PYUSD",
            gross,
            matcha_gas_fee,
            estimated_net,
        )
    if stable.maximum is not None and amount_in > stable.maximum:
        return (
            "WAIT_MAXIMUM",
            f"Stable.com maximum is {stable.maximum} PYUSD",
            gross,
            matcha_gas_fee,
            estimated_net,
        )
    if stable.reported_capacity is not None and stable_output > stable.reported_capacity:
        return (
            "WAIT_CAPACITY",
            "Stable.com reports less destination capacity than the quoted output",
            gross,
            matcha_gas_fee,
            estimated_net,
        )
    if gross <= 0:
        return "NO_EDGE", "the quoted cycle has no gross spread", gross, matcha_gas_fee, estimated_net
    if bridge_fee_usd is None:
        return (
            "REVIEW_BRIDGE_FEE",
            "gross spread is positive, but the LayerZero native messaging fee is not included",
            gross,
            matcha_gas_fee,
            None,
        )
    if estimated_net is None or estimated_net < min_net_profit:
        return (
            "BELOW_FLOOR",
            f"estimated net is below the {money(min_net_profit)} PYUSD floor",
            gross,
            matcha_gas_fee,
            estimated_net,
        )
    return (
        "MANUAL_REVIEW",
        "quote clears the configured estimate; bridge timing and fresh post-bridge quotes still require review",
        gross,
        matcha_gas_fee,
        estimated_net,
    )


def quote_snapshot(
    *,
    http: HttpJsonClient,
    ethereum_address: str,
    solana_address: str,
    amount_raw: int,
    rpc_url: str,
    stable_base_url: str,
    matcha_base_url: str,
    slippage_bps: int,
    aggregators: Iterable[str],
    reserve_floor: Decimal,
    min_net_profit: Decimal,
    bridge_fee_usd: Decimal | None,
    eth_price_override: str | None,
) -> RouteSnapshot:
    observed_at = datetime.now(timezone.utc).isoformat()
    reserve = solana_usdg_reserve(http, rpc_url)
    stable = stable_crosschain_quote(
        http,
        stable_base_url,
        ethereum_address,
        solana_address,
        amount_raw,
    )
    # The canonical USDG OFT uses six shared decimals on both networks, so the
    # bridged token quantity is identical. Native messaging fees are separate.
    bridged_raw = stable.amount_out_raw
    matcha = selected_matcha_quote(
        http,
        ethereum_address,
        bridged_raw,
        slippage_bps,
        aggregators,
        matcha_base_url,
    )
    price = eth_usd_price(http, eth_price_override)
    state, reason, gross, matcha_fee, estimated_net = evaluate_route(
        reserve=reserve,
        reserve_floor=reserve_floor,
        amount_in_raw=amount_raw,
        stable=stable,
        matcha_output_raw=matcha.buy_amount,
        matcha_gas=matcha.gas,
        matcha_gas_price=matcha.gas_price,
        eth_price=price,
        bridge_fee_usd=bridge_fee_usd,
        min_net_profit=min_net_profit,
    )
    return RouteSnapshot(
        observed_at=observed_at,
        state=state,
        reason=reason,
        solana_usdg_reserve=money(reserve),
        input_pyusd=raw_to_amount(amount_raw),
        stable_output_usdg=raw_to_amount(stable.amount_out_raw),
        stable_reported_capacity=(
            money(stable.reported_capacity)
            if stable.reported_capacity is not None
            else None
        ),
        bridged_usdg=raw_to_amount(bridged_raw),
        matcha_output_pyusd=raw_to_amount(matcha.buy_amount),
        matcha_aggregator=matcha.aggregator,
        gross_profit_pyusd=money(gross),
        stable_known_fee_usd=money(stable.known_fee_usd),
        matcha_gas_fee_usd=money(matcha_fee),
        bridge_fee_usd=money(bridge_fee_usd) if bridge_fee_usd is not None else None,
        estimated_net_pyusd=money(estimated_net) if estimated_net is not None else None,
    )


def format_snapshot(snapshot: RouteSnapshot) -> str:
    route = (
        f"reserve={snapshot.solana_usdg_reserve} USDG | "
        f"{snapshot.input_pyusd} PYUSD(ETH) -> {snapshot.stable_output_usdg} USDG(SOL) "
        f"-> {snapshot.bridged_usdg} USDG(ETH) -> "
        f"{snapshot.matcha_output_pyusd} PYUSD [{snapshot.matcha_aggregator}]"
    )
    economics = (
        f"gross={snapshot.gross_profit_pyusd} | "
        f"stableFees={snapshot.stable_known_fee_usd} USD | "
        f"matchaGas={snapshot.matcha_gas_fee_usd} USD | "
        f"bridgeFee={snapshot.bridge_fee_usd or 'UNKNOWN'}"
    )
    if snapshot.estimated_net_pyusd is not None:
        economics += f" | estimatedNet={snapshot.estimated_net_pyusd} PYUSD"
    return (
        f"{snapshot.observed_at} | {snapshot.state} | {route}\n"
        f"  {economics}\n"
        f"  {snapshot.reason}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Continuously monitor and quote the PYUSD(ETH) -> USDG(SOL) -> "
            "USDG(ETH) -> PYUSD(ETH) route without signing or broadcasting."
        )
    )
    parser.add_argument(
        "--eth-address",
        default=os.environ.get("CROSSCHAIN_ETH_ADDRESS")
        or os.environ.get("ETH_OPERATOR_ADDRESS"),
        help="public Ethereum taker/source address",
    )
    parser.add_argument(
        "--solana-address",
        default=os.environ.get("CROSSCHAIN_SOLANA_ADDRESS"),
        help="public Solana destination/source address",
    )
    parser.add_argument(
        "--amount",
        default=os.environ.get("CROSSCHAIN_AMOUNT_PYUSD", "1000"),
        help="PYUSD amount per quoted cycle (default: 1000)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("CROSSCHAIN_INTERVAL_SECONDS", "10")),
        help="seconds between quote cycles (default: 10)",
    )
    parser.add_argument(
        "--reserve-floor",
        default=os.environ.get("CROSSCHAIN_RESERVE_FLOOR_USDG", "1.90"),
        help="minimum Solana Stable.com USDG reserve left untouched",
    )
    parser.add_argument(
        "--min-net-profit",
        default=os.environ.get("CROSSCHAIN_MIN_NET_PROFIT_PYUSD", "1"),
        help="manual-review threshold after all configured fee estimates",
    )
    parser.add_argument(
        "--bridge-fee-usd",
        default=os.environ.get("CROSSCHAIN_BRIDGE_FEE_USD"),
        help="conservative LayerZero native messaging fee estimate in USD",
    )
    parser.add_argument(
        "--eth-usd",
        default=os.environ.get("ETH_ARB_ETH_USD"),
        help="fixed ETH/USD value; otherwise fetched from ETH_ARB_ETH_PRICE_URL",
    )
    parser.add_argument(
        "--slippage-bps",
        type=int,
        default=int(os.environ.get("CROSSCHAIN_MATCHA_SLIPPAGE_BPS", "10")),
        help="MetaMatcha slippage setting used only for the quote (default: 10)",
    )
    parser.add_argument(
        "--aggregators",
        default=os.environ.get("ETH_ARB_AGGREGATORS", ",".join(MATCHA_AGGREGATORS)),
        help="comma-separated MetaMatcha aggregators",
    )
    parser.add_argument("--once", action="store_true", help="perform one quote cycle and exit")
    parser.add_argument("--json", action="store_true", help="emit one JSON object per cycle")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if load_dotenv is None or Pubkey is None:
        print("ERROR: install dependencies with: pip install -r requirements.txt -r requirements-eth.txt", file=sys.stderr)
        return 2
    try:
        ethereum_address, solana_address = validate_public_addresses(
            args.eth_address, args.solana_address
        )
        amount_raw = amount_to_raw(args.amount)
        reserve_floor = decimal_value(args.reserve_floor, "reserve floor")
        min_net_profit = decimal_value(args.min_net_profit, "minimum net profit")
        bridge_fee_usd = (
            decimal_value(args.bridge_fee_usd, "bridge fee")
            if args.bridge_fee_usd not in (None, "")
            else None
        )
        if not 0 <= args.slippage_bps < 10_000:
            raise MonitorError("slippage must be between 0 and 9999 bps")
        if args.interval <= 0:
            raise MonitorError("interval must be positive")
        aggregators = tuple(
            item.strip() for item in args.aggregators.split(",") if item.strip()
        )
        if not aggregators:
            raise MonitorError("at least one MetaMatcha aggregator is required")

        http = HttpJsonClient(
            timeout=float(os.environ.get("ETH_ARB_HTTP_TIMEOUT_SECONDS", "20")),
            user_agent=os.environ.get("EVM_QUOTE_USER_AGENT", "Mozilla/5.0"),
        )
        # Fail closed if LayerZero no longer identifies these exact canonical
        # USDG deployments. The result is intentionally not transfer calldata.
        discover_usdg_oft(
            http, os.environ.get("LAYERZERO_OFT_API_BASE", LAYERZERO_OFT_API)
        )
    except (ArbError, MonitorError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    stable_base_url = os.environ.get("ETH_ARB_STABLE_BASE_URL", "https://api-defi.stable.com")
    matcha_base_url = os.environ.get("ETH_ARB_MATCHA_BASE_URL", "https://meta.matcha.xyz")

    while True:
        try:
            snapshot = quote_snapshot(
                http=http,
                ethereum_address=ethereum_address,
                solana_address=solana_address,
                amount_raw=amount_raw,
                rpc_url=rpc_url,
                stable_base_url=stable_base_url,
                matcha_base_url=matcha_base_url,
                slippage_bps=args.slippage_bps,
                aggregators=aggregators,
                reserve_floor=reserve_floor,
                min_net_profit=min_net_profit,
                bridge_fee_usd=bridge_fee_usd,
                eth_price_override=args.eth_usd,
            )
            print(json.dumps(asdict(snapshot), separators=(",", ":")) if args.json else format_snapshot(snapshot), flush=True)
        except KeyboardInterrupt:
            return 130
        except (ArbError, MonitorError, ValueError) as exc:
            error = {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "state": "PROVIDER_ERROR",
                "reason": str(exc),
            }
            print(json.dumps(error, separators=(",", ":")) if args.json else f"{error['observed_at']} | PROVIDER_ERROR | {exc}", flush=True)

        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
