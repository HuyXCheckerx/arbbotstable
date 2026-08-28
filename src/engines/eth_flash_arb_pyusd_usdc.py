#!/usr/bin/env python3
"""Build and simulate flash-funded Ethereum stablecoin arbitrage.

The default mode never signs or broadcasts. A deployed
MorphoMatchaStableArbUsdc contract is required because an atomic flash loan cannot
be executed from an EOA or from Python alone.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable
import uuid

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

try:
    import requests
except ImportError:  # Allows helpers and --help to run before dependencies install.
    requests = None

try:
    from dotenv import load_dotenv
except ImportError:  # Reported with the other runtime dependencies when used.
    load_dotenv = None

if load_dotenv is not None:
    # Route configuration belongs to this project's .env. Override stale shell
    # exports so `python3 eth_flash_arb_pyusd_usdc.py` behaves consistently in every shell.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)


CHAIN_ID = 1
STABLE_CHAIN_ID = "101"
DECIMALS = 6
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
PYUSD = "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8"
USDG = "0xe343167631d89B6Ffc58B88d6b7fB0228795491D"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
UNISWAP_V4_POOL_MANAGER = "0x000000000004444c5dc75cB358380D2e3dE08A90"
AAVE_V3_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
AAVE_V3_DATA_PROVIDER = "0x0a16f2FCC0D44FaE41cc54e079281D84A363bECD"
STABLE_POOL = "0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da"
MATCHA_BASE_URL = "https://meta.matcha.xyz"
STABLE_BASE_URL = "https://api-defi.stable.com"
BINANCE_ETH_PRICE_URL = (
    "https://data-api.binance.vision/api/v3/ticker/price?symbol=ETHUSDC"
)
MATCHA_AGGREGATORS = (
    "0x",
    "Lightning",
    "1inch",
    "Barter",
    "Bebop",
    "Bitget",
    "KyberSwap",
    "OKX",
    "ParaSwap",
    "Enso",
)
LOAN_TOKENS = {
    "USDC": USDC,
    "PYUSD": PYUSD,
    "USDG": USDG,
}
FLASH_PROVIDER_IDS = {"morpho": 0, "uniswap-v4": 1, "aave-v3": 2}
FLASH_PROVIDER_ADDRESSES = {
    "morpho": MORPHO,
    "uniswap-v4": UNISWAP_V4_POOL_MANAGER,
    "aave-v3": AAVE_V3_POOL,
}
FLASH_PROVIDER_LABELS = {
    "morpho": "Morpho",
    "uniswap-v4": "Uniswap v4",
    "aave-v3": "Aave v3",
}
AUTO_FLASH_PROVIDER_ORDER = ("morpho", "aave-v3")
MAX_CAPACITY_SIZING_ATTEMPTS = 5

EXECUTOR_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "loanAmount", "type": "uint256"},
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {
                        "internalType": "address",
                        "name": "allowanceTarget",
                        "type": "address",
                    },
                    {
                        "internalType": "uint256",
                        "name": "sellAmount",
                        "type": "uint256",
                    },
                    {"internalType": "uint256", "name": "value", "type": "uint256"},
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.MatchaRoute",
                "name": "matcha",
                "type": "tuple",
            },
            {
                "components": [
                    {
                        "internalType": "uint256",
                        "name": "amountIn",
                        "type": "uint256",
                    },
                    {"internalType": "uint64", "name": "deadline", "type": "uint64"},
                    {"internalType": "uint256", "name": "nonce", "type": "uint256"},
                    {
                        "internalType": "bytes",
                        "name": "maintainerSignature",
                        "type": "bytes",
                    },
                    {
                        "internalType": "uint256",
                        "name": "executionFeeNative",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.StableOrder",
                "name": "stable",
                "type": "tuple",
            },
            {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
        ],
        "name": "executeArbitrageWithLoanToken",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "loanAmount", "type": "uint256"},
            {"internalType": "address", "name": "loanToken", "type": "address"},
            {
                "internalType": "address",
                "name": "intermediateToken",
                "type": "address",
            },
            {
                "internalType": "enum MorphoMatchaStableArbUsdc.FlashProvider",
                "name": "flashProvider",
                "type": "uint8",
            },
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {
                        "internalType": "address",
                        "name": "allowanceTarget",
                        "type": "address",
                    },
                    {"internalType": "uint256", "name": "sellAmount", "type": "uint256"},
                    {"internalType": "uint256", "name": "value", "type": "uint256"},
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.MatchaRoute",
                "name": "matcha",
                "type": "tuple",
            },
            {
                "components": [
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint64", "name": "deadline", "type": "uint64"},
                    {"internalType": "uint256", "name": "nonce", "type": "uint256"},
                    {
                        "internalType": "bytes",
                        "name": "maintainerSignature",
                        "type": "bytes",
                    },
                    {
                        "internalType": "uint256",
                        "name": "executionFeeNative",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.StableOrder",
                "name": "stable",
                "type": "tuple",
            },
            {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
        ],
        "name": "executeArbitrageWithTokensAndProvider",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "loanAmount", "type": "uint256"},
            {"internalType": "address", "name": "loanToken", "type": "address"},
            {
                "internalType": "address",
                "name": "intermediateToken",
                "type": "address",
            },
            {
                "internalType": "enum MorphoMatchaStableArbUsdc.FlashProvider",
                "name": "flashProvider",
                "type": "uint8",
            },
            {
                "internalType": "enum MorphoMatchaStableArbUsdc.SwapOrder",
                "name": "swapOrder",
                "type": "uint8",
            },
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {
                        "internalType": "address",
                        "name": "allowanceTarget",
                        "type": "address",
                    },
                    {"internalType": "uint256", "name": "sellAmount", "type": "uint256"},
                    {"internalType": "uint256", "name": "value", "type": "uint256"},
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.MatchaRoute",
                "name": "matcha",
                "type": "tuple",
            },
            {
                "components": [
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint64", "name": "deadline", "type": "uint64"},
                    {"internalType": "uint256", "name": "nonce", "type": "uint256"},
                    {
                        "internalType": "bytes",
                        "name": "maintainerSignature",
                        "type": "bytes",
                    },
                    {
                        "internalType": "uint256",
                        "name": "executionFeeNative",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.StableOrder",
                "name": "stable",
                "type": "tuple",
            },
            {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
        ],
        "name": "executeArbitrageWithTokensAndProviderAndOrder",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "loanAmount", "type": "uint256"},
            {
                "internalType": "address",
                "name": "loanToken",
                "type": "address",
            },
            {
                "internalType": "address",
                "name": "intermediateToken",
                "type": "address",
            },
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {
                        "internalType": "address",
                        "name": "allowanceTarget",
                        "type": "address",
                    },
                    {
                        "internalType": "uint256",
                        "name": "sellAmount",
                        "type": "uint256",
                    },
                    {"internalType": "uint256", "name": "value", "type": "uint256"},
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.MatchaRoute",
                "name": "matcha",
                "type": "tuple",
            },
            {
                "components": [
                    {
                        "internalType": "uint256",
                        "name": "amountIn",
                        "type": "uint256",
                    },
                    {"internalType": "uint64", "name": "deadline", "type": "uint64"},
                    {"internalType": "uint256", "name": "nonce", "type": "uint256"},
                    {
                        "internalType": "bytes",
                        "name": "maintainerSignature",
                        "type": "bytes",
                    },
                    {
                        "internalType": "uint256",
                        "name": "executionFeeNative",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct MorphoMatchaStableArbUsdc.StableOrder",
                "name": "stable",
                "type": "tuple",
            },
            {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
        ],
        "name": "executeArbitrageWithTokens",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "token", "type": "address"},
        ],
        "name": "supportsLoanToken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint8", "name": "provider", "type": "uint8"}],
        "name": "supportsFlashProvider",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class ArbError(RuntimeError):
    pass


class RetryableArbError(ArbError):
    pass


class QuoteStaleError(RetryableArbError):
    pass


class TransientRpcError(RetryableArbError):
    pass


def classify_atomic_simulation_error(exc: Exception) -> ArbError:
    detail = str(exc)
    lowered = detail.lower()
    if any(
        marker in lowered
        for marker in (
            "read timed out",
            "connect timeout",
            "connection reset",
            "max retries exceeded",
            "too many requests",
            "status 429",
            "status 502",
            "status 503",
            "status 504",
        )
    ):
        return TransientRpcError("Ethereum RPC temporarily failed during simulation")
    if "064a4ec6" in lowered:
        return QuoteStaleError(
            "Matcha route became stale: aggregator output fell below its "
            "minimum return"
        )
    if "3e0aa470" in lowered:
        return QuoteStaleError(
            "Matcha route became stale: output fell below the Stable order input"
        )
    if "4e88422a" in lowered:
        return QuoteStaleError(
            "atomic route could not repay the flash principal, provider fee, "
            "and configured profit floor"
        )
    if "5090d6c6" in lowered:
        return ArbError(
            "Uniswap v4 flash funding conflicts with this Matcha route because "
            "the route tries to unlock the same PoolManager"
        )
    return ArbError(f"atomic mainnet simulation reverted: {detail}")


def ensure_project_runtime() -> None:
    missing = []
    if requests is None:
        missing.append("requests")
    if load_dotenv is None:
        missing.append("python-dotenv")
    try:
        import web3  # noqa: F401
    except ImportError:
        missing.append("web3")
    if not missing:
        return

    project_python = Path(__file__).resolve().parent / ".venv-eth" / "bin" / "python"
    project_environment = project_python.parent.parent
    if (
        project_python.is_file()
        and Path(sys.prefix).resolve() != project_environment.resolve()
    ):
        os.execv(str(project_python), [str(project_python), *sys.argv])
    raise ArbError(
        "missing Ethereum dependencies: "
        + ", ".join(missing)
        + "; run python3 -m venv .venv-eth && "
        ".venv-eth/bin/python -m pip install -r requirements-eth.txt"
    )


@dataclass(frozen=True)
class MatchaQuote:
    aggregator: str
    target: str
    allowance_target: str
    data: str
    value: int
    sell_amount: int
    buy_amount: int
    gas: int | None = None
    gas_price: int | None = None

    def contract_tuple(self) -> tuple[str, str, int, int, bytes]:
        return (
            self.target,
            self.allowance_target,
            self.sell_amount,
            self.value,
            bytes.fromhex(self.data[2:]),
        )


@dataclass(frozen=True)
class StableQuote:
    amount_in: int
    amount_out: int
    token_fee: int | None
    capacity: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None


@dataclass(frozen=True)
class StableOrder:
    amount_in: int
    deadline: int
    nonce: int
    maintainer_signature: str
    execution_fee_native: int
    order_id: str | None = None

    def contract_tuple(self) -> tuple[int, int, int, bytes, int]:
        return (
            self.amount_in,
            self.deadline,
            self.nonce,
            bytes.fromhex(self.maintainer_signature[2:]),
            self.execution_fee_native,
        )


@dataclass(frozen=True)
class FlashLiquidity:
    key: str
    provider_id: int
    label: str
    address: str
    available: int
    premium_bps: int = 0


def amount_to_raw(value: str | Decimal, allow_zero: bool = False) -> int:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ArbError(f"invalid token amount: {value!r}") from exc
    scaled = amount * (Decimal(10) ** DECIMALS)
    if (amount < 0 if allow_zero else amount <= 0) or scaled != scaled.to_integral_value():
        raise ArbError("token amount must be non-negative with at most 6 decimals" if allow_zero else "token amount must be positive with at most 6 decimals")
    return int(scaled)


def minimum_output_after_slippage(amount: int, slippage_bps: int) -> int:
    if amount <= 0 or not 0 <= slippage_bps < 10_000:
        raise ArbError("invalid amount or slippage")
    return amount * (10_000 - slippage_bps) // 10_000


def capacity_limited_loan_amount(
    loan_amount: int,
    stable_amount_in: int,
    capacity_raw: int,
) -> int:
    """Scale the loan so Stable's input fits its reported token balance."""
    if loan_amount <= 0 or stable_amount_in <= 0 or capacity_raw <= 0:
        raise ArbError("invalid Stable.com capacity sizing values")
    if stable_amount_in <= capacity_raw:
        return loan_amount
    adjusted = loan_amount * capacity_raw // stable_amount_in
    if adjusted <= 0 or adjusted >= loan_amount:
        raise ArbError("Stable.com capacity is too small to size this route")
    return adjusted


def parse_binance_eth_usdc_price(payload: Any) -> Decimal:
    raw_val = None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and "amount" in data:
            raw_val = data["amount"]
        elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict) and "last" in data[0]:
            raw_val = data[0]["last"]
        elif payload.get("symbol") in ("ETHUSDC", "ETH-USD") or "price" in payload or "last" in payload:
            raw_val = payload.get("price") or payload.get("last") or payload.get("rate")
    if raw_val is None:
        raise ArbError(f"Unexpected ETH price response: {payload!r}")
    try:
        price = Decimal(str(raw_val))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ArbError(f"Invalid ETH price: {raw_val!r}") from exc
    if not price.is_finite() or price <= 0:
        raise ArbError("Binance returned a non-positive ETHUSDC price")
    return price


def buffered_eth_usd_price(price: Decimal, buffer_bps: int) -> Decimal:
    if price <= 0 or not 0 <= buffer_bps <= 1_000:
        raise ArbError("invalid ETH price or price buffer")
    buffered = price * Decimal(10_000 + buffer_bps) / Decimal(10_000)
    return buffered.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def raw_to_amount(value: int) -> str:
    if value < 0:
        raise ArbError("raw token amount cannot be negative")
    text = f"{Decimal(value) / (Decimal(10) ** DECIMALS):.{DECIMALS}f}"
    return text.rstrip("0").rstrip(".") or "0"


def raw_to_signed_amount(value: int) -> str:
    return f"-{raw_to_amount(-value)}" if value < 0 else raw_to_amount(value)


def parse_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ArbError(f"missing or invalid {label}")
    try:
        if isinstance(value, str) and value.lower().startswith("0x"):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ArbError(f"missing or invalid {label}") from exc


def is_address(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def is_hex_data(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) < 10:
        return False
    try:
        bytes.fromhex(value[2:])
    except ValueError:
        return False
    return True


def dictionaries(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from dictionaries(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from dictionaries(nested)


def first_key(value: Any, names: Iterable[str]) -> Any:
    wanted = tuple(names)
    for item in dictionaries(value):
        for name in wanted:
            if name in item and item[name] not in (None, ""):
                return item[name]
    return None


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def parse_matcha_quote(
    aggregator: str,
    payload: Any,
    expected_sell_amount: int,
) -> MatchaQuote:
    roots = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        roots.insert(0, payload["data"])

    holder: dict[str, Any] | None = None
    for root in roots:
        if not isinstance(root, dict):
            continue
        candidate = root.get("allowanceHolder")
        if isinstance(candidate, dict):
            holder = candidate
            break
    if holder is None:
        raise ArbError(f"{aggregator}: response has no allowanceHolder route")

    simulation = holder.get("simulation")
    if not isinstance(simulation, dict) or str(simulation.get("result", "")).lower() != "success":
        raise ArbError(f"{aggregator}: allowance-holder simulation was not successful")

    quote = holder.get("quote")
    if not isinstance(quote, dict):
        raise ArbError(f"{aggregator}: response has no executable quote")
    transaction = quote.get("transaction")
    tx = transaction if isinstance(transaction, dict) else quote

    target = tx.get("to") or quote.get("to")
    allowance_target = quote.get("allowanceTarget") or holder.get("allowanceTarget")
    call_data = tx.get("data") or quote.get("data")
    if not is_address(target) or not is_address(allowance_target) or not is_hex_data(call_data):
        raise ArbError(f"{aggregator}: incomplete target, allowance target, or calldata")

    sell_amount = parse_integer(quote.get("sellAmount"), f"{aggregator} sellAmount")
    buy_amount = parse_integer(quote.get("buyAmount"), f"{aggregator} buyAmount")
    if sell_amount != expected_sell_amount:
        raise ArbError(
            f"{aggregator}: sell amount changed from {expected_sell_amount} to {sell_amount}"
        )
    if buy_amount <= 0:
        raise ArbError(f"{aggregator}: non-positive buy amount")

    gas_value = quote.get("gas") or tx.get("gas")
    gas_price_value = quote.get("gasPrice") or tx.get("gasPrice")
    return MatchaQuote(
        aggregator=aggregator,
        target=target,
        allowance_target=allowance_target,
        data=call_data,
        value=parse_integer(tx.get("value", quote.get("value", 0)), "Matcha value"),
        sell_amount=sell_amount,
        buy_amount=buy_amount,
        gas=parse_integer(gas_value, "Matcha gas") if gas_value is not None else None,
        gas_price=(
            parse_integer(gas_price_value, "Matcha gasPrice")
            if gas_price_value is not None
            else None
        ),
    )


def select_best_matcha_quote(
    responses: Iterable[tuple[str, Any]],
    expected_sell_amount: int,
) -> MatchaQuote:
    valid: list[MatchaQuote] = []
    errors: list[str] = []
    for aggregator, response in responses:
        try:
            valid.append(parse_matcha_quote(aggregator, response, expected_sell_amount))
        except ArbError as exc:
            errors.append(str(exc))
    if not valid:
        detail = "; ".join(errors) if errors else "no aggregator responses"
        raise ArbError(f"Matcha returned no executable simulated quote: {detail}")
    return max(valid, key=lambda quote: (quote.buy_amount, -(quote.gas or 0)))


def _stable_raw_amount(
    value: Any,
    label: str,
    fractional_rounding: str | None = None,
) -> int:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ArbError(f"invalid Stable.com {label}") from exc
    if decimal_value < 0:
        raise ArbError(f"invalid Stable.com {label}")
    scaled = decimal_value * (Decimal(10) ** DECIMALS)
    if scaled != scaled.to_integral_value():
        if fractional_rounding is None:
            raise ArbError(f"Stable.com {label} has more than 6 decimals")
        scaled = scaled.to_integral_value(rounding=fractional_rounding)
    return int(scaled)


def parse_stable_quote(payload: Any, amount_in: int) -> StableQuote:
    amount_out_value = first_key(payload, ("amountTo", "amountOut", "outputAmount"))
    if amount_out_value is None:
        raise ArbError("Stable.com status response has no output amount")
    token_fee_value = first_key(payload, ("tokenFee", "protocolFee"))
    capacity_value = first_key(
        payload,
        ("available", "capacity", "liquidity", "balance"),
    )
    minimum_value = first_key(payload, ("min", "minimum"))
    maximum_value = first_key(payload, ("max", "maximum"))

    def optional_decimal(value: Any) -> Decimal | None:
        try:
            parsed = Decimal(str(value)) if value is not None else None
        except InvalidOperation:
            return None
        return parsed if parsed is not None and parsed >= 0 else None

    return StableQuote(
        amount_in=amount_in,
        amount_out=_stable_raw_amount(
            amount_out_value,
            "output amount",
            fractional_rounding=ROUND_DOWN,
        ),
        token_fee=(
            _stable_raw_amount(
                token_fee_value,
                "token fee",
                fractional_rounding=ROUND_CEILING,
            )
            if token_fee_value is not None
            else None
        ),
        capacity=optional_decimal(capacity_value),
        minimum=optional_decimal(minimum_value),
        maximum=optional_decimal(maximum_value),
    )


def parse_stable_order(payload: Any, expected_amount_in: int) -> StableOrder:
    order = unwrap_data(payload)
    if not isinstance(order, dict):
        raise ArbError(f"Stable.com create response is not an object: {payload!r}")
    err = first_key(order, ("error", "message", "msg", "reason", "detail")) or (
        first_key(payload, ("error", "message", "msg", "reason", "detail"))
        if isinstance(payload, dict)
        else None
    )
    signature = order.get("maintainerSignature")
    if not isinstance(signature, str):
        if err:
            raise ArbError(f"Stable.com create order failed: {err}")
        raise ArbError(f"Stable.com order has no maintainer signature: {order!r}")
    signature = signature if signature.startswith("0x") else f"0x{signature}"
    try:
        signature_bytes = bytes.fromhex(signature[2:])
    except ValueError as exc:
        raise ArbError("Stable.com maintainer signature is not hex") from exc
    if len(signature_bytes) != 65:
        raise ArbError("Ethereum Stable.com signature must be 65 bytes")

    amount_value = order.get("amountFrom", order.get("amountIn"))
    if amount_value is not None:
        returned_amount = _stable_raw_amount(amount_value, "order input amount")
        if returned_amount != expected_amount_in:
            raise ArbError("Stable.com changed the signed input amount")

    deadline = parse_integer(order.get("deadline"), "Stable.com deadline")
    if deadline <= int(time.time()):
        raise ArbError("Stable.com returned an expired order")
    return StableOrder(
        amount_in=expected_amount_in,
        deadline=deadline,
        nonce=parse_integer(order.get("nonce"), "Stable.com nonce"),
        maintainer_signature=signature,
        execution_fee_native=parse_integer(
            order.get("executionFeeNative", order.get("nativeFee", 0)),
            "Stable.com execution fee",
        ),
        order_id=str(order["orderId"]) if order.get("orderId") is not None else None,
    )


class HttpJsonClient:
    def __init__(self, timeout: float, user_agent: str):
        if cffi_requests is not None:
            self.session = cffi_requests.Session(impersonate="chrome124")
        elif requests is not None:
            self.session = requests.Session()
        else:
            raise ArbError("requests or curl_cffi is required; install requirements-eth.txt")

        self.timeout = timeout
        self.session.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": user_agent,
            }
        )
        cookies = os.environ.get("ETH_ARB_MATCHA_COOKIES") or os.environ.get("MATCHA_COOKIES")
        if cookies:
            for item in cookies.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    self.session.cookies.set(k.strip(), v.strip())

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except Exception as exc:
            raise ArbError(f"GET {url} failed: {exc}") from exc
        return self._decode(response, url)

    def post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = self.session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ArbError(f"POST {url} failed: {exc}") from exc
        return self._decode(response, url)

    @staticmethod
    def _decode(response: Any, url: str) -> Any:
        excerpt = " ".join(response.text.split())[:400]
        if response.status_code >= 400:
            cloudflare = (
                " Cloudflare may be blocking this undocumented API."
                if response.status_code == 403
                else ""
            )
            detail = f": {excerpt}" if excerpt else ""
            message = f"{url} returned HTTP {response.status_code}{detail}.{cloudflare}"
            if response.status_code in (429, 502, 503, 504):
                raise RetryableArbError(message)
            raise ArbError(message)
        try:
            payload = response.json()
        except ValueError as exc:
            detail = f": {excerpt}" if excerpt else ""
            raise ArbError(f"{url} returned non-JSON content{detail}") from exc
        if payload is None:
            raise RetryableArbError(f"{url} returned an empty JSON response")
        return payload


class MatchaClient:
    def __init__(self, http: HttpJsonClient, base_url: str = MATCHA_BASE_URL):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "origin": "https://matcha.xyz",
            "referer": "https://matcha.xyz/",
        }

    def gas_price(self) -> int:
        payload = self.http.get(
            f"{self.base_url}/api/gas?chainId={CHAIN_ID}",
            headers=self.headers,
        )
        value = first_key(payload, ("price", "gasPrice", "fast", "standard"))
        return parse_integer(value, "Matcha gas price")

    def quotes(
        self,
        executor: str,
        sell_amount: int,
        slippage_bps: int,
        aggregators: Iterable[str],
        sell_token_address: str = PYUSD,
        buy_token_address: str = USDC,
    ) -> list[tuple[str, Any]]:
        gas_price = self.gas_price()
        competition = self.http.post(
            f"{self.base_url}/api/competitions",
            {
                "chainId": CHAIN_ID,
                "isAllowanceHolderFlow": True,
                "gasPrice": str(gas_price),
                "sellTokenAddress": sell_token_address.lower(),
                "sellTokenDecimals": DECIMALS,
                "buyTokenAddress": buy_token_address.lower(),
                "buyTokenDecimals": DECIMALS,
                "sellAmount": str(sell_amount),
                "slippageBps": slippage_bps,
                "slippagePpm": slippage_bps * 100,
                "taker": executor,
            },
            headers=self.headers,
        )
        competition_id = first_key(competition, ("competitionId", "id"))
        if not competition_id:
            raise ArbError("Matcha competition response has no competitionId")

        def fetch(aggregator: str) -> tuple[str, Any]:
            response = self.http.post(
                f"{self.base_url}/api/quotes?aggregator={aggregator}",
                {"competitionId": competition_id, "aggregator": aggregator},
                headers=self.headers,
            )
            return aggregator, response

        responses: list[tuple[str, Any]] = []
        errors: list[str] = []
        selected = tuple(aggregators)
        with ThreadPoolExecutor(max_workers=min(4, len(selected))) as pool:
            futures = {pool.submit(fetch, name): name for name in selected}
            for future in as_completed(futures):
                try:
                    responses.append(future.result())
                except ArbError as exc:
                    errors.append(f"{futures[future]}: {exc}")
        if not responses:
            raise ArbError("all Matcha quote requests failed: " + "; ".join(errors))
        return responses


class StableClient:
    def __init__(self, http: HttpJsonClient, base_url: str = STABLE_BASE_URL):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "accept-language": "en-US,en;q=0.9",
            "origin": "https://stable.com",
            "priority": "u=1, i",
            "referer": "https://stable.com/",
            "sec-ch-ua": (
                '"Not;A=Brand";v="8", "Chromium";v="150", '
                '"Google Chrome";v="150"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }

    @staticmethod
    def _base_payload(
        executor: str,
        asset_from: str,
        asset_to: str,
        amount_in: int,
    ) -> dict[str, Any]:
        return {
            "assetFrom": asset_from,
            "assetTo": asset_to,
            "chainFrom": STABLE_CHAIN_ID,
            "chainTo": STABLE_CHAIN_ID,
            "amountFrom": raw_to_amount(amount_in),
            "addressFrom": executor,
            "addressTo": executor,
            "gasLess": False,
        }

    def quote(
        self,
        executor: str,
        asset_from: str,
        asset_to: str,
        amount_in: int,
    ) -> StableQuote:
        payload = self.http.post(
            f"{self.base_url}/swap/status",
            self._base_payload(executor, asset_from, asset_to, amount_in),
            headers=self.headers,
        )
        return parse_stable_quote(payload, amount_in)

    def create_order(
        self,
        executor: str,
        asset_from: str,
        asset_to: str,
        amount_in: int,
        amount_out: int,
    ) -> StableOrder:
        request = self._base_payload(executor, asset_from, asset_to, amount_in)
        request.update(
            {
                "amountTo": raw_to_amount(amount_out),
                "device": str(uuid.uuid4()),
            }
        )
        response = self.http.post(
            f"{self.base_url}/swap/create/singleChain",
            request,
            headers=self.headers,
        )
        return parse_stable_order(response, amount_in)


def gas_cost_usdc_raw(
    gas_limit: int,
    max_fee_per_gas_wei: int,
    eth_usd_price: Decimal,
) -> int:
    gas_cost_wei = Decimal(gas_limit * max_fee_per_gas_wei)
    gas_cost_eth = gas_cost_wei / Decimal(10**18)
    gas_cost_usd = gas_cost_eth * eth_usd_price
    scaled = gas_cost_usd * (Decimal(10) ** DECIMALS)
    return int(scaled.to_integral_value(rounding=ROUND_CEILING))


def wei_cost_usdc_raw(wei_amount: int, eth_usd_price: Decimal) -> int:
    if wei_amount <= 0:
        return 0
    eth_amount = Decimal(wei_amount) / Decimal(10**18)
    usd_amount = eth_amount * eth_usd_price
    scaled = usd_amount * (Decimal(10) ** DECIMALS)
    return int(scaled.to_integral_value(rounding=ROUND_CEILING))


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, dict):
        return {key: json_safe(val) for key, val in value.items()}
    if isinstance(value, list):
        return [json_safe(val) for val in value]
    if isinstance(value, tuple):
        return [json_safe(val) for val in value]
    return value


def require_web3() -> Any:
    try:
        from web3 import Web3
    except ImportError as exc:
        raise ArbError(
            "web3 is required for simulation/broadcast; "
            "install requirements-eth.txt"
        ) from exc
    return Web3


def require_executor_loan_support(
    rpc_url: str,
    rpc_timeout: float,
    executor: str,
    loan_token: str,
) -> None:
    Web3 = require_web3()
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": rpc_timeout}))
    if not web3.is_connected():
        raise ArbError("could not connect to Ethereum RPC endpoint")
    contract = web3.eth.contract(
        address=web3.to_checksum_address(executor),
        abi=EXECUTOR_ABI,
    )
    try:
        supported = contract.functions.supportsLoanToken(
            web3.to_checksum_address(loan_token)
        ).call()
    except Exception as exc:
        raise ArbError(f"failed to check executor loan token support: {exc}") from exc
    if not supported:
        raise ArbError(
            f"executor {executor} does not support loan token {loan_token}"
        )


def select_flash_provider(
    requested: str,
    loan_symbol: str,
    loan_amount: int,
    available_by_provider: dict[str, int],
    premium_bps_by_provider: dict[str, int] | None = None,
) -> FlashLiquidity:
    if requested not in ("auto", *FLASH_PROVIDER_IDS):
        raise ArbError(f"unsupported flash provider: {requested}")
    candidates = AUTO_FLASH_PROVIDER_ORDER if requested == "auto" else (requested,)
    premiums = premium_bps_by_provider or {}
    for key in candidates:
        available = available_by_provider.get(key, 0)
        if available >= loan_amount:
            return FlashLiquidity(
                key=key,
                provider_id=FLASH_PROVIDER_IDS[key],
                label=FLASH_PROVIDER_LABELS[key],
                address=FLASH_PROVIDER_ADDRESSES[key],
                available=available,
                premium_bps=premiums.get(key, 0),
            )

    details = ", ".join(
        f"{FLASH_PROVIDER_LABELS[key]} {raw_to_amount(available_by_provider.get(key, 0))}"
        for key in candidates
    )
    if requested == "auto":
        raise ArbError(
            f"no flash provider has {raw_to_amount(loan_amount)} {loan_symbol}; "
            f"available: {details} {loan_symbol}"
        )
    raise ArbError(
        f"{FLASH_PROVIDER_LABELS[requested]} flash liquidity is "
        f"{raw_to_amount(available_by_provider.get(requested, 0))} {loan_symbol}, "
        f"below requested {raw_to_amount(loan_amount)} {loan_symbol}"
    )


def resolve_flash_provider(
    rpc_url: str,
    rpc_timeout: float,
    requested: str,
    loan_symbol: str,
    loan_token: str,
    loan_amount: int,
) -> FlashLiquidity:
    Web3 = require_web3()
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": rpc_timeout}))
    if not web3.is_connected():
        raise ArbError("could not connect to Ethereum RPC endpoint")
    token = web3.eth.contract(
        address=web3.to_checksum_address(loan_token),
        abi=[
            {
                "inputs": [{"name": "account", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }
        ],
    )
    available: dict[str, int] = {}
    premiums: dict[str, int] = {}
    keys = AUTO_FLASH_PROVIDER_ORDER if requested == "auto" else (requested,)
    try:
        for key in keys:
            if key == "aave-v3":
                data_provider = web3.eth.contract(
                    address=web3.to_checksum_address(AAVE_V3_DATA_PROVIDER),
                    abi=[
                        {
                            "inputs": [{"name": "asset", "type": "address"}],
                            "name": "getReserveTokensAddresses",
                            "outputs": [
                                {"name": "aTokenAddress", "type": "address"},
                                {"name": "stableDebtTokenAddress", "type": "address"},
                                {"name": "variableDebtTokenAddress", "type": "address"},
                            ],
                            "stateMutability": "view",
                            "type": "function",
                        },
                        {
                            "inputs": [{"name": "asset", "type": "address"}],
                            "name": "getFlashLoanEnabled",
                            "outputs": [{"name": "", "type": "bool"}],
                            "stateMutability": "view",
                            "type": "function",
                        },
                    ],
                )
                flash_enabled = data_provider.functions.getFlashLoanEnabled(
                    web3.to_checksum_address(loan_token)
                ).call()
                a_token = data_provider.functions.getReserveTokensAddresses(
                    web3.to_checksum_address(loan_token)
                ).call()[0]
                available[key] = (
                    int(token.functions.balanceOf(a_token).call())
                    if flash_enabled and int(a_token, 16) != 0
                    else 0
                )
                pool = web3.eth.contract(
                    address=web3.to_checksum_address(AAVE_V3_POOL),
                    abi=[
                        {
                            "inputs": [],
                            "name": "FLASHLOAN_PREMIUM_TOTAL",
                            "outputs": [{"name": "", "type": "uint128"}],
                            "stateMutability": "view",
                            "type": "function",
                        }
                    ],
                )
                premiums[key] = int(pool.functions.FLASHLOAN_PREMIUM_TOTAL().call())
            else:
                available[key] = int(
                    token.functions.balanceOf(
                        web3.to_checksum_address(FLASH_PROVIDER_ADDRESSES[key])
                    ).call()
                )
    except Exception as exc:
        raise ArbError(f"failed to read flash-loan liquidity: {exc}") from exc
    return select_flash_provider(
        requested,
        loan_symbol,
        loan_amount,
        available,
        premiums,
    )


def flash_provider_fee(amount: int, premium_bps: int) -> int:
    if amount <= 0 or not 0 <= premium_bps <= 10_000:
        raise ArbError("invalid flash-loan fee inputs")
    return (amount * premium_bps + 5_000) // 10_000


def require_executor_flash_provider_support(
    rpc_url: str,
    rpc_timeout: float,
    executor: str,
    provider: FlashLiquidity,
) -> None:
    if provider.key == "morpho":
        return
    Web3 = require_web3()
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": rpc_timeout}))
    if not web3.is_connected():
        raise ArbError("could not connect to Ethereum RPC endpoint")
    contract = web3.eth.contract(
        address=web3.to_checksum_address(executor),
        abi=EXECUTOR_ABI,
    )
    try:
        supported = contract.functions.supportsFlashProvider(provider.provider_id).call()
    except Exception as exc:
        raise ArbError(
            f"executor {executor} does not implement {provider.label} flash funding; "
            "deploy the updated MorphoMatchaStableArbUsdc contract and set "
            "ETH_ARB_STABLECOIN_EXECUTOR"
        ) from exc
    if not supported:
        raise ArbError(
            f"executor {executor} does not support {provider.label} flash funding"
        )


def checksum_matcha_arguments(web3: Any, matcha: MatchaQuote) -> tuple[Any, ...]:
    """Normalize API-supplied addresses before Web3 ABI validation."""
    arguments = matcha.contract_tuple()
    return (
        web3.to_checksum_address(arguments[0]),
        web3.to_checksum_address(arguments[1]),
        *arguments[2:],
    )


def prepare_transaction(
    rpc_url: str,
    rpc_timeout: float,
    executor: str,
    operator: str,
    loan_amount: int,
    loan_token: str,
    intermediate_token: str,
    flash_provider: FlashLiquidity,
    matcha: MatchaQuote,
    stable: StableOrder,
    min_profit: int,
    gas_limit_multiplier: Decimal,
    max_fee_gwei_override: Decimal | None,
    swap_order: str = "dex-first",
) -> tuple[Any, dict[str, Any], int, int]:
    Web3 = require_web3()
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": rpc_timeout}))
    if not web3.is_connected():
        raise ArbError("could not connect to Ethereum RPC endpoint")

    contract = web3.eth.contract(
        address=web3.to_checksum_address(executor),
        abi=EXECUTOR_ABI,
    )
    matcha_arguments = checksum_matcha_arguments(web3, matcha)

    swap_order_id = 1 if swap_order == "stable-first" else 0
    call = contract.functions.executeArbitrageWithTokensAndProviderAndOrder(
        loan_amount,
        web3.to_checksum_address(loan_token),
        web3.to_checksum_address(intermediate_token),
        flash_provider.provider_id,
        swap_order_id,
        matcha_arguments,
        stable.contract_tuple(),
        min_profit,
    )

    native_value = matcha.value + stable.execution_fee_native
    tx_params: dict[str, Any] = {
        "from": web3.to_checksum_address(operator),
        "value": native_value,
        "chainId": CHAIN_ID,
    }

    try:
        estimated_gas = call.estimate_gas(tx_params)
    except Exception as exc:
        raise classify_atomic_simulation_error(exc) from exc

    gas_limit = int(
        (Decimal(estimated_gas) * gas_limit_multiplier).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    latest_block = web3.eth.get_block("latest")
    base_fee = latest_block.get("baseFeePerGas")
    if base_fee is None:
        raise ArbError("RPC block header did not include baseFeePerGas")

    priority_fee = web3.eth.max_priority_fee
    computed_max_fee = (base_fee * 2) + priority_fee
    if max_fee_gwei_override is not None:
        override_wei = int(
            (max_fee_gwei_override * Decimal(10**9)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if override_wei < computed_max_fee:
            raise ArbError(
                f"--max-fee-gwei {max_fee_gwei_override} is below safe block "
                f"minimum {Decimal(computed_max_fee) / Decimal(10**9):.3f} Gwei"
            )
        max_fee_per_gas = override_wei
    else:
        max_fee_per_gas = computed_max_fee

    nonce = web3.eth.get_transaction_count(web3.to_checksum_address(operator))
    tx_params.update(
        {
            "nonce": nonce,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": priority_fee,
        }
    )
    transaction = call.build_transaction(tx_params)
    return web3, transaction, estimated_gas, max_fee_per_gas


def setting(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is not None and value.strip():
        return value.strip()
    return default


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--executor",
        default=(
            setting("ETH_ARB_STABLECOIN_EXECUTOR")
            or setting("ETH_ARB_PYUSD_USDC_EXECUTOR")
            or setting("ETH_EXECUTOR_ADDRESS")
            or setting("ETH_ARB_EXECUTOR")
            or "0x6FA26637Db03519B520A44056fc4D93858Ba5833"
        ),
        help="deployed MorphoMatchaStableArbUsdc contract address",
    )
    result.add_argument(
        "--rpc-url",
        default=setting("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com"),
        help="Ethereum RPC URL used for state simulation and broadcasting",
    )
    result.add_argument(
        "--loan-token",
        choices=tuple(LOAN_TOKENS.keys()),
        default=setting("ETH_ARB_LOAN_TOKEN", "PYUSD"),
        help="flash loan token to borrow and profit in (USDC, PYUSD, or USDG)",
    )
    result.add_argument(
        "--intermediate-token",
        choices=tuple(LOAN_TOKENS.keys()),
        default=setting("ETH_ARB_INTERMEDIATE_TOKEN"),
        help=(
            "token bought on Matcha and returned through Stable.com; defaults "
            "to the legacy PYUSD/USDC counterpart"
        ),
    )
    result.add_argument(
        "--flash-provider",
        choices=("auto", *FLASH_PROVIDER_IDS),
        default=setting("ETH_ARB_FLASH_PROVIDER", "auto"),
        help=(
            "atomic funding source; auto prefers fee-free Morpho and falls "
            "back to Aave v3; Uniswap v4 is explicit because nested v4 swap "
            "routes are incompatible with its unlock"
        ),
    )
    result.add_argument(
        "--amount",
        default=setting("ETH_ARB_AMOUNT", "300000"),
        help="flash loan size in loan token units",
    )
    result.add_argument(
        "--min-profit",
        default=setting("ETH_ARB_MIN_PROFIT", "1"),
        help="minimum gross profit floor required on-chain",
    )
    result.add_argument(
        "--min-net-profit",
        default=setting("ETH_ARB_MIN_NET_PROFIT", "0"),
        help="minimum net profit floor after subtracting maximum gas cost",
    )
    result.add_argument(
        "--operator",
        default=setting("ETH_OPERATOR_ADDRESS", "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"),
        help="EOA address initiating the transaction",
    )
    result.add_argument(
        "--slippage-bps",
        type=int,
        default=int(setting("ETH_ARB_SLIPPAGE_BPS", "0")),
        help="Matcha -> Stable slippage tolerance in basis points (0-100)",
    )
    result.add_argument(
        "--stable-capacity-buffer",
        default=setting("ETH_ARB_STABLE_CAPACITY_BUFFER", "1"),
        help="safety buffer subtracted from Stable's capacity",
    )
    result.add_argument(
        "--eth-usd",
        type=Decimal,
        default=setting("ETH_ARB_ETH_USD"),
        help="optional minimum ETH/USD; a live Binance price is always fetched",
    )
    result.add_argument(
        "--timeout",
        type=float,
        default=setting("ETH_ARB_HTTP_TIMEOUT_SECONDS", "20"),
    )
    result.add_argument(
        "--quote-attempts",
        type=int,
        default=setting("ETH_ARB_QUOTE_ATTEMPTS", "3"),
        help="retry only transient stale-route simulation failures",
    )
    result.add_argument(
        "--rpc-timeout",
        type=float,
        default=setting("ETH_ARB_RPC_TIMEOUT_SECONDS", "90"),
        help="timeout in seconds for complex Ethereum RPC simulations",
    )
    result.add_argument(
        "--aggregators",
        default=setting("ETH_ARB_AGGREGATORS", ",".join(MATCHA_AGGREGATORS)),
    )
    result.add_argument(
        "--gas-limit-multiplier",
        type=Decimal,
        default=setting("ETH_ARB_GAS_LIMIT_MULTIPLIER", "1.20"),
    )
    result.add_argument(
        "--max-fee-gwei",
        type=Decimal,
        default=setting("ETH_ARB_MAX_FEE_GWEI"),
    )
    result.add_argument(
        "--matcha-base-url",
        default=setting("ETH_ARB_MATCHA_BASE_URL", MATCHA_BASE_URL),
    )
    result.add_argument(
        "--stable-base-url",
        default=setting("ETH_ARB_STABLE_BASE_URL", STABLE_BASE_URL),
    )
    result.add_argument(
        "--eth-price-url",
        default=setting("ETH_ARB_ETH_PRICE_URL", BINANCE_ETH_PRICE_URL),
    )
    result.add_argument(
        "--eth-price-buffer-bps",
        type=int,
        default=setting("ETH_ARB_ETH_PRICE_BUFFER_BPS", "100"),
        help="upward safety buffer applied to live ETHUSDC for gas accounting",
    )
    result.add_argument(
        "--output",
        default=setting("ETH_ARB_OUTPUT_PATH"),
        help="optional path for the dry-run JSON plan",
    )
    result.add_argument(
        "--swap-order",
        choices=("dex-first", "stable-first"),
        default=setting("ETH_ARB_SWAP_ORDER", "dex-first"),
        help="execution order: dex-first (Matcha -> Stable) or stable-first (Stable -> Matcha)",
    )
    result.add_argument("--send", action="store_true", help="sign and broadcast after all checks")
    result.add_argument(
        "--confirm-mainnet",
        help="must equal EXECUTE_ATOMIC_ARB when --send is used",
    )
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not is_address(args.executor):
        raise ArbError("--executor must be the deployed executor contract address")
    if not args.rpc_url:
        raise ArbError("--rpc-url or ETH_RPC_URL is required for atomic simulation")
    if not 0 <= args.slippage_bps <= 100:
        raise ArbError("--slippage-bps must be between 0 and 100")
    if not 1 <= args.quote_attempts <= 10:
        raise ArbError("--quote-attempts must be between 1 and 10")
    if not 1 <= args.rpc_timeout <= 300:
        raise ArbError("--rpc-timeout must be between 1 and 300 seconds")
    if not 0 <= args.eth_price_buffer_bps <= 1_000:
        raise ArbError("--eth-price-buffer-bps must be between 0 and 1000")
    if args.gas_limit_multiplier < 1:
        raise ArbError("--gas-limit-multiplier must be at least 1")

    requested_loan_amount = amount_to_raw(args.amount)
    loan_amount = requested_loan_amount
    min_profit = amount_to_raw(args.min_profit, allow_zero=True)
    min_net_profit = amount_to_raw(args.min_net_profit, allow_zero=True)
    loan_symbol = args.loan_token
    loan_token = LOAN_TOKENS[loan_symbol]
    
    if args.intermediate_token:
        intermediate_symbol = args.intermediate_token
    elif loan_symbol in {"PYUSD", "USDG"}:
        intermediate_symbol = "USDC"
    else:
        intermediate_symbol = "PYUSD"
    if intermediate_symbol == loan_symbol:
        raise ArbError("--intermediate-token must differ from --loan-token")
    intermediate_token = LOAN_TOKENS[intermediate_symbol]

    capacity_buffer = amount_to_raw(args.stable_capacity_buffer)

    private_key = os.getenv("ETH_OPERATOR_PRIVATE_KEY")
    operator = args.operator
    if args.send:
        if args.confirm_mainnet != "EXECUTE_ATOMIC_ARB":
            raise ArbError("--send requires --confirm-mainnet EXECUTE_ATOMIC_ARB")
        if not private_key:
            raise ArbError("ETH_OPERATOR_PRIVATE_KEY is required when using --send (check .env)")
        Web3 = require_web3()
        derived = Web3().eth.account.from_key(private_key).address
        if operator and operator.lower() != derived.lower():
            raise ArbError("--operator does not match ETH_OPERATOR_PRIVATE_KEY")
        operator = derived
    if not is_address(operator):
        raise ArbError("--operator or ETH_OPERATOR_ADDRESS is required")

    require_executor_loan_support(
        args.rpc_url,
        args.rpc_timeout,
        args.executor,
        loan_token,
    )
    require_executor_loan_support(
        args.rpc_url,
        args.rpc_timeout,
        args.executor,
        intermediate_token,
    )
    flash_provider = resolve_flash_provider(
        args.rpc_url,
        args.rpc_timeout,
        args.flash_provider,
        loan_symbol,
        loan_token,
        loan_amount,
    )
    require_executor_flash_provider_support(
        args.rpc_url,
        args.rpc_timeout,
        args.executor,
        flash_provider,
    )

    user_agent = os.getenv(
        "ETH_QUOTE_USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    )
    http = HttpJsonClient(args.timeout, user_agent)
    matcha_client = MatchaClient(http, args.matcha_base_url)
    stable_client = StableClient(http, args.stable_base_url)

    def fetch_eth_price(primary_url: str) -> Decimal:
        candidates = [
            primary_url,
            "https://api.coinbase.com/v2/prices/ETH-USD/spot",
            "https://www.okx.com/api/v5/market/ticker?instId=ETH-USDC",
            "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDC",
        ]
        seen = set()
        urls = [u for u in candidates if u and not (u in seen or seen.add(u))]
        last_exc = None
        for u in urls:
            try:
                data = http.get(u)
                return parse_binance_eth_usdc_price(data)
            except Exception as exc:
                last_exc = exc
                continue
        raise ArbError(f"All ETH price endpoints failed: {last_exc}")

    live_eth_usd = fetch_eth_price(args.eth_price_url)
    gas_accounting_eth_usd = buffered_eth_usd_price(
        live_eth_usd,
        args.eth_price_buffer_bps,
    )
    if args.eth_usd is not None:
        if args.eth_usd <= 0:
            raise ArbError("--eth-usd must be positive when provided")
        gas_accounting_eth_usd = max(gas_accounting_eth_usd, args.eth_usd)

    aggregators = tuple(name.strip() for name in args.aggregators.split(",") if name.strip())
    if not aggregators:
        raise ArbError("at least one Matcha aggregator is required")
    capacity_adjusted = False
    capacity_raw: int | None = None
    stable_minimum_raw: int | None = None
    stable_maximum_raw: int | None = None
    if args.swap_order == "stable-first":
        for sizing_attempt in range(1, MAX_CAPACITY_SIZING_ATTEMPTS + 1):
            stable_quote = stable_client.quote(
                args.executor,
                loan_symbol,
                intermediate_symbol,
                loan_amount,
            )
            capacity_raw = (
                _stable_raw_amount(
                    stable_quote.capacity,
                    "capacity",
                    fractional_rounding=ROUND_DOWN,
                )
                if stable_quote.capacity is not None
                else None
            )
            if capacity_raw is not None:
                if capacity_raw <= 0:
                    raise ArbError("Stable.com pool has no remaining capacity")
                usable_capacity_raw = (
                    capacity_raw - capacity_buffer
                    if capacity_raw > capacity_buffer
                    else (capacity_raw - 1 if capacity_raw > 1 else capacity_raw)
                )
                if loan_amount > usable_capacity_raw:
                    loan_amount = usable_capacity_raw
                    capacity_adjusted = True
                    continue

            matcha_responses = matcha_client.quotes(
                args.executor,
                stable_quote.amount_out,
                args.slippage_bps,
                aggregators,
                sell_token_address=intermediate_token,
                buy_token_address=loan_token,
            )
            matcha = select_best_matcha_quote(matcha_responses, stable_quote.amount_out)
            break
        else:
            raise ArbError("Stable.com capacity kept changing; could not size an executable route")

        gross_profit_before_flash_fee = matcha.buy_amount - loan_amount
        flash_loan_fee = flash_provider_fee(
            loan_amount,
            flash_provider.premium_bps,
        )
        gross_profit = gross_profit_before_flash_fee - flash_loan_fee
        print("\n--- QUOTE BREAKDOWN (STABLE-FIRST) ---", file=sys.stderr)
        print(
            f"Flash Provider:          {flash_provider.label} "
            f"({raw_to_amount(flash_provider.available)} {loan_symbol} available)",
            file=sys.stderr,
        )
        print(f"Loan Amount:             {raw_to_amount(loan_amount)} {loan_symbol}", file=sys.stderr)
        print(f"Leg 1 (Stable.com):       {raw_to_amount(stable_quote.amount_in)} {loan_symbol} -> {raw_to_amount(stable_quote.amount_out)} {intermediate_symbol}", file=sys.stderr)
        print(f"Leg 2 (Matcha - {matcha.aggregator}): {raw_to_amount(matcha.sell_amount)} {intermediate_symbol} -> {raw_to_amount(matcha.buy_amount)} {loan_symbol}", file=sys.stderr)
        if flash_loan_fee:
            print(
                f"Flash Loan Fee:          {raw_to_amount(flash_loan_fee)} "
                f"{loan_symbol} ({flash_provider.premium_bps} bps)",
                file=sys.stderr,
            )
        print(f"Gross Profit:            {raw_to_signed_amount(gross_profit)} {loan_symbol}", file=sys.stderr)
        print("---------------------------------------\n", file=sys.stderr)

        if gross_profit < min_profit:
            raise ArbError(
                "quoted route is below the on-chain profit floor: "
                f"{raw_to_signed_amount(gross_profit)} {loan_symbol} < "
                f"{raw_to_amount(min_profit)} {loan_symbol}"
            )
        stable_order = stable_client.create_order(
            args.executor,
            loan_symbol,
            intermediate_symbol,
            stable_quote.amount_in,
            stable_quote.amount_out,
        )
    else:
        for sizing_attempt in range(1, MAX_CAPACITY_SIZING_ATTEMPTS + 1):
            matcha_responses = matcha_client.quotes(
                args.executor,
                loan_amount,
                args.slippage_bps,
                aggregators,
                sell_token_address=loan_token,
                buy_token_address=intermediate_token,
            )
            matcha = select_best_matcha_quote(matcha_responses, loan_amount)

            stable_amount_in = minimum_output_after_slippage(
                matcha.buy_amount,
                args.slippage_bps,
            )
            stable_quote = stable_client.quote(
                args.executor,
                intermediate_symbol,
                loan_symbol,
                stable_amount_in,
            )
            capacity_raw = (
                _stable_raw_amount(
                    stable_quote.capacity,
                    "capacity",
                    fractional_rounding=ROUND_DOWN,
                )
                if stable_quote.capacity is not None
                else None
            )
            stable_minimum_raw = (
                _stable_raw_amount(
                    stable_quote.minimum,
                    "minimum",
                    fractional_rounding=ROUND_CEILING,
                )
                if stable_quote.minimum is not None
                else None
            )
            stable_maximum_raw = (
                _stable_raw_amount(
                    stable_quote.maximum,
                    "maximum",
                    fractional_rounding=ROUND_DOWN,
                )
                if stable_quote.maximum is not None
                else None
            )
            if capacity_raw is not None:
                if capacity_raw <= 0:
                    raise ArbError("Stable.com pool has no remaining capacity")
                usable_capacity_raw = (
                    capacity_raw - capacity_buffer
                    if capacity_raw > capacity_buffer
                    else (capacity_raw - 1 if capacity_raw > 1 else capacity_raw)
                )
            else:
                usable_capacity_raw = None

            if stable_maximum_raw is not None and usable_capacity_raw is not None:
                usable_capacity_raw = min(usable_capacity_raw, stable_maximum_raw)

            if usable_capacity_raw is not None and usable_capacity_raw <= 0:
                raise ArbError("Stable.com pool has no remaining capacity")
            if (
                usable_capacity_raw is not None
                and stable_minimum_raw is not None
                and usable_capacity_raw < stable_minimum_raw
            ):
                raise ArbError(
                    "Stable.com pool capacity is below its minimum order: "
                    f"{raw_to_amount(usable_capacity_raw)} {intermediate_symbol} < "
                    f"{raw_to_amount(stable_minimum_raw)} {intermediate_symbol}"
                )
            if (
                usable_capacity_raw is None
                or stable_quote.amount_in <= usable_capacity_raw
            ):
                break

            adjusted_loan_amount = capacity_limited_loan_amount(
                loan_amount,
                stable_quote.amount_in,
                usable_capacity_raw,
            )
            print(
                f"Stable.com capacity reduced the {loan_symbol} loan from "
                f"{raw_to_amount(loan_amount)} to "
                f"{raw_to_amount(adjusted_loan_amount)}; requesting fresh quotes...",
                file=sys.stderr,
            )
            loan_amount = adjusted_loan_amount
            capacity_adjusted = True
        else:
            raise ArbError(
                "Stable.com capacity kept changing; could not size an executable route"
            )

        if (
            stable_minimum_raw is not None
            and stable_quote.amount_in < stable_minimum_raw
        ):
            raise ArbError(
                "Stable.com order is below its minimum: "
                f"{raw_to_amount(stable_quote.amount_in)} {intermediate_symbol} < "
                f"{raw_to_amount(stable_minimum_raw)} {intermediate_symbol}"
            )

        gross_profit_before_flash_fee = stable_quote.amount_out - loan_amount
        flash_loan_fee = flash_provider_fee(
            loan_amount,
            flash_provider.premium_bps,
        )
        gross_profit = gross_profit_before_flash_fee - flash_loan_fee
        print("\n--- QUOTE BREAKDOWN ---", file=sys.stderr)
        print(
            f"Flash Provider:          {flash_provider.label} "
            f"({raw_to_amount(flash_provider.available)} {loan_symbol} available)",
            file=sys.stderr,
        )
        print(f"Loan Amount:             {raw_to_amount(loan_amount)} {loan_symbol}", file=sys.stderr)
        print(f"Leg 1 (Matcha - {matcha.aggregator}): {raw_to_amount(matcha.sell_amount)} {loan_symbol} -> {raw_to_amount(matcha.buy_amount)} {intermediate_symbol}", file=sys.stderr)
        print(f"Leg 2 (Stable.com):       {raw_to_amount(stable_quote.amount_in)} {intermediate_symbol} -> {raw_to_amount(stable_quote.amount_out)} {loan_symbol}", file=sys.stderr)
        if flash_loan_fee:
            print(
                f"Flash Loan Fee:          {raw_to_amount(flash_loan_fee)} "
                f"{loan_symbol} ({flash_provider.premium_bps} bps)",
                file=sys.stderr,
            )
            print(
                f"Gross Before Flash Fee:  "
                f"{raw_to_signed_amount(gross_profit_before_flash_fee)} {loan_symbol}",
                file=sys.stderr,
            )
        print(f"Gross Profit:            {raw_to_signed_amount(gross_profit)} {loan_symbol}", file=sys.stderr)
        print("-----------------------\n", file=sys.stderr)

        if gross_profit < min_profit:
            raise ArbError(
                "quoted route is below the on-chain profit floor: "
                f"{raw_to_signed_amount(gross_profit)} {loan_symbol} < "
                f"{raw_to_amount(min_profit)} {loan_symbol}"
            )
        stable_order = stable_client.create_order(
            args.executor,
            intermediate_symbol,
            loan_symbol,
            stable_quote.amount_in,
            stable_quote.amount_out,
        )

    web3, transaction, estimated_gas, max_fee_per_gas = prepare_transaction(
        args.rpc_url,
        args.rpc_timeout,
        args.executor,
        operator,
        loan_amount,
        loan_token,
        intermediate_token,
        flash_provider,
        matcha,
        stable_order,
        min_profit,
        args.gas_limit_multiplier,
        args.max_fee_gwei,
        args.swap_order,
    )
    gas_limit = int(transaction["gas"])
    max_gas_cost = gas_cost_usdc_raw(
        gas_limit,
        max_fee_per_gas,
        gas_accounting_eth_usd,
    )
    native_value = matcha.value + stable_order.execution_fee_native
    native_execution_cost = wei_cost_usdc_raw(
        native_value,
        gas_accounting_eth_usd,
    )
    maximum_execution_cost = max_gas_cost + native_execution_cost
    predicted_net = gross_profit - maximum_execution_cost

    plan = {
        "mode": "broadcast" if args.send else "dry-run",
        "chainId": CHAIN_ID,
        "flashLoanProvider": flash_provider.address,
        "flashLoanProviderName": flash_provider.label,
        "flashLoanProviderLiquidity": raw_to_amount(flash_provider.available),
        "flashLoanPremiumBps": flash_provider.premium_bps,
        "flashLoanFee": raw_to_amount(flash_loan_fee),
        "executor": args.executor,
        "operator": operator,
        "loanToken": {
            "symbol": loan_symbol,
            "address": loan_token,
        },
        "intermediateToken": {
            "symbol": intermediate_symbol,
            "address": intermediate_token,
        },
        "requestedLoanAmount": raw_to_amount(requested_loan_amount),
        "ethPrice": {
            "source": "Binance ETHUSDC",
            "marketUsd": str(live_eth_usd),
            "bufferBps": args.eth_price_buffer_bps,
            "gasAccountingUsd": str(gas_accounting_eth_usd),
        },
        "loanAmount": raw_to_amount(loan_amount),
        "capacityAdjusted": capacity_adjusted,
        "matcha": {
            "aggregator": matcha.aggregator,
            "sellLoanToken": raw_to_amount(matcha.sell_amount),
            "buyIntermediate": raw_to_amount(matcha.buy_amount),
            "target": matcha.target,
            "allowanceTarget": matcha.allowance_target,
        },
        "stable": {
            "pool": STABLE_POOL,
            "sellIntermediate": raw_to_amount(stable_quote.amount_in),
            "buyLoanToken": raw_to_amount(stable_quote.amount_out),
            "tokenFee": (
                raw_to_amount(stable_quote.token_fee)
                if stable_quote.token_fee is not None
                else None
            ),
            "capacityIntermediate": (
                raw_to_amount(capacity_raw) if capacity_raw is not None else None
            ),
            "capacityBufferIntermediate": raw_to_amount(capacity_buffer),
            "minimumIntermediate": (
                raw_to_amount(stable_minimum_raw)
                if stable_minimum_raw is not None
                else None
            ),
            "maximumIntermediate": (
                raw_to_amount(stable_maximum_raw)
                if stable_maximum_raw is not None
                else None
            ),
            "orderId": stable_order.order_id,
            "deadline": stable_order.deadline,
        },
        "grossProfitBeforeFlashFee": raw_to_signed_amount(
            gross_profit_before_flash_fee
        ),
        "grossProfit": raw_to_signed_amount(gross_profit),
        "minimumProfit": raw_to_amount(min_profit),
        "estimatedGas": estimated_gas,
        "gasLimit": gas_limit,
        "maxFeePerGasWei": max_fee_per_gas,
        "maxGasCostUsd": raw_to_amount(max_gas_cost),
        "nativeExecutionCostUsd": raw_to_amount(native_execution_cost),
        "maximumExecutionCostUsd": raw_to_amount(maximum_execution_cost),
        "predictedNetProfit": raw_to_signed_amount(predicted_net),
        "transaction": json_safe(transaction),
    }

    if predicted_net < min_net_profit:
        raise ArbError(
            "route is below the maximum-gas net-profit floor: "
            f"{raw_to_signed_amount(predicted_net or 0)} {loan_symbol} < "
            f"{raw_to_amount(min_net_profit)} {loan_symbol}"
        )
    if args.send:
        signed = web3.eth.account.sign_transaction(transaction, private_key)
        transaction_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        plan["transactionHash"] = transaction_hash.hex()

    return plan


def main() -> int:
    try:
        ensure_project_runtime()
        args = parser().parse_args()
        for attempt in range(1, args.quote_attempts + 1):
            try:
                plan = run(args)
                break
            except RetryableArbError as exc:
                if attempt == args.quote_attempts:
                    raise ArbError(
                        f"{exc}; exhausted {args.quote_attempts} fresh quote attempts"
                    ) from exc
                print(
                    f"Transient route/RPC failure on attempt "
                    f"{attempt}/{args.quote_attempts}: {exc}; "
                    "requesting fresh quotes...",
                    file=sys.stderr,
                )
        rendered = json.dumps(plan, indent=2, sort_keys=True)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as output:
                output.write(rendered + "\n")
        print(rendered)
        if not args.send:
            print("\nDry run only: no transaction was signed or broadcast.", file=sys.stderr)
        return 0
    except ArbError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
