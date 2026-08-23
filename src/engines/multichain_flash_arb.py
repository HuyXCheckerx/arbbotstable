#!/usr/bin/env python3
"""Build, simulate, and optionally send Polygon or BNB USDC flash arbitrage."""

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
    import requests
    from dotenv import load_dotenv
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError as exc:
    raise SystemExit(
        "Install dependencies: python3 -m pip install -r requirements-eth.txt"
    ) from exc


PROJECT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_DIR / ".env", override=True)

STABLE_POOL = "0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da"
MATCHA_BASE_URL = "https://meta.matcha.xyz"
STABLE_BASE_URL = "https://api-defi.stable.com"
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
MAX_CAPACITY_SIZING_ATTEMPTS = 5


@dataclass(frozen=True)
class ChainConfig:
    key: str
    name: str
    env_prefix: str
    chain_id: int
    stable_chain_id: str
    decimals: int
    usdc: str
    usdt: str
    pyusd: str | None
    provider_kind: int
    flash_lender: str
    flash_liquidity_holder: str
    flash_provider_name: str
    native_symbol: str
    native_price_symbol: str
    explorer_tx_url: str
    confirmation: str


CHAINS = {
    "polygon": ChainConfig(
        key="polygon",
        name="Polygon PoS",
        env_prefix="POLYGON",
        chain_id=137,
        stable_chain_id="106",
        decimals=6,
        usdc="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        usdt="0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        pyusd="0x99aF3EeA856556646C98c8B9b2548Fe815240750",
        provider_kind=0,
        flash_lender="0x1bF0c2541F820E775182832f06c0B7Fc27A25f67",
        flash_liquidity_holder="0x1bF0c2541F820E775182832f06c0B7Fc27A25f67",
        flash_provider_name="Morpho",
        native_symbol="POL",
        native_price_symbol="POLUSDC",
        explorer_tx_url="https://polygonscan.com/tx/",
        confirmation="EXECUTE_POLYGON_ARB",
    ),
    "bsc": ChainConfig(
        key="bsc",
        name="BNB Smart Chain",
        env_prefix="BSC",
        chain_id=56,
        stable_chain_id="103",
        decimals=18,
        usdc="0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        usdt="0x55d398326f99059fF775485246999027B3197955",
        pyusd=None,
        provider_kind=1,
        flash_lender="0x6807dc923806fE8Fd134338EABCA509979a7e0cB",
        # Aave transfers flash liquidity out of the reserve's aToken contract.
        flash_liquidity_holder="0x00901a076785e0906d1028c7d6372d247bec7d61",
        flash_provider_name="Aave V3",
        native_symbol="BNB",
        native_price_symbol="BNBUSDC",
        explorer_tx_url="https://bscscan.com/tx/",
        confirmation="EXECUTE_BSC_ARB",
    ),
}


PAIR_TO_SYMBOL = {
    "USDT/USDC": "USDT",
    "PYUSD/USDC": "PYUSD",
}


def supported_pairs(config: ChainConfig) -> tuple[str, ...]:
    pairs = ["USDT/USDC"]
    if config.pyusd:
        pairs.append("PYUSD/USDC")
    return tuple(pairs)


def intermediate_for_pair(config: ChainConfig, pair: str) -> tuple[str, str]:
    symbol = PAIR_TO_SYMBOL.get(pair.upper())
    if symbol == "USDT":
        return symbol, config.usdt
    if symbol == "PYUSD" and config.pyusd:
        return symbol, config.pyusd
    raise ArbError(f"{pair} is not supported on {config.name}")


EXECUTOR_ABI = [
    {
        "inputs": [
            {"name": "loanAmount", "type": "uint256"},
            {
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowanceTarget", "type": "address"},
                    {"name": "sellAmount", "type": "uint256"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data", "type": "bytes"},
                ],
                "name": "matcha",
                "type": "tuple",
            },
            {
                "components": [
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "deadline", "type": "uint64"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "maintainerSignature", "type": "bytes"},
                    {"name": "executionFeeNative", "type": "uint256"},
                ],
                "name": "stable",
                "type": "tuple",
            },
            {"name": "minProfit", "type": "uint256"},
        ],
        "name": "executeArbitrage",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    *[
        {
            "inputs": [],
            "name": name,
            "outputs": [{"name": "", "type": output_type}],
            "stateMutability": "view",
            "type": "function",
        }
        for name, output_type in (
            ("owner", "address"),
            ("expectedChainId", "uint256"),
            ("providerKind", "uint8"),
            ("flashLender", "address"),
            ("loanToken", "address"),
            ("intermediateToken", "address"),
            ("stablePool", "address"),
        )
    ],
]

ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

AAVE_POOL_ABI = [
    {
        "inputs": [],
        "name": "FLASHLOAN_PREMIUM_TOTAL",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    }
]


class ArbError(RuntimeError):
    pass


class RetryableArbError(ArbError):
    pass


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


def parse_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ArbError(f"missing or invalid {label}")
    try:
        if isinstance(value, str) and value.lower().startswith("0x"):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ArbError(f"missing or invalid {label}") from exc


def dictionaries(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from dictionaries(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from dictionaries(nested)


def first_key(value: Any, names: Iterable[str]) -> Any:
    for item in dictionaries(value):
        for name in names:
            if name in item and item[name] not in (None, ""):
                return item[name]
    return None


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def amount_to_raw(value: str | Decimal, decimals: int) -> int:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ArbError(f"invalid token amount: {value!r}") from exc
    scaled = amount * (Decimal(10) ** decimals)
    if amount <= 0 or scaled != scaled.to_integral_value():
        raise ArbError(
            f"token amount must be positive with at most {decimals} decimals"
        )
    return int(scaled)


def raw_to_amount(value: int, decimals: int) -> str:
    if value < 0:
        raise ArbError("raw token amount cannot be negative")
    text = f"{Decimal(value) / (Decimal(10) ** decimals):.{decimals}f}"
    return text.rstrip("0").rstrip(".") or "0"


def raw_to_signed_amount(value: int, decimals: int) -> str:
    return (
        f"-{raw_to_amount(-value, decimals)}"
        if value < 0
        else raw_to_amount(value, decimals)
    )


def minimum_order_error(
    config: ChainConfig,
    intermediate_symbol: str,
    loan_amount: int,
    matcha: MatchaQuote,
    stable_minimum_raw: int,
) -> ArbError:
    return ArbError(
        f"No executable Matcha liquidity for USDC -> {intermediate_symbol} on "
        f"{config.name}: best {matcha.aggregator} route quotes "
        f"{raw_to_amount(loan_amount, config.decimals)} USDC -> "
        f"{raw_to_amount(matcha.buy_amount, config.decimals)} "
        f"{intermediate_symbol}, below Stable.com's minimum of "
        f"{raw_to_amount(stable_minimum_raw, config.decimals)} "
        f"{intermediate_symbol}"
    )


def minimum_output_after_slippage(amount: int, slippage_bps: int) -> int:
    if amount <= 0 or not 0 <= slippage_bps < 10_000:
        raise ArbError("invalid amount or slippage")
    return amount * (10_000 - slippage_bps) // 10_000


def capacity_limited_loan_amount(
    loan_amount: int,
    stable_amount_in: int,
    capacity_raw: int,
) -> int:
    if loan_amount <= 0 or stable_amount_in <= 0 or capacity_raw <= 0:
        raise ArbError("invalid Stable.com capacity sizing values")
    if stable_amount_in <= capacity_raw:
        return loan_amount
    adjusted = loan_amount * capacity_raw // stable_amount_in
    if adjusted <= 0 or adjusted >= loan_amount:
        raise ArbError("Stable.com capacity is too small to size this route")
    return adjusted


def flash_fee_raw(amount: int, fee_bps: int) -> int:
    if amount <= 0 or not 0 <= fee_bps < 10_000:
        raise ArbError("invalid flash-loan amount or premium")
    return (amount * fee_bps + 9_999) // 10_000


def decimal_to_raw(
    value: Any,
    decimals: int,
    label: str,
    rounding: str | None = None,
) -> int:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ArbError(f"invalid Stable.com {label}") from exc
    if decimal_value < 0:
        raise ArbError(f"invalid Stable.com {label}")
    scaled = decimal_value * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        if rounding is None:
            raise ArbError(
                f"Stable.com {label} has more than {decimals} decimals"
            )
        scaled = scaled.to_integral_value(rounding=rounding)
    return int(scaled)


def parse_matcha_quote(
    aggregator: str,
    payload: Any,
    expected_sell_amount: int,
) -> MatchaQuote:
    roots = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        roots.insert(0, payload["data"])
    holder = next(
        (
            root.get("allowanceHolder")
            for root in roots
            if isinstance(root, dict) and isinstance(root.get("allowanceHolder"), dict)
        ),
        None,
    )
    if holder is None:
        raise ArbError(f"{aggregator}: response has no allowanceHolder route")
    simulation = holder.get("simulation")
    if (
        not isinstance(simulation, dict)
        or str(simulation.get("result", "")).lower() != "success"
    ):
        raise ArbError(f"{aggregator}: route simulation was not successful")
    quote = holder.get("quote")
    if not isinstance(quote, dict):
        raise ArbError(f"{aggregator}: response has no executable quote")
    transaction = quote.get("transaction")
    tx = transaction if isinstance(transaction, dict) else quote
    target = tx.get("to") or quote.get("to")
    allowance_target = quote.get("allowanceTarget") or holder.get("allowanceTarget")
    call_data = tx.get("data") or quote.get("data")
    if not is_address(target) or not is_address(allowance_target) or not is_hex_data(call_data):
        raise ArbError(f"{aggregator}: incomplete executable calldata")
    sell_amount = parse_integer(quote.get("sellAmount"), f"{aggregator} sellAmount")
    buy_amount = parse_integer(quote.get("buyAmount"), f"{aggregator} buyAmount")
    if sell_amount != expected_sell_amount:
        raise ArbError(f"{aggregator}: Matcha changed the sell amount")
    return MatchaQuote(
        aggregator=aggregator,
        target=target,
        allowance_target=allowance_target,
        data=call_data,
        value=parse_integer(tx.get("value", quote.get("value", 0)), "Matcha value"),
        sell_amount=sell_amount,
        buy_amount=buy_amount,
        gas=(
            parse_integer(quote.get("gas") or tx.get("gas"), "Matcha gas")
            if quote.get("gas") is not None or tx.get("gas") is not None
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
        raise ArbError(
            "Matcha returned no executable simulated quote: " + "; ".join(errors)
        )
    return max(valid, key=lambda quote: (quote.buy_amount, -(quote.gas or 0)))


def parse_stable_quote(
    payload: Any,
    amount_in: int,
    decimals: int,
) -> StableQuote:
    amount_out_value = first_key(payload, ("amountTo", "amountOut", "outputAmount"))
    if amount_out_value is None:
        raise ArbError("Stable.com status response has no output amount")
    token_fee_value = first_key(payload, ("tokenFee", "protocolFee"))
    capacity_value = first_key(payload, ("available", "capacity", "liquidity", "balance"))
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
        amount_out=decimal_to_raw(
            amount_out_value, decimals, "output amount", ROUND_DOWN
        ),
        token_fee=(
            decimal_to_raw(token_fee_value, decimals, "token fee", ROUND_CEILING)
            if token_fee_value is not None
            else None
        ),
        capacity=optional_decimal(capacity_value),
        minimum=optional_decimal(minimum_value),
        maximum=optional_decimal(maximum_value),
    )


def parse_stable_order(
    payload: Any,
    expected_amount_in: int,
    decimals: int,
) -> StableOrder:
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
        raise ArbError("Stable.com EVM signature must be 65 bytes")
    returned_input = order.get("amountFrom", order.get("amountIn"))
    if (
        returned_input is not None
        and decimal_to_raw(returned_input, decimals, "order input")
        != expected_amount_in
    ):
        raise ArbError("Stable.com changed the signed input amount")
    deadline = parse_integer(order.get("deadline"), "Stable.com deadline")
    if deadline <= int(time.time()) + 5:
        raise ArbError("Stable.com order expires too soon")
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
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "user-agent": user_agent,
            }
        )

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RetryableArbError(f"GET {url} failed: {exc}") from exc
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
                url, json=payload, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise RetryableArbError(f"POST {url} failed: {exc}") from exc
        return self._decode(response, url)

    @staticmethod
    def _decode(response: requests.Response, url: str) -> Any:
        excerpt = " ".join(response.text.split())[:400]
        if response.status_code >= 400:
            if response.status_code == 403:
                raise ArbError(
                    f"{url} returned HTTP 403; this website API may require a "
                    "browser session or may be blocking terminal traffic"
                )
            message = f"{url} returned HTTP {response.status_code}"
            if excerpt:
                message += f": {excerpt}"
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
    def __init__(
        self,
        http: HttpJsonClient,
        config: ChainConfig,
        base_url: str,
        intermediate_token: str,
    ):
        self.http = http
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.intermediate_token = intermediate_token
        self.headers = {
            "origin": "https://meta.matcha.xyz",
            "referer": "https://meta.matcha.xyz/",
        }

    def gas_price(self) -> int:
        payload = self.http.get(
            f"{self.base_url}/api/gas?chainId={self.config.chain_id}",
            headers=self.headers,
        )
        return parse_integer(
            first_key(payload, ("price", "gasPrice", "fast", "standard")),
            "Matcha gas price",
        )

    def quotes(
        self,
        executor: str,
        sell_amount: int,
        slippage_bps: int,
        aggregators: Iterable[str],
    ) -> list[tuple[str, Any]]:
        competition = self.http.post(
            f"{self.base_url}/api/competitions",
            {
                "chainId": self.config.chain_id,
                "isAllowanceHolderFlow": True,
                "gasPrice": str(self.gas_price()),
                "sellTokenAddress": self.config.usdc.lower(),
                "sellTokenDecimals": self.config.decimals,
                "buyTokenAddress": self.intermediate_token.lower(),
                "buyTokenDecimals": self.config.decimals,
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

        selected = tuple(aggregators)
        responses: list[tuple[str, Any]] = []
        errors: list[str] = []
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
    def __init__(
        self,
        http: HttpJsonClient,
        config: ChainConfig,
        base_url: str,
        intermediate_symbol: str,
    ):
        self.http = http
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.intermediate_symbol = intermediate_symbol
        self.headers = {
            "origin": "https://stable.com",
            "referer": "https://stable.com/",
        }

    def _payload(
        self,
        executor: str,
        amount_in: int,
        amount_out: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "assetFrom": self.intermediate_symbol,
            "assetTo": "USDC",
            "chainFrom": self.config.stable_chain_id,
            "chainTo": self.config.stable_chain_id,
            "amountFrom": raw_to_amount(amount_in, self.config.decimals),
            "addressFrom": executor,
            "addressTo": executor,
            "gasLess": False,
        }
        if amount_out is not None:
            payload["amountTo"] = raw_to_amount(amount_out, self.config.decimals)
        return payload

    def quote(self, executor: str, amount_in: int) -> StableQuote:
        payload = self.http.post(
            f"{self.base_url}/swap/status",
            self._payload(executor, amount_in),
            headers=self.headers,
        )
        return parse_stable_quote(payload, amount_in, self.config.decimals)

    def create_order(
        self,
        executor: str,
        amount_in: int,
        amount_out: int,
    ) -> StableOrder:
        request = self._payload(executor, amount_in, amount_out)
        request["device"] = str(uuid.uuid4())
        payload = self.http.post(
            f"{self.base_url}/swap/create/singleChain",
            request,
            headers=self.headers,
        )
        return parse_stable_order(payload, amount_in, self.config.decimals)


def chain_web3(config: ChainConfig, rpc_url: str, timeout: float) -> Web3:
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    try:
        chain_id = web3.eth.chain_id
    except Exception as exc:
        detail = str(exc).replace(rpc_url, "<redacted RPC URL>")
        raise RetryableArbError(f"cannot connect to {config.name} RPC: {detail}") from exc
    if chain_id != config.chain_id:
        raise ArbError(
            f"RPC chain ID is {chain_id}; {config.name} requires {config.chain_id}"
        )
    return web3


def validate_executor(
    web3: Web3,
    config: ChainConfig,
    executor_address: str,
    operator_address: str,
    intermediate_token: str,
) -> Any:
    executor = Web3.to_checksum_address(executor_address)
    if len(web3.eth.get_code(executor)) == 0:
        raise ArbError("executor address has no deployed bytecode")
    contract = web3.eth.contract(address=executor, abi=EXECUTOR_ABI)
    expected = {
        "owner": operator_address,
        "expectedChainId": config.chain_id,
        "providerKind": config.provider_kind,
        "flashLender": config.flash_lender,
        "loanToken": config.usdc,
        "intermediateToken": intermediate_token,
        "stablePool": STABLE_POOL,
    }
    for getter, wanted in expected.items():
        try:
            actual = getattr(contract.functions, getter)().call()
        except Exception as exc:
            raise ArbError(
                "executor is not the expected multichain contract"
            ) from exc
        if isinstance(wanted, str):
            if str(actual).lower() != wanted.lower():
                raise ArbError(f"executor {getter} is {actual}; expected {wanted}")
        elif int(actual) != wanted:
            raise ArbError(f"executor {getter} is {actual}; expected {wanted}")
    return contract


def get_flash_state(
    web3: Web3,
    config: ChainConfig,
) -> tuple[int, int]:
    lender = Web3.to_checksum_address(config.flash_lender)
    if len(web3.eth.get_code(lender)) == 0:
        raise ArbError(f"configured {config.flash_provider_name} lender has no bytecode")
    token = web3.eth.contract(
        address=Web3.to_checksum_address(config.usdc), abi=ERC20_ABI
    )
    onchain_decimals = int(token.functions.decimals().call())
    if onchain_decimals != config.decimals:
        raise ArbError(
            f"USDC has {onchain_decimals} decimals; expected {config.decimals}"
        )
    liquidity = int(
        token.functions.balanceOf(
            Web3.to_checksum_address(config.flash_liquidity_holder)
        ).call()
    )
    if config.provider_kind == 0:
        premium_bps = 0
    else:
        pool = web3.eth.contract(address=lender, abi=AAVE_POOL_ABI)
        premium_bps = int(pool.functions.FLASHLOAN_PREMIUM_TOTAL().call())
    return liquidity, premium_bps


def parse_native_price(payload: Any, expected_symbol: str) -> Decimal:
    if not isinstance(payload, dict) or payload.get("symbol") != expected_symbol:
        raise ArbError(f"price endpoint returned an unexpected {expected_symbol} response")
    try:
        price = Decimal(str(payload.get("price")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ArbError("native-token price is invalid") from exc
    if not price.is_finite() or price <= 0:
        raise ArbError("native-token price is non-positive")
    return price


def buffered_price(price: Decimal, buffer_bps: int) -> Decimal:
    if price <= 0 or not 0 <= buffer_bps <= 1_000:
        raise ArbError("invalid native-token price buffer")
    result = price * Decimal(10_000 + buffer_bps) / Decimal(10_000)
    return result.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def native_cost_raw(
    value_wei: int,
    native_usd: Decimal,
    decimals: int,
) -> int:
    cost = (
        Decimal(value_wei)
        / (Decimal(10) ** 18)
        * native_usd
        * (Decimal(10) ** decimals)
    )
    return int(cost.to_integral_value(rounding=ROUND_CEILING))


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if hasattr(value, "hex") and callable(value.hex):
        rendered = value.hex()
        return rendered if str(rendered).startswith("0x") else f"0x{rendered}"
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    return value


def classify_simulation_error(exc: Exception) -> ArbError:
    detail = str(exc)
    lowered = detail.lower()
    if any(
        marker in lowered
        for marker in (
            "timed out",
            "connection reset",
            "too many requests",
            "status 429",
            "status 502",
            "status 503",
            "status 504",
        )
    ):
        return RetryableArbError(f"RPC simulation temporarily failed: {detail}")
    if "064a4ec6" in lowered or "3e0aa470" in lowered:
        return RetryableArbError("route became stale during atomic simulation")
    return ArbError(f"atomic simulation reverted: {detail}")


def prepare_transaction(
    web3: Web3,
    config: ChainConfig,
    contract: Any,
    operator: str,
    loan_amount: int,
    matcha: MatchaQuote,
    stable_order: StableOrder,
    min_profit: int,
    gas_multiplier: Decimal,
    configured_max_fee_gwei: Decimal | None,
) -> tuple[dict[str, Any], int, int]:
    matcha_tuple = matcha.contract_tuple()
    matcha_tuple = (
        Web3.to_checksum_address(matcha_tuple[0]),
        Web3.to_checksum_address(matcha_tuple[1]),
        *matcha_tuple[2:],
    )
    function = contract.functions.executeArbitrage(
        loan_amount,
        matcha_tuple,
        stable_order.contract_tuple(),
        min_profit,
    )
    native_value = matcha.value + stable_order.execution_fee_native
    call_parameters = {"from": operator, "value": native_value}
    try:
        function.call(call_parameters)
        estimated_gas = int(function.estimate_gas(call_parameters))
    except Exception as exc:
        raise classify_simulation_error(exc) from exc
    gas_limit = int(
        (Decimal(estimated_gas) * gas_multiplier).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    latest = web3.eth.get_block("latest")
    try:
        priority_fee = int(web3.eth.max_priority_fee)
    except Exception:
        priority_fee = int(web3.to_wei(Decimal("0.05"), "gwei"))
    transaction_fields: dict[str, Any]
    base_fee = latest.get("baseFeePerGas")
    if base_fee is not None:
        max_fee = (
            int(web3.to_wei(configured_max_fee_gwei, "gwei"))
            if configured_max_fee_gwei is not None
            else int(base_fee) * 2 + priority_fee
        )
        if max_fee < priority_fee:
            raise ArbError("maximum fee per gas is below the priority fee")
        transaction_fields = {
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
        }
    else:
        max_fee = (
            int(web3.to_wei(configured_max_fee_gwei, "gwei"))
            if configured_max_fee_gwei is not None
            else int(web3.eth.gas_price)
        )
        transaction_fields = {"gasPrice": max_fee}
    transaction = function.build_transaction(
        {
            "from": operator,
            "chainId": config.chain_id,
            "nonce": web3.eth.get_transaction_count(operator, "pending"),
            "gas": gas_limit,
            "value": native_value,
            **transaction_fields,
        }
    )
    return transaction, estimated_gas, max_fee


def parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument(
        "--chain",
        choices=tuple(CHAINS),
        default=os.getenv("EVM_ARB_CHAIN", "polygon").lower(),
    )
    selected, _ = bootstrap.parse_known_args(argv)
    config = CHAINS[selected.chain]

    pair_parser = argparse.ArgumentParser(add_help=False, parents=[bootstrap])
    pair_parser.add_argument(
        "--pair",
        choices=supported_pairs(config),
        default=os.getenv(f"{config.env_prefix}_ARB_PAIR", "USDT/USDC").upper(),
    )
    selected_route, _ = pair_parser.parse_known_args(argv)
    intermediate_symbol, _ = intermediate_for_pair(config, selected_route.pair)

    def setting(suffix: str, fallback: str | None = None) -> str | None:
        value = os.getenv(f"{config.env_prefix}_{suffix}")
        return value if value not in (None, "") else fallback

    result = argparse.ArgumentParser(description=__doc__, parents=[pair_parser])
    pair_executor = setting(
        f"{intermediate_symbol}_USDC_EXECUTOR_ADDRESS",
        setting("EXECUTOR_ADDRESS") if intermediate_symbol == "USDT" else None,
    )
    result.add_argument("--executor", default=pair_executor)
    result.add_argument("--operator", default=setting("OPERATOR_ADDRESS"))
    result.add_argument("--rpc-url", default=setting("RPC_URL"))
    result.add_argument(
        "--amount",
        default=setting("ARB_AMOUNT_USDC", "50000"),
        help="maximum USDC flash-loan principal",
    )
    result.add_argument(
        "--flash-liquidity-buffer",
        default=setting("ARB_FLASH_LIQUIDITY_BUFFER_USDC", "1"),
    )
    result.add_argument(
        "--stable-capacity-buffer",
        default=setting(
            f"ARB_STABLE_CAPACITY_BUFFER_{intermediate_symbol}",
            setting("ARB_STABLE_CAPACITY_BUFFER_USDT", "1"),
        ),
    )
    result.add_argument(
        "--slippage-bps", type=int, default=setting("ARB_SLIPPAGE_BPS", "0")
    )
    result.add_argument("--min-profit", default=setting("ARB_MIN_PROFIT_USDC", "1"))
    result.add_argument(
        "--min-net-profit", default=setting("ARB_MIN_NET_PROFIT_USDC", "1")
    )
    result.add_argument(
        "--timeout", type=float, default=setting("ARB_HTTP_TIMEOUT_SECONDS", "20")
    )
    result.add_argument(
        "--quote-attempts", type=int, default=setting("ARB_QUOTE_ATTEMPTS", "3")
    )
    result.add_argument(
        "--rpc-timeout", type=float, default=setting("ARB_RPC_TIMEOUT_SECONDS", "90")
    )
    result.add_argument(
        "--aggregators",
        default=setting("ARB_AGGREGATORS", ",".join(MATCHA_AGGREGATORS)),
    )
    result.add_argument(
        "--gas-limit-multiplier",
        type=Decimal,
        default=setting("ARB_GAS_LIMIT_MULTIPLIER", "1.20"),
    )
    result.add_argument(
        "--max-fee-gwei", type=Decimal, default=setting("ARB_MAX_FEE_GWEI")
    )
    result.add_argument(
        "--native-usd", type=Decimal, default=setting("ARB_NATIVE_USD")
    )
    result.add_argument(
        "--native-price-buffer-bps",
        type=int,
        default=setting("ARB_NATIVE_PRICE_BUFFER_BPS", "100"),
    )
    result.add_argument(
        "--native-price-url",
        default=setting(
            "ARB_NATIVE_PRICE_URL",
            "https://data-api.binance.vision/api/v3/ticker/price"
            f"?symbol={config.native_price_symbol}",
        ),
    )
    result.add_argument(
        "--matcha-base-url", default=setting("ARB_MATCHA_BASE_URL", MATCHA_BASE_URL)
    )
    result.add_argument(
        "--stable-base-url", default=setting("ARB_STABLE_BASE_URL", STABLE_BASE_URL)
    )
    result.add_argument(
        "--output",
        default=setting("ARB_OUTPUT_PATH", f"/tmp/{config.key}-arb-plan.json"),
    )
    result.add_argument("--send", action="store_true")
    result.add_argument("--confirm-mainnet")
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = CHAINS[args.chain]
    intermediate_symbol, intermediate_token = intermediate_for_pair(
        config, args.pair
    )
    if not is_address(args.executor):
        raise ArbError("--executor must be the deployed multichain executor")
    if not is_address(args.operator):
        raise ArbError("--operator or the chain operator address is required")
    if not args.rpc_url:
        raise ArbError("the selected chain RPC URL is required")
    if not 0 <= args.slippage_bps <= 100:
        raise ArbError("--slippage-bps must be between 0 and 100")
    if not 1 <= args.quote_attempts <= 10:
        raise ArbError("--quote-attempts must be between 1 and 10")
    if args.gas_limit_multiplier < 1:
        raise ArbError("--gas-limit-multiplier must be at least 1")
    if not 0 <= args.native_price_buffer_bps <= 1_000:
        raise ArbError("--native-price-buffer-bps must be between 0 and 1000")

    private_key = os.getenv(f"{config.env_prefix}_OPERATOR_PRIVATE_KEY", "").strip()
    operator = Web3.to_checksum_address(args.operator)
    if args.send:
        if args.confirm_mainnet != config.confirmation:
            raise ArbError(
                f"--send requires --confirm-mainnet {config.confirmation}"
            )
        if not private_key:
            raise ArbError(
                f"{config.env_prefix}_OPERATOR_PRIVATE_KEY is required for --send"
            )
        derived = Web3().eth.account.from_key(private_key).address
        if derived.lower() != operator.lower():
            raise ArbError("configured operator does not match its private key")

    web3 = chain_web3(config, args.rpc_url, args.rpc_timeout)
    contract = validate_executor(
        web3, config, args.executor, operator, intermediate_token
    )
    available_liquidity, premium_bps = get_flash_state(web3, config)
    requested_amount = amount_to_raw(args.amount, config.decimals)
    flash_buffer = amount_to_raw(args.flash_liquidity_buffer, config.decimals)
    if available_liquidity <= flash_buffer:
        raise ArbError(f"{config.flash_provider_name} USDC liquidity is below its buffer")
    usable_flash_liquidity = available_liquidity - flash_buffer
    loan_amount = min(requested_amount, usable_flash_liquidity)
    min_profit = amount_to_raw(args.min_profit, config.decimals)
    min_net_profit = amount_to_raw(args.min_net_profit, config.decimals)
    stable_buffer = amount_to_raw(args.stable_capacity_buffer, config.decimals)

    user_agent = os.getenv(
        "EVM_QUOTE_USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    )
    http = HttpJsonClient(args.timeout, user_agent)
    matcha_client = MatchaClient(
        http, config, args.matcha_base_url, intermediate_token
    )
    stable_client = StableClient(
        http, config, args.stable_base_url, intermediate_symbol
    )
    market_native_usd = parse_native_price(
        http.get(args.native_price_url), config.native_price_symbol
    )
    accounting_native_usd = buffered_price(
        market_native_usd, args.native_price_buffer_bps
    )
    if args.native_usd is not None:
        if args.native_usd <= 0:
            raise ArbError("--native-usd must be positive")
        accounting_native_usd = max(accounting_native_usd, args.native_usd)
    aggregators = tuple(x.strip() for x in args.aggregators.split(",") if x.strip())
    if not aggregators:
        raise ArbError("at least one Matcha aggregator is required")

    capacity_adjusted = loan_amount != requested_amount
    capacity_raw: int | None = None
    stable_minimum_raw: int | None = None
    stable_maximum_raw: int | None = None
    for _ in range(MAX_CAPACITY_SIZING_ATTEMPTS):
        responses = matcha_client.quotes(
            args.executor, loan_amount, args.slippage_bps, aggregators
        )
        matcha = select_best_matcha_quote(responses, loan_amount)
        stable_amount_in = minimum_output_after_slippage(
            matcha.buy_amount, args.slippage_bps
        )
        stable_quote = stable_client.quote(args.executor, stable_amount_in)
        capacity_raw = (
            decimal_to_raw(
                stable_quote.capacity, config.decimals, "capacity", ROUND_DOWN
            )
            if stable_quote.capacity is not None
            else None
        )
        stable_minimum_raw = (
            decimal_to_raw(
                stable_quote.minimum, config.decimals, "minimum", ROUND_CEILING
            )
            if stable_quote.minimum is not None
            else None
        )
        stable_maximum_raw = (
            decimal_to_raw(
                stable_quote.maximum, config.decimals, "maximum", ROUND_DOWN
            )
            if stable_quote.maximum is not None
            else None
        )
        if capacity_raw is not None:
            if capacity_raw <= 0:
                raise ArbError("Stable.com pool has no remaining capacity")
            usable_capacity = (
                capacity_raw - stable_buffer
                if capacity_raw > stable_buffer
                else (capacity_raw - 1 if capacity_raw > 1 else capacity_raw)
            )
        else:
            usable_capacity = None

        if stable_maximum_raw is not None and usable_capacity is not None:
            usable_capacity = min(usable_capacity, stable_maximum_raw)

        if usable_capacity is not None and usable_capacity <= 0:
            raise ArbError("Stable.com pool has no remaining capacity")
        if (
            usable_capacity is not None
            and stable_minimum_raw is not None
            and usable_capacity < stable_minimum_raw
        ):
            raise ArbError("Stable.com usable capacity is below its minimum order")
        if usable_capacity is None or stable_quote.amount_in <= usable_capacity:
            break
        adjusted = capacity_limited_loan_amount(
            loan_amount, stable_quote.amount_in, usable_capacity
        )
        print(
            f"Stable.com capacity reduced the loan from "
            f"{raw_to_amount(loan_amount, config.decimals)} to "
            f"{raw_to_amount(adjusted, config.decimals)} USDC; re-quoting...",
            file=sys.stderr,
        )
        loan_amount = adjusted
        capacity_adjusted = True
    else:
        raise ArbError("Stable.com capacity kept changing during sizing")

    if stable_minimum_raw is not None and stable_quote.amount_in < stable_minimum_raw:
        raise minimum_order_error(
            config,
            intermediate_symbol,
            loan_amount,
            matcha,
            stable_minimum_raw,
        )
    provider_fee = flash_fee_raw(loan_amount, premium_bps)
    gross_profit = stable_quote.amount_out - loan_amount - provider_fee
    if gross_profit < min_profit:
        raise ArbError(
            "route is below the on-chain profit floor after flash premium: "
            f"{raw_to_signed_amount(gross_profit, config.decimals)} USDC < "
            f"{raw_to_amount(min_profit, config.decimals)} USDC"
        )
    stable_order = stable_client.create_order(
        args.executor, stable_quote.amount_in, stable_quote.amount_out
    )
    transaction, estimated_gas, max_fee_per_gas = prepare_transaction(
        web3,
        config,
        contract,
        operator,
        loan_amount,
        matcha,
        stable_order,
        min_profit,
        args.gas_limit_multiplier,
        args.max_fee_gwei,
    )
    gas_limit = int(transaction["gas"])
    max_gas_cost = native_cost_raw(
        gas_limit * max_fee_per_gas, accounting_native_usd, config.decimals
    )
    native_value = matcha.value + stable_order.execution_fee_native
    native_execution_cost = native_cost_raw(
        native_value, accounting_native_usd, config.decimals
    )
    maximum_execution_cost = max_gas_cost + native_execution_cost
    predicted_net = gross_profit - maximum_execution_cost

    plan = {
        "mode": "broadcast" if args.send else "dry-run",
        "chain": config.name,
        "chainId": config.chain_id,
        "pair": args.pair,
        "flashLoanProvider": config.flash_provider_name,
        "flashLender": config.flash_lender,
        "flashPremiumBps": premium_bps,
        "flashPremiumUSDC": raw_to_amount(provider_fee, config.decimals),
        "flashLiquidityUSDC": raw_to_amount(available_liquidity, config.decimals),
        "executor": args.executor,
        "operator": operator,
        "loanToken": config.usdc,
        "intermediateToken": intermediate_token,
        "intermediateSymbol": intermediate_symbol,
        "requestedLoanAmount": raw_to_amount(requested_amount, config.decimals),
        "loanAmount": raw_to_amount(loan_amount, config.decimals),
        "capacityAdjusted": capacity_adjusted,
        "matcha": {
            "aggregator": matcha.aggregator,
            "sellLoanToken": raw_to_amount(matcha.sell_amount, config.decimals),
            "buyIntermediate": raw_to_amount(matcha.buy_amount, config.decimals),
            "target": matcha.target,
            "allowanceTarget": matcha.allowance_target,
        },
        "stable": {
            "pool": STABLE_POOL,
            "sellIntermediate": raw_to_amount(
                stable_quote.amount_in, config.decimals
            ),
            "buyLoanToken": raw_to_amount(
                stable_quote.amount_out, config.decimals
            ),
            "tokenFee": (
                raw_to_amount(stable_quote.token_fee, config.decimals)
                if stable_quote.token_fee is not None
                else None
            ),
            "capacityIntermediate": (
                raw_to_amount(capacity_raw, config.decimals)
                if capacity_raw is not None
                else None
            ),
            "orderId": stable_order.order_id,
            "deadline": stable_order.deadline,
        },
        "nativePrice": {
            "symbol": config.native_price_symbol,
            "marketUsd": str(market_native_usd),
            "accountingUsd": str(accounting_native_usd),
        },
        "grossProfitAfterFlashFee": raw_to_signed_amount(
            gross_profit, config.decimals
        ),
        "minimumProfit": raw_to_amount(min_profit, config.decimals),
        "estimatedGas": estimated_gas,
        "gasLimit": gas_limit,
        "maxFeePerGasWei": max_fee_per_gas,
        "maximumExecutionCostUSDC": raw_to_amount(
            maximum_execution_cost, config.decimals
        ),
        "predictedNetProfit": raw_to_signed_amount(
            predicted_net, config.decimals
        ),
        "transaction": json_safe(transaction),
    }
    if args.send:
        if predicted_net < min_net_profit:
            raise ArbError(
                "route is below the maximum-gas net-profit floor: "
                f"{raw_to_signed_amount(predicted_net, config.decimals)} USDC"
            )
        maximum_native_cost = gas_limit * max_fee_per_gas + native_value
        if web3.eth.get_balance(operator) < maximum_native_cost:
            raise ArbError(f"operator has insufficient {config.native_symbol} for gas")
        signed = web3.eth.account.sign_transaction(transaction, private_key)
        transaction_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        plan["transactionHash"] = transaction_hash.hex()
        plan["explorer"] = config.explorer_tx_url + transaction_hash.hex()
    return plan


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser(argv).parse_args(argv)
        for attempt in range(1, args.quote_attempts + 1):
            try:
                plan = run(args)
                break
            except RetryableArbError as exc:
                if attempt == args.quote_attempts:
                    raise ArbError(
                        f"{exc}; exhausted {args.quote_attempts} fresh attempts"
                    ) from exc
                print(
                    f"Transient failure {attempt}/{args.quote_attempts}: {exc}; retrying...",
                    file=sys.stderr,
                )
        rendered = json.dumps(plan, indent=2, sort_keys=True)
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        if not args.send:
            print("Dry run only: nothing was signed or broadcast.", file=sys.stderr)
        return 0
    except ArbError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
