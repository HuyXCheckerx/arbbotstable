"""Non-blocking EVM wallet balance monitoring for the web dashboard."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import threading
import time
from typing import Any, Callable, Iterable

import requests


BALANCE_OF_SELECTOR = "70a08231"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_evm_address(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def sanitize_error(error: Exception, rpc_url: str) -> str:
    detail = " ".join(str(error).replace(rpc_url, "<RPC>").split())
    return detail[:240] or type(error).__name__


def parse_rpc_integer(value: Any, label: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"invalid {label} response")
    try:
        return int(value, 16)
    except ValueError as error:
        raise ValueError(f"invalid {label} response") from error


def asset(raw: int, decimals: int, usd_price: float | None = None) -> dict[str, Any]:
    amount = int(raw) / (10**decimals)
    return {
        # Raw BSC token balances exceed JavaScript's safe integer range.
        "raw": str(int(raw)),
        "decimals": int(decimals),
        "amount": amount,
        "usd_value": amount * usd_price if usd_price is not None else None,
    }


@dataclass(frozen=True)
class EvmToken:
    symbol: str
    address: str
    decimals: int


@dataclass(frozen=True)
class EvmChain:
    key: str
    name: str
    chain_id: int
    native_symbol: str
    rpc_url: str
    wallet: str
    tokens: tuple[EvmToken, ...]

    @property
    def configured(self) -> bool:
        return bool(self.rpc_url) and is_evm_address(self.wallet)


def chain_configs_from_environment() -> tuple[EvmChain, ...]:
    return (
        EvmChain(
            key="ethereum",
            name="Ethereum",
            chain_id=1,
            native_symbol="ETH",
            rpc_url=os.environ.get("ETH_RPC_URL", "").strip(),
            wallet=os.environ.get("ETH_OPERATOR_ADDRESS", "").strip(),
            tokens=(
                EvmToken("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
                EvmToken("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
                EvmToken("PYUSD", "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8", 6),
            ),
        ),
        EvmChain(
            key="polygon",
            name="Polygon PoS",
            chain_id=137,
            native_symbol="POL",
            rpc_url=os.environ.get("POLYGON_RPC_URL", "").strip(),
            wallet=os.environ.get("POLYGON_OPERATOR_ADDRESS", "").strip(),
            tokens=(
                EvmToken("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
                EvmToken("USDT", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6),
            ),
        ),
        EvmChain(
            key="bsc",
            name="BNB Smart Chain",
            chain_id=56,
            native_symbol="BNB",
            rpc_url=os.environ.get("BSC_RPC_URL", "").strip(),
            wallet=os.environ.get("BSC_OPERATOR_ADDRESS", "").strip(),
            tokens=(
                EvmToken("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
                EvmToken("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
            ),
        ),
    )


def balance_of_calldata(wallet: str) -> str:
    if not is_evm_address(wallet):
        raise ValueError("invalid EVM wallet address")
    return "0x" + BALANCE_OF_SELECTOR + wallet[2:].lower().rjust(64, "0")


def _rpc_batch(chain: EvmChain) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = [
        {"jsonrpc": "2.0", "id": "chain", "method": "eth_chainId", "params": []},
        {
            "jsonrpc": "2.0",
            "id": "block",
            "method": "eth_blockNumber",
            "params": [],
        },
        {
            "jsonrpc": "2.0",
            "id": "native",
            "method": "eth_getBalance",
            "params": [chain.wallet, "latest"],
        },
    ]
    calldata = balance_of_calldata(chain.wallet)
    for token in chain.tokens:
        calls.append(
            {
                "jsonrpc": "2.0",
                "id": f"token:{token.symbol}",
                "method": "eth_call",
                "params": [{"to": token.address, "data": calldata}, "latest"],
            }
        )
    return calls


def read_chain_balances(
    chain: EvmChain,
    *,
    timeout_seconds: float,
    post: Callable[..., Any] = requests.post,
) -> dict[str, Any]:
    if not chain.configured:
        raise ValueError("RPC URL or operator address is not configured")
    started = time.monotonic()
    response = post(
        chain.rpc_url,
        json=_rpc_batch(chain),
        headers={"accept": "application/json", "content-type": "application/json"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("RPC endpoint does not support batch balance requests")
    results: dict[str, Any] = {}
    for item in payload:
        if not isinstance(item, dict) or "id" not in item:
            continue
        if item.get("error") is not None:
            raise ValueError(f"RPC {item['id']} failed: {item['error']}")
        results[str(item["id"])] = item.get("result")
    required = {"chain", "block", "native", *(f"token:{x.symbol}" for x in chain.tokens)}
    missing = required.difference(results)
    if missing:
        raise ValueError("RPC response omitted: " + ", ".join(sorted(missing)))
    returned_chain_id = parse_rpc_integer(results["chain"], "chain ID")
    if returned_chain_id != chain.chain_id:
        raise ValueError(
            f"RPC returned chain ID {returned_chain_id}; expected {chain.chain_id}"
        )

    tokens = {
        token.symbol: asset(
            parse_rpc_integer(results[f"token:{token.symbol}"], token.symbol),
            token.decimals,
            1.0,
        )
        for token in chain.tokens
    }
    return {
        "key": chain.key,
        "name": chain.name,
        "chain_id": chain.chain_id,
        "wallet": chain.wallet,
        "status": "ok",
        "error": None,
        "updated_at": utc_now(),
        "checked_at": utc_now(),
        "block_number": parse_rpc_integer(results["block"], "block number"),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "native": {
            "symbol": chain.native_symbol,
            **asset(parse_rpc_integer(results["native"], "native balance"), 18),
        },
        "tokens": tokens,
        "stablecoin_value_usd": sum(x["usd_value"] or 0 for x in tokens.values()),
    }


def _initial_chain_state(chain: EvmChain) -> dict[str, Any]:
    return {
        "key": chain.key,
        "name": chain.name,
        "chain_id": chain.chain_id,
        "wallet": chain.wallet,
        "status": "waiting" if chain.configured else "unconfigured",
        "error": None if chain.configured else "RPC URL or operator address is missing",
        "updated_at": None,
        "checked_at": None,
        "block_number": None,
        "latency_ms": None,
        "native": {"symbol": chain.native_symbol, **asset(0, 18)},
        "tokens": {
            token.symbol: asset(0, token.decimals, 1.0) for token in chain.tokens
        },
        "stablecoin_value_usd": 0.0,
    }


class EvmBalanceMonitor:
    """Poll EVM chains in the background and serve lock-protected snapshots."""

    def __init__(
        self,
        chains: Iterable[EvmChain] | None = None,
        *,
        poll_seconds: float = 15,
        timeout_seconds: float = 4,
        post: Callable[..., Any] = requests.post,
    ):
        self.chains = tuple(chains or chain_configs_from_environment())
        self.poll_seconds = max(2.0, float(poll_seconds))
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.post = post
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = {
            "updated_at": None,
            "chains": {chain.key: _initial_chain_state(chain) for chain in self.chains},
        }

    @classmethod
    def from_environment(cls) -> "EvmBalanceMonitor":
        return cls(
            poll_seconds=float(os.environ.get("WEB_EVM_BALANCE_POLL_SECONDS", "15")),
            timeout_seconds=float(os.environ.get("WEB_EVM_RPC_TIMEOUT_SECONDS", "4")),
        )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.state)

    def poll_once(self) -> dict[str, Any]:
        configured = [chain for chain in self.chains if chain.configured]
        if configured:
            with ThreadPoolExecutor(max_workers=min(3, len(configured))) as pool:
                futures = {
                    pool.submit(
                        read_chain_balances,
                        chain,
                        timeout_seconds=self.timeout_seconds,
                        post=self.post,
                    ): chain
                    for chain in configured
                }
                for future in as_completed(futures):
                    chain = futures[future]
                    try:
                        observed = future.result()
                    except Exception as error:
                        with self.lock:
                            previous = self.state["chains"][chain.key]
                            previous["status"] = (
                                "stale" if previous.get("updated_at") else "error"
                            )
                            previous["error"] = sanitize_error(error, chain.rpc_url)
                            previous["checked_at"] = utc_now()
                    else:
                        with self.lock:
                            self.state["chains"][chain.key] = observed
        with self.lock:
            self.state["updated_at"] = utc_now()
            return copy.deepcopy(self.state)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self.poll_once()
            self.stop_event.wait(self.poll_seconds)

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="evm-wallet-balance-monitor",
            daemon=True,
        )
        self.thread.start()

    def stop(self, timeout: float = 5) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)


__all__ = [
    "EvmBalanceMonitor",
    "EvmChain",
    "EvmToken",
    "asset",
    "balance_of_calldata",
    "chain_configs_from_environment",
    "read_chain_balances",
]
