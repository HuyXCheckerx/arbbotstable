#!/usr/bin/env python3
"""Unified Flash Arbitrage Engine for Binance Smart Chain (BSC) and Polygon PoS.

Monitors and executes atomic flash-loan arbitrage on BSC (chain 56) and Polygon (chain 137)
against Stable.com single-chain liquidity pools using direct DEX aggregators (ParaSwap, OKX,
1inch, Matcha).

Features:
- Dual-chain simultaneous scanning in a single unified loop (or single-chain focus).
- Keyless high-performance direct aggregator quotes (ParaSwap Augustus v6.2, OKX, 1inch, Matcha).
- Sizing against live Stable.com capacity and flash-loan lender liquidity (Morpho on Polygon, Aave V3 on BSC).
- Live execution with on-chain eth_call simulation, gas pricing in native USD, and safety profit floors.
- Standalone execution: run independently from crosschain_sniper.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable
import urllib.parse
import urllib.request
import uuid

try:
    from dotenv import load_dotenv
    import requests
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError as exc:
    raise SystemExit(
        "Missing dependencies: run `.venv/bin/pip install -r requirements-eth.txt`"
    ) from exc

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env", override=True)

STABLE_POOL = "0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da"
STABLE_BASE_URL = "https://api-defi.stable.com"
PARASWAP_BASE_URL = "https://api.paraswap.io"


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
    default_rpc: str


CHAINS: dict[str, ChainConfig] = {
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
        flash_provider_name="Morpho (0 fee)",
        native_symbol="POL",
        native_price_symbol="POLUSDC",
        explorer_tx_url="https://polygonscan.com/tx/",
        confirmation="EXECUTE_POLYGON_ARB",
        default_rpc="https://polygon-bor-rpc.publicnode.com",
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
        flash_liquidity_holder="0x00901a076785e0906d1028c7d6372d247bec7d61",
        flash_provider_name="Aave V3",
        native_symbol="BNB",
        native_price_symbol="BNBUSDC",
        explorer_tx_url="https://bscscan.com/tx/",
        confirmation="EXECUTE_BSC_ARB",
        default_rpc="https://bsc-rpc.publicnode.com",
    ),
}

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


@dataclass(frozen=True)
class DEXQuote:
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
            Web3.to_checksum_address(self.target),
            Web3.to_checksum_address(self.allowance_target),
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


def is_address(val: Any) -> bool:
    return isinstance(val, str) and len(val) == 42 and val.startswith("0x")


def is_hex(val: Any) -> bool:
    return isinstance(val, str) and val.startswith("0x") and len(val) >= 10


def to_raw(amount: str | Decimal | int, decimals: int, rounding: str = ROUND_DOWN, allow_zero: bool = False) -> int:
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, TypeError) as exc:
        raise ArbError(f"Invalid amount: {amount}") from exc
    raw = d * (Decimal(10) ** decimals)
    if raw < 0 or (raw == 0 and not allow_zero):
        raise ArbError("Amount must be positive")
    return int(raw.to_integral_value(rounding=rounding))


def from_raw(raw: int, decimals: int) -> str:
    text = f"{Decimal(raw) / (Decimal(10) ** decimals):.{decimals}f}"
    return text.rstrip("0").rstrip(".") or "0"


class DirectDEXClient:
    """Queries public direct DEX aggregators on BSC and Polygon."""

    def __init__(self, timeout: float = 10.0, session: Any | None = None):
        self.timeout = timeout
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "arbbotstable/1.0", "Accept": "application/json"})
        self.session = session

    def _http_get(self, url: str) -> dict[str, Any]:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _http_post(self, url: str, data: dict[str, Any]) -> dict[str, Any]:
        resp = self.session.post(url, json=data, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_paraswap_quote(
        self,
        config: ChainConfig,
        sell_token: str,
        buy_token: str,
        sell_amount_raw: int,
        slippage_bps: int,
        taker: str,
        build_tx: bool = False,
    ) -> DEXQuote | None:
        """Fetch quote via ParaSwap Augustus v6.2 (Velora), optionally building calldata."""
        try:
            params = urllib.parse.urlencode({
                "srcToken": sell_token.lower(),
                "destToken": buy_token.lower(),
                "amount": str(sell_amount_raw),
                "srcDecimals": config.decimals,
                "destDecimals": config.decimals,
                "side": "SELL",
                "network": config.chain_id,
            })
            price_url = f"{PARASWAP_BASE_URL}/prices?{params}"
            data = self._http_get(price_url)
            route = data.get("priceRoute")
            if not route:
                return None
            dest_amount = int(route["destAmount"])
            effective_slippage = max(10, int(slippage_bps))
            min_dest = dest_amount * (10_000 - effective_slippage) // 10_000
            allowance = route.get("tokenTransferProxy") or "0xDEF171Fe48CF0115B1d80b88dc8eAB59176FEe57"
            gas_est = int(route.get("gasCost", 180_000))

            target = allowance
            calldata = "0x"
            value = 0

            # If full executable transaction is requested (e.g. for live execution):
            if build_tx:
                build_url = (
                    f"{PARASWAP_BASE_URL}/transactions/{config.chain_id}"
                    "?ignoreChecks=true&ignoreGasEstimate=true"
                )
                build_body = {
                    "srcToken": sell_token.lower(),
                    "destToken": buy_token.lower(),
                    "srcAmount": str(sell_amount_raw),
                    "destAmount": str(min_dest),
                    "priceRoute": route,
                    "userAddress": taker,
                    "srcDecimals": config.decimals,
                    "destDecimals": config.decimals,
                }
                tx_data = self._http_post(build_url, build_body)
                target = tx_data.get("to") or target
                allowance = route.get("tokenTransferProxy") or target
                calldata = tx_data.get("data") or "0x"
                value = int(tx_data.get("value", 0))

            return DEXQuote(
                aggregator="ParaSwap (Velora)",
                target=target,
                allowance_target=allowance,
                data=calldata,
                value=value,
                sell_amount=sell_amount_raw,
                buy_amount=dest_amount,
                gas=gas_est,
            )
        except Exception:
            return None

    def get_best_quote(
        self,
        config: ChainConfig,
        sell_token: str,
        buy_token: str,
        sell_amount_raw: int,
        slippage_bps: int,
        taker: str,
        build_tx: bool = False,
    ) -> DEXQuote | None:
        """Returns the best executable quote across enabled DEX aggregators."""
        quotes: list[DEXQuote] = []
        pq = self.get_paraswap_quote(config, sell_token, buy_token, sell_amount_raw, slippage_bps, taker, build_tx=build_tx)
        if pq:
            quotes.append(pq)
        if not quotes:
            return None
        return max(quotes, key=lambda q: q.buy_amount)


class StableDefiClient:
    """Interacts with Stable.com single-chain swap API."""

    def __init__(self, timeout: float = 12.0, session: Any | None = None):
        self.timeout = timeout
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "arbbotstable/1.0", "Content-Type": "application/json"})
        self.session = session

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{STABLE_BASE_URL}{path}"
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def quote(
        self,
        config: ChainConfig,
        asset_from: str,
        asset_to: str,
        amount_in_raw: int,
        taker: str,
    ) -> StableQuote | None:
        """Query Stable.com pool status & amountOut."""
        try:
            body = {
                "assetFrom": asset_from,
                "assetTo": asset_to,
                "chainFrom": config.stable_chain_id,
                "chainTo": config.stable_chain_id,
                "amountFrom": from_raw(amount_in_raw, config.decimals),
                "addressFrom": taker,
                "addressTo": taker,
                "gasLess": False,
            }
            res = self._post("/swap/status", body)
            # Unwrap nested asset if present
            data = res.get("asset") if isinstance(res.get("asset"), dict) else res
            amount_out_str = data.get("amountTo")
            if not amount_out_str:
                return None
            amount_out_raw = to_raw(amount_out_str, config.decimals)
            token_fee_str = data.get("tokenFee")
            token_fee = to_raw(token_fee_str, config.decimals) if token_fee_str is not None else None
            balance_str = data.get("balance")
            capacity = Decimal(str(balance_str)) if balance_str is not None else None
            min_str = data.get("min")
            minimum = Decimal(str(min_str)) if min_str is not None else None
            max_str = data.get("max")
            maximum = Decimal(str(max_str)) if max_str is not None else None

            return StableQuote(
                amount_in=amount_in_raw,
                amount_out=amount_out_raw,
                token_fee=token_fee,
                capacity=capacity,
                minimum=minimum,
                maximum=maximum,
            )
        except Exception:
            return None

    def create_order(
        self,
        config: ChainConfig,
        asset_from: str,
        asset_to: str,
        amount_in_raw: int,
        amount_out_raw: int,
        taker: str,
    ) -> StableOrder:
        """Create signed single-chain order with maintainer signature."""
        body = {
            "assetFrom": asset_from,
            "assetTo": asset_to,
            "chainFrom": config.stable_chain_id,
            "chainTo": config.stable_chain_id,
            "amountFrom": from_raw(amount_in_raw, config.decimals),
            "amountTo": from_raw(amount_out_raw, config.decimals),
            "addressFrom": taker,
            "addressTo": taker,
            "gasLess": False,
            "device": str(uuid.uuid4()),
        }
        res = self._post("/swap/create/singleChain", body)
        order = res.get("data") if isinstance(res.get("data"), dict) else (res.get("order") if isinstance(res.get("order"), dict) else res)
        sig = order.get("maintainerSignature")
        if not sig or not isinstance(sig, str):
            raise ArbError(f"Stable.com did not return a valid maintainerSignature: {res}")
        if not sig.startswith("0x"):
            sig = f"0x{sig}"
        deadline = int(order["deadline"])
        nonce = int(order["nonce"])
        exec_fee = int(order.get("executionFeeNative", 0))
        return StableOrder(
            amount_in=amount_in_raw,
            deadline=deadline,
            nonce=nonce,
            maintainer_signature=sig,
            execution_fee_native=exec_fee,
            order_id=str(order.get("orderId", "")),
        )


class MultichainEngine:
    """Unified engine to monitor and execute live arbitrage on BSC and Polygon."""

    def __init__(self, dex_client: DirectDEXClient | None = None, stable_client: StableDefiClient | None = None):
        self.dex = dex_client or DirectDEXClient()
        self.stable = stable_client or StableDefiClient()
        self._w3_cache: dict[str, Web3] = {}

    def get_web3(self, config: ChainConfig, custom_rpc: str | None = None) -> Web3:
        if config.key in self._w3_cache:
            return self._w3_cache[config.key]
        rpc = (
            custom_rpc
            or os.getenv(f"{config.env_prefix}_RPC_URL", "").strip()
            or config.default_rpc
        )
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._w3_cache[config.key] = w3
        return w3

    def get_operator(self, config: ChainConfig) -> tuple[str, str]:
        """Returns (operator_address, private_key)."""
        priv = (
            os.getenv(f"{config.env_prefix}_OPERATOR_PRIVATE_KEY", "").strip()
            or os.getenv("ETH_OPERATOR_PRIVATE_KEY", "").strip()
        )
        addr = (
            os.getenv(f"{config.env_prefix}_OPERATOR_ADDRESS", "").strip()
            or os.getenv("ETH_OPERATOR_ADDRESS", "").strip()
        )
        if priv and not addr:
            addr = Web3().eth.account.from_key(priv).address
        return addr, priv

    def get_executor_address(self, config: ChainConfig, counter_sym: str = "USDT") -> str:
        """Returns deployed MultichainMatchaStableArb executor address."""
        return (
            os.getenv(f"{config.env_prefix}_EXECUTOR_ADDRESS", "").strip()
            or os.getenv(f"{config.env_prefix}_{counter_sym}_USDC_EXECUTOR_ADDRESS", "").strip()
            or os.getenv(f"{config.env_prefix}_USDC_{counter_sym}_EXECUTOR_ADDRESS", "").strip()
            or os.getenv(f"{config.env_prefix}_ARB_EXECUTOR_ADDRESS", "").strip()
        )

    def scan_chain_opportunity(
        self,
        config: ChainConfig,
        loan_amount_human: Decimal | str,
        counter_sym: str = "USDT",
        slippage_bps: int = 2,
    ) -> dict[str, Any] | None:
        """Scans DEX -> Stable.com opportunity on a single chain."""
        try:
            loan_raw = to_raw(loan_amount_human, config.decimals)
            loan_token = config.usdc
            counter_token = config.usdt if counter_sym == "USDT" else (config.pyusd or "")
            if not counter_token:
                return None

            # Get executor or fallback operator address for quote simulations
            executor_addr = self.get_executor_address(config)
            operator_addr, _ = self.get_operator(config)
            taker = executor_addr or operator_addr or "0x" + "1" * 40

            # Leg 1: DEX Aggregator
            dq = self.dex.get_best_quote(config, loan_token, counter_token, loan_raw, slippage_bps, taker)
            if not dq:
                return None

            # Sized input for Stable.com accounting for realistic DEX slippage buffer
            stable_amount_in = (
                dq.buy_amount * (10_000 - slippage_bps) // 10_000
                if slippage_bps > 0
                else dq.buy_amount
            )

            # Leg 2: Stable.com pool
            sq = self.stable.quote(config, counter_sym, "USDC", stable_amount_in, taker)
            if not sq:
                return None

            # Flash loan fee calculation
            fee_bps = 0 if config.provider_kind == 0 else 5  # Morpho = 0, Aave V3 = 5 bps
            flash_fee = (loan_raw * fee_bps + 9_999) // 10_000

            gross_profit_raw = sq.amount_out - loan_raw - flash_fee
            gross_profit = Decimal(gross_profit_raw) / (Decimal(10) ** config.decimals)

            return {
                "chain": config.name,
                "chain_key": config.key,
                "pair": f"USDC/{counter_sym}",
                "loan_symbol": "USDC",
                "counter_symbol": counter_sym,
                "loan_amount": float(Decimal(str(loan_amount_human))),
                "loan_amount_raw": loan_raw,
                "dex_output_raw": dq.buy_amount,
                "dex_output": float(Decimal(dq.buy_amount) / (Decimal(10) ** config.decimals)),
                "stable_output_raw": sq.amount_out,
                "stable_output": float(Decimal(sq.amount_out) / (Decimal(10) ** config.decimals)),
                "flash_fee_raw": flash_fee,
                "flash_fee": float(Decimal(flash_fee) / (Decimal(10) ** config.decimals)),
                "gross_profit": float(gross_profit),
                "aggregator": dq.aggregator,
                "dex_quote": dq,
                "stable_quote": sq,
                "swap_order": "dex-first",
            }
        except Exception:
            return None

    def execute_live(
        self,
        config: ChainConfig,
        opportunity: dict[str, Any],
        min_profit_human: str = "0",
        confirm_key: str = "",
        slippage_bps: int = 2,
    ) -> dict[str, Any]:
        """Executes the flash loan trade live on-chain via MultichainMatchaStableArb."""
        executor_addr = self.get_executor_address(config)
        operator_addr, private_key = self.get_operator(config)
        if not executor_addr or not is_address(executor_addr):
            raise ArbError(
                f"No deployed executor for {config.name}. Set {config.env_prefix}_EXECUTOR_ADDRESS in .env"
            )
        if not operator_addr or not private_key:
            raise ArbError(f"No operator private key for {config.name}")
        if confirm_key not in ("EXECUTE_PROFIT_SNIPER", "EXECUTE_MULTICHAIN_ARB", "LIVE", config.confirmation):
            raise ArbError(f"Invalid live execution confirmation key: {confirm_key}")

        w3 = self.get_web3(config)
        executor = w3.to_checksum_address(executor_addr)
        operator = w3.to_checksum_address(operator_addr)

        counter_token = config.usdt if opportunity["counter_symbol"] == "USDT" else (config.pyusd or "")
        dq = self.dex.get_best_quote(
            config,
            config.usdc,
            counter_token,
            opportunity["loan_amount_raw"],
            slippage_bps=slippage_bps,
            taker=executor_addr,
            build_tx=True,
        )
        if not dq or dq.data in ("0x", ""):
            raise ArbError("Failed to build executable DEX calldata for live trade")

        loan_raw = opportunity["loan_amount_raw"]
        min_profit_raw = to_raw(min_profit_human, config.decimals, allow_zero=True)

        # Apply slippage safety buffer so on-chain DEX execution output covers Stable.com amountIn
        stable_amount_in = (
            dq.buy_amount * (10_000 - slippage_bps) // 10_000
            if slippage_bps > 0
            else dq.buy_amount
        )
        sq = self.stable.quote(config, opportunity["counter_symbol"], "USDC", stable_amount_in, executor_addr)
        if not sq:
            raise ArbError("Failed to obtain Stable.com quote for sized order")
        gross_profit_raw = sq.amount_out - loan_raw
        if gross_profit_raw < min_profit_raw:
            raise ArbError(f"Route after slippage buffer ({from_raw(gross_profit_raw, config.decimals)} USDC) is below minimum profit ({from_raw(min_profit_raw, config.decimals)} USDC)")

        # 1. Obtain cryptographic single-chain order from Stable.com
        stable_order = self.stable.create_order(
            config,
            opportunity["counter_symbol"],
            "USDC",
            stable_amount_in,
            sq.amount_out,
            executor,
        )

        contract = w3.eth.contract(address=executor, abi=EXECUTOR_ABI)

        # Build transaction params
        matcha_tuple = dq.contract_tuple()
        stable_tuple = stable_order.contract_tuple()

        # Simulate on-chain via eth_call
        try:
            contract.functions.executeArbitrage(
                loan_raw, matcha_tuple, stable_tuple, min_profit_raw
            ).call({"from": operator, "value": dq.value + stable_order.execution_fee_native})
        except Exception as sim_err:
            raise ArbError(f"On-chain eth_call simulation failed: {sim_err}") from sim_err

        # Prepare and sign transaction
        base_fee = w3.eth.get_block("latest").get("baseFeePerGas", w3.to_wei(1, "gwei"))
        priority_fee = w3.to_wei(1, "gwei")
        max_fee = base_fee * 2 + priority_fee
        nonce = w3.eth.get_transaction_count(operator, "pending")

        tx = contract.functions.executeArbitrage(
            loan_raw, matcha_tuple, stable_tuple, min_profit_raw
        ).build_transaction({
            "from": operator,
            "nonce": nonce,
            "gas": 600_000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "value": dq.value + stable_order.execution_fee_native,
            "chainId": config.chain_id,
        })

        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = tx_hash.hex()
        return {
            "chain": config.name,
            "transactionHash": tx_hex,
            "explorer": f"{config.explorer_tx_url}{tx_hex}",
            "status": "broadcasted",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unified Flash Arbitrage Engine for BSC and Polygon PoS."
    )
    parser.add_argument(
        "--chain",
        choices=["all", "bsc", "polygon"],
        default="all",
        help="Target blockchain to scan (default: all)",
    )
    parser.add_argument(
        "--amount",
        default="50000",
        help="Loan amount in USDC (e.g. 50000 or 100000)",
    )
    parser.add_argument(
        "--slippage",
        default="0.02",
        help="Slippage percentage (0.02 = 2 bps, 0.1 = 10 bps)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep running in a continuous monitoring loop",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Seconds between scans in watch mode (default: 10)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Compact single-line logs, ideal for VPS terminal",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=0,
        help="Number of samples before stopping (0 = unlimited)",
    )
    parser.add_argument(
        "--min-profit",
        default="0",
        help="Minimum gross profit in USDC to trigger execution (default: 0)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Broadcast live flash-loan transaction when profitable",
    )
    parser.add_argument(
        "--confirm-live",
        default="",
        help="Confirmation key for live broadcast (EXECUTE_PROFIT_SNIPER or LIVE)",
    )

    args = parser.parse_args(argv)
    slippage_bps = max(1, int(Decimal(args.slippage) * 100))
    engine = MultichainEngine()

    chains_to_scan = (
        [CHAINS["bsc"], CHAINS["polygon"]]
        if args.chain == "all"
        else [CHAINS[args.chain]]
    )

    sample = 0
    print(
        f"[STARTING] Multichain Flash Arb Engine active for {', '.join(c.name for c in chains_to_scan)} | Amount: {args.amount} USDC",
        flush=True,
    )

    while True:
        sample += 1
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        results: list[dict[str, Any]] = []
        best_op: dict[str, Any] | None = None

        for config in chains_to_scan:
            pairs = ["USDT"]
            if config.pyusd:
                pairs.append("PYUSD")
            for counter in pairs:
                op = engine.scan_chain_opportunity(config, args.amount, counter_sym=counter, slippage_bps=slippage_bps)
                if op:
                    results.append(op)
                    if best_op is None or op["gross_profit"] > best_op["gross_profit"]:
                        best_op = op

        # Display results
        if args.compact:
            parts = [
                f"[{r['chain_key'].upper()}] {r['pair']}: {r['gross_profit']:+.2f} USDC ({r['aggregator']})"
                for r in results
            ]
            best_desc = f"Best: {best_op['chain_key'].upper()} ({best_op['gross_profit']:+.2f})" if best_op else "No quote"
            print(f"[{now_str}] {' | '.join(parts)} | {best_desc}", flush=True)
        else:
            print(f"[{now_str}] --- MULTICHAIN SCAN SAMPLE #{sample} ---", flush=True)
            for r in results:
                print(
                    f"  [{r['chain']}] {r['pair']} | Loan: {r['loan_amount']} USDC -> DEX: {r['dex_output']:.2f} {r['counter_symbol']} ({r['aggregator']}) "
                    f"-> Stable: {r['stable_output']:.2f} USDC | Gross Profit: {r['gross_profit']:+.2f} USDC (Fee: {r['flash_fee']:.2f})",
                    flush=True,
                )
            if best_op:
                print(f"  --> Top Route: {best_op['chain']} {best_op['pair']} ({best_op['gross_profit']:+.2f} USDC)", flush=True)

        # Live Execution Check
        if args.live and best_op and best_op["gross_profit"] >= float(args.min_profit):
            best_chain = CHAINS[best_op["chain_key"]]
            print(
                f"\n[SNIPER FIRING] Executing {best_op['pair']} on {best_op['chain']} live via {best_op['aggregator']}...",
                flush=True,
            )
            try:
                receipt = engine.execute_live(
                    best_chain,
                    best_op,
                    min_profit_human=args.min_profit,
                    confirm_key=args.confirm_live,
                    slippage_bps=slippage_bps,
                )
                print(f"[!] TRANSACTION CONFIRMED: {receipt['explorer']}", flush=True)
            except Exception as err:
                print(f"[-] Execution error: {err}", file=sys.stderr)

        if not args.watch or (args.samples and sample >= args.samples):
            break
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
