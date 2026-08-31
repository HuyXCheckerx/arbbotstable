#!/usr/bin/env python3
"""Continuously execute guarded Ethereum and Solana stablecoin arbitrage."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Iterator

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "logs"
PLAN_DIR = LOG_DIR / "sniper-plans"
LOCK_PATH = LOG_DIR / ".crosschain-sniper.lock"
PID_PATH = LOG_DIR / "crosschain-sniper.pid"
STOP_PATH = LOG_DIR / ".crosschain-sniper.stop"
DASHBOARD_PATH = LOG_DIR / "sniper-dashboard.json"
LIVE_CONFIRMATION = "EXECUTE_PROFIT_SNIPER"
MINIMUM_ALLOWED_THRESHOLD = Decimal("5")
SOLANA_MINIMUM_ALLOWED_THRESHOLD = Decimal("1")
TOKEN_QUANTUM = Decimal("0.000001")
ROUTE_TOKENS = ("USDC", "USDG", "PYUSD")
DEFAULT_SWAP_ORDERS = ("dex-first", "stable-first")
DEFAULT_ROUTE_PAIRS = tuple(
    f"{loan}/{counter}"
    for loan in ROUTE_TOKENS
    for counter in ROUTE_TOKENS
    if loan != counter
)


class SniperError(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    chain: str
    pair: str
    swap_order: str = "stable-first"

    @property
    def loan(self) -> str:
        """Flash-loan token and the token returned at the end of the cycle."""
        return self.pair.split("/", 1)[0]

    @property
    def intermediate(self) -> str:
        """Counter-token used between the two atomic swap legs."""
        return self.pair.split("/", 1)[1]

    @property
    def stable_from(self) -> str:
        return self.loan if self.swap_order == "stable-first" else self.intermediate

    @property
    def stable_to(self) -> str:
        return self.intermediate if self.swap_order == "stable-first" else self.loan

    @property
    def dex_from(self) -> str:
        return self.loan if self.swap_order == "dex-first" else self.intermediate

    @property
    def dex_to(self) -> str:
        return self.intermediate if self.swap_order == "dex-first" else self.loan

    @property
    def dex_name(self) -> str:
        return "Jupiter" if self.chain == "solana" else "MetaMatcha"

    @property
    def display(self) -> str:
        if self.swap_order == "dex-first":
            return (
                f"{self.loan} -> {self.intermediate} ({self.dex_name}) -> "
                f"{self.loan} (Stable.com)"
            )
        return (
            f"{self.loan} -> {self.intermediate} (Stable.com) -> "
            f"{self.loan} ({self.dex_name})"
        )

    @property
    def key(self) -> str:
        return (
            f"{self.chain}-{self.swap_order}-loan-{self.loan.lower()}-via-"
            f"{self.intermediate.lower()}"
        )


@dataclass(frozen=True)
class Invocation:
    command: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True)
class Outcome:
    executed: bool
    detail: str
    category: str = "normal"
    gross_profit: str | None = None
    net_profit: str | None = None
    profit_token: str | None = None
    elapsed_seconds: float | None = None
    transaction: str | None = None


@dataclass(frozen=True)
class CooldownPolicy:
    transient_base_seconds: float
    transient_max_seconds: float
    provider_access_seconds: float
    no_route_seconds: float
    capacity_seconds: float
    unstable_capacity_seconds: float
    reverted_seconds: float


class AdaptiveBackoff:
    """Thread-safe, process-local backoff shared by both chain workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deadlines: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    def remaining(self, keys: tuple[str, ...]) -> float:
        now = time.monotonic()
        with self._lock:
            return max(
                (self._deadlines.get(key, 0.0) - now for key in keys),
                default=0.0,
            )

    def block(self, key: str, seconds: float) -> float:
        with self._lock:
            deadline = time.monotonic() + seconds
            self._deadlines[key] = max(self._deadlines.get(key, 0.0), deadline)
        return seconds

    def fail(self, key: str, base_seconds: float, max_seconds: float) -> float:
        with self._lock:
            failures = self._failures.get(key, 0) + 1
            self._failures[key] = failures
            delay = min(
                max_seconds,
                base_seconds * (2 ** min(failures - 1, 20)),
            )
            self._deadlines[key] = time.monotonic() + delay
        return delay

    def succeed(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._deadlines.pop(key, None)


def decimal_setting(name: str, fallback: str) -> Decimal:
    raw = os.getenv(name, fallback).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise SniperError(f"{name} must be a decimal number") from exc
    if not value.is_finite():
        raise SniperError(f"{name} must be finite")
    return value


def route_minimum_allowed_threshold(route: Route | None = None) -> Decimal:
    if route and route.chain == "solana":
        return SOLANA_MINIMUM_ALLOWED_THRESHOLD
    return MINIMUM_ALLOWED_THRESHOLD


def strict_execution_floor(threshold: Decimal, route: Route | None = None) -> Decimal:
    min_allowed = route_minimum_allowed_threshold(route)
    if not threshold.is_finite() or threshold < min_allowed:
        if route:
            raise SniperError(
                f"profit threshold for {route.chain}:{route.pair} must be at least {min_allowed} USD"
            )
        raise SniperError(f"profit threshold must be at least {min_allowed} USD")
    return threshold + TOKEN_QUANTUM


def route_execution_floor(route: Route, base_threshold: Decimal) -> Decimal:
    if route.chain == "solana":
        effective_threshold = (
            Decimal("1") if base_threshold == Decimal("5") else base_threshold
        )
        return strict_execution_floor(effective_threshold, route)
    return strict_execution_floor(base_threshold, route)


def amount_text(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".")


def selected_routes(
    chains: list[str],
    pairs: list[str],
    swap_orders: list[str],
) -> list[Route]:
    return [
        Route(chain, pair, swap_order)
        for chain in chains
        for pair in pairs
        for swap_order in swap_orders
    ]


def build_route_invocation(
    route: Route,
    *,
    live: bool,
    execution_floor: Decimal,
) -> Invocation:
    environment = dict(os.environ)
    floor = amount_text(execution_floor)
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    output_path = str(PLAN_DIR / f"{route.key}.json")

    if route.chain == "ethereum":
        command = [
            sys.executable,
            str(PROJECT_ROOT / "src" / "engines" / "eth_flash_arb_pyusd_usdc.py"),
            "--loan-token",
            route.loan,
            "--intermediate-token",
            route.intermediate,
            "--swap-order",
            route.swap_order,
            "--min-profit",
            floor,
            "--min-net-profit",
            floor,
            "--output",
            output_path,
        ]
        if live:
            command.extend(
                ["--send", "--confirm-mainnet", "EXECUTE_ATOMIC_ARB"]
            )
    elif route.chain == "solana":
        environment.pop("SOL_FLASH_ARB_SLIPPAGE_BPS", None)
        executable = "npx.cmd" if sys.platform == "win32" else "npx"
        command = [
            executable,
            "tsx",
            str(PROJECT_ROOT / "src" / "engines" / "solana_flash_arb.ts"),
            "--swap-order",
            route.swap_order,
        ]
        environment.update(
            {
                "SOL_FLASH_ARB_LOAN_TOKEN": route.loan,
                "SOL_FLASH_ARB_INTERMEDIATE_TOKEN": route.intermediate,
                "SOL_FLASH_ARB_SWAP_ORDER": route.swap_order,
                "SOL_FLASH_ARB_MIN_GROSS_PROFIT_USDC": floor,
                "SOL_FLASH_ARB_MIN_NET_PROFIT_USDC": floor,
                f"SOL_FLASH_ARB_MIN_GROSS_PROFIT_{route.loan}": floor,
                f"SOL_FLASH_ARB_MIN_NET_PROFIT_{route.loan}": floor,
                "SOL_FLASH_ARB_OUTPUT_PATH": output_path,
            }
        )
        if {route.stable_from, route.stable_to} == {"USDG", "PYUSD"}:
            # The PYUSD/USDG return market may need a Jupiter-managed hop
            # through USDC. The engine still enforces Solana's wire-size cap.
            environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"] = "false"
            environment["SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS"] = "24"
        else:
            environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"] = "true"
        if live:
            command.extend(
                [
                    "--send",
                    "--confirm-mainnet",
                    "EXECUTE_SOLANA_FLASH_ARB",
                ]
            )
    else:
        raise SniperError(f"unsupported sniper chain: {route.chain}")

    return Invocation(tuple(command), environment)


def execution_reference(route: Route, stdout: str) -> tuple[str | None, str | None]:
    if route.chain == "ethereum":
        try:
            plan = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            submitted = re.search(
                r"Submitted:\s*(https://etherscan\.io/tx/0x[0-9a-fA-F]+)",
                stdout,
            )
            return ("submitted", submitted.group(1)) if submitted else (None, None)
        tx_hash = plan.get("transactionHash") if isinstance(plan, dict) else None
        if not tx_hash:
            return None, None
        status = str(plan.get("transactionStatus") or "submitted").lower()
        link = f"https://etherscan.io/tx/0x{str(tx_hash).removeprefix('0x')}"
        return status, link
    try:
        plan = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        plan = None
    if isinstance(plan, dict) and plan.get("transactionSignature"):
        status = str(plan.get("transactionStatus") or "submitted").lower()
        signature = str(plan["transactionSignature"])
        return status, f"https://solscan.io/tx/{signature}"
    confirmed = re.search(r"Confirmed:\s*([1-9A-HJ-NP-Za-km-z]+)", stdout)
    if confirmed:
        return "confirmed", f"https://solscan.io/tx/{confirmed.group(1)}"
    expired = re.search(r"Expired:\s*([1-9A-HJ-NP-Za-km-z]+)", stdout)
    if expired:
        return "expired", f"https://solscan.io/tx/{expired.group(1)}"
    submitted = re.search(
        r"Submitted:\s*(?:https://solscan\.io/tx/)?([1-9A-HJ-NP-Za-km-z]+)",
        stdout,
    )
    if submitted:
        return "submitted", f"https://solscan.io/tx/{submitted.group(1)}"
    return None, None


def execution_detail(route: Route, stdout: str) -> str | None:
    status, link = execution_reference(route, stdout)
    return link if status == "confirmed" else None


def failure_category(detail: str) -> str:
    lowered = detail.lower()
    matcha_access_block = (
        "http 403" in lowered
        and ("cloudflare" in lowered or "access blocked" in lowered)
        and ("matcha" in lowered or "metamatcha" in lowered)
    )
    zero_ex_auth_block = (
        ("http 401" in lowered or "http 403" in lowered)
        and ("api.0x.org" in lowered or "official 0x" in lowered)
    )
    if matcha_access_block or zero_ex_auth_block:
        return "access-blocked-matcha"
    if (
        "no_routes_found" in lowered
        or "no routes found" in lowered
        or "no executable simulated quote" in lowered
        or "no executable matcha liquidity" in lowered
        or "solana 1232-byte size limit" in lowered
        or "exceeds solana 1232-byte size limit" in lowered
    ):
        return "no-route"
    if "capacity kept changing" in lowered:
        return "unstable-capacity"
    if (
        "pool capacity is below" in lowered
        or "usable capacity" in lowered and "below" in lowered
        or "pool has no remaining" in lowered
    ):
        return "capacity"
    transient = any(
        marker in lowered
        for marker in (
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "internal server error",
            "rate limits exceeded",
            "timed out",
            "temporarily failed",
            "connection reset",
        )
    )
    if transient and "stable.com" in lowered:
        return "transient-stable"
    if transient and "jupiter" in lowered:
        return "transient-jupiter"
    if transient and (
        "matcha" in lowered
        or "metamatcha" in lowered
        or "api.0x.org" in lowered
        or "official 0x" in lowered
    ):
        return "transient-matcha"
    if transient:
        return "transient-rpc"
    if "below" in lowered or "no executable opportunity" in lowered:
        return "unprofitable"
    return "failure"


def jupiter_market_key(route: Route) -> str:
    return f"jupiter:{route.dex_from}/{route.dex_to}"


def dex_market_key(route: Route) -> str:
    venue = "jupiter" if route.chain == "solana" else "metamatcha"
    return f"{venue}:{route.dex_from}/{route.dex_to}"


def dependency_keys(route: Route) -> tuple[str, ...]:
    keys = [f"stable:{route.chain}", f"rpc:{route.chain}"]
    if route.chain == "solana":
        keys.extend(("jupiter:solana", jupiter_market_key(route)))
    else:
        keys.extend(("metamatcha:ethereum", dex_market_key(route)))
    return tuple(keys)


def unresolved_submission(
    routes: list[Route],
) -> tuple[Route | None, Path, str] | None:
    route_paths = {PLAN_DIR / f"{route.key}.json": route for route in routes}
    try:
        candidate_paths = set(PLAN_DIR.glob("*.json")) | set(route_paths)
    except OSError:
        candidate_paths = set(route_paths)
    for path in sorted(candidate_paths, key=str):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(plan, dict) or plan.get("transactionStatus") != "submitted":
            continue
        reference = plan.get("transactionHash") or plan.get("transactionSignature")
        if reference:
            return route_paths.get(path), path, str(reference)
    return None


def concise_failure(stdout: str, stderr: str, returncode: int) -> str:
    combined = "\n".join(part for part in (stderr, stdout) if part)
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            return line[6:].strip()
    for line in reversed(lines):
        if "No executable opportunity" in line or "below" in line:
            return line.removeprefix("Error:").strip()
    return lines[-1] if lines else f"engine exited with status {returncode}"


def readable_failure(route: Route, detail: str, category: str) -> str:
    """Turn provider/engine jargon into a short operator-facing explanation."""
    lowered = detail.lower()
    dex_leg = f"{route.dex_from} -> {route.dex_to}"
    if category == "no-route":
        if "1232-byte" in lowered:
            return (
                f"{route.dex_name} found a {dex_leg} route, but the full "
                "atomic transaction is too large for Solana"
            )
        return f"{route.dex_name} has no executable {dex_leg} route right now"
    if category == "transient-stable":
        status = re.search(r"HTTP\s+(\d{3})", detail, re.IGNORECASE)
        suffix = f" (HTTP {status.group(1)})" if status else (f" ({detail})" if detail else "")
        return f"Stable.com is temporarily unavailable{suffix}"
    if category == "transient-jupiter":
        status = re.search(r"HTTP\s+(\d{3})", detail, re.IGNORECASE)
        suffix = f" (HTTP {status.group(1)})" if status else (f" ({detail})" if detail else "")
        return f"Jupiter is temporarily unavailable{suffix}"
    if category == "transient-matcha":
        status = re.search(r"HTTP\s+(\d{3})", detail, re.IGNORECASE)
        suffix = f" (HTTP {status.group(1)})" if status else (f" ({detail})" if detail else "")
        return f"MetaMatcha is temporarily unavailable{suffix}"
    if category == "access-blocked-matcha":
        if "api.0x.org" in lowered or "official 0x" in lowered:
            return (
                "the official 0x API rejected this request (HTTP 401/403); "
                "check ETH_ARB_ZERO_EX_API_KEY and this machine's egress"
            )
        return (
            "MetaMatcha denied this machine's network access (Cloudflare HTTP 403); "
            "configure the official 0x quote provider or use an allowed egress"
        )
    if category == "transient-rpc":
        status = re.search(r"HTTP\s+(\d{3})", detail, re.IGNORECASE)
        suffix = f" (HTTP {status.group(1)})" if status else (f" ({detail})" if detail else "")
        return f"{route.chain.title()} RPC is temporarily unavailable{suffix}"
    if category == "capacity":
        return "Stable.com does not currently have enough usable input capacity"
    if category == "unstable-capacity":
        return "Stable.com capacity changed repeatedly while the route was being sized"
    if category == "unprofitable":
        comparison = re.search(
            r"(?:floor:\s*|guaranteed net\s+)([-+\d.]+\s+[A-Z]+)\s+"
            r"(?:is below|<)\s+([-+\d.]+\s+[A-Z]+)",
            detail,
            re.IGNORECASE,
        )
        if comparison:
            return (
                f"guaranteed result {comparison.group(1)}; required "
                f"{comparison.group(2)}"
            )
        return detail.removeprefix("No executable opportunity: ")
    return detail


def dry_run_detail(route: Route, stdout: str, elapsed: float) -> str:
    if route.chain == "ethereum":
        try:
            plan = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            plan = None
        if isinstance(plan, dict) and plan.get("predictedNetProfit") is not None:
            return (
                f"guaranteed net {plan['predictedNetProfit']} {route.loan}; "
                f"simulation passed in {elapsed:.1f}s; not sent"
            )
    match = re.search(
        r"Guaranteed net result:\s*([-+\d.]+\s+[A-Z]+)",
        stdout,
        re.IGNORECASE,
    )
    if match:
        return f"guaranteed net {match.group(1)}; simulation passed in {elapsed:.1f}s; not sent"
    return f"executable simulation passed in {elapsed:.1f}s; not sent"


def outcome_label(outcome: Outcome) -> str:
    if outcome.category == "confirmed":
        return "CONFIRMED"
    if outcome.category == "submitted":
        return "STOPPED"
    if outcome.category == "reverted":
        return "REVERTED"
    if outcome.category == "expired":
        return "DROPPED"
    if outcome.category == "dropped":
        return "DROPPED"
    if outcome.category == "eligible":
        return "READY"
    if outcome.category == "unprofitable":
        return "NO TRADE"
    if outcome.category in {
        "no-route",
        "capacity",
        "unstable-capacity",
        "transient-stable",
        "transient-jupiter",
        "transient-matcha",
        "transient-rpc",
        "access-blocked-matcha",
    }:
        return "PAUSED"
    return "ERROR"


def dependency_label(key: str) -> str:
    provider, _, market = key.partition(":")
    names = {
        "stable": "Stable.com",
        "jupiter": "Jupiter",
        "metamatcha": "MetaMatcha",
        "rpc": "chain RPC",
    }
    label = names.get(provider, provider)
    return f"{label} {market}".strip()


def parse_gas_fee_gwei(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text.endswith("gwei"):
        text = text.removesuffix("gwei").strip()
        factor = Decimal(1)
    elif text.endswith("wei"):
        text = text.removesuffix("wei").strip()
        factor = Decimal("1e-9")
    else:
        factor = Decimal(1)
    try:
        amt = Decimal(text)
    except InvalidOperation:
        raise SniperError(f"invalid gas fee limit: {value!r}")
    if not amt.is_finite() or amt <= 0:
        raise SniperError(f"gas fee limit must be positive: {value!r}")
    scaled = amt * factor
    if factor == 1 and amt >= Decimal(1000):
        scaled = amt / Decimal(10**9)
    return scaled


def fetch_ethereum_base_fee_gwei(
    rpc_url: str,
    timeout: float = 5.0,
) -> Decimal | None:
    if not rpc_url:
        return None
    try:
        import urllib.request

        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "eth_getBlockByNumber",
                "params": ["latest", False],
                "id": 1,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            rpc_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "arbbot-sniper/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            result = body.get("result")
            if not isinstance(result, dict):
                return None
            base_fee_hex = result.get("baseFeePerGas")
            if not base_fee_hex:
                return None
            base_fee_wei = int(base_fee_hex, 16)
            return Decimal(base_fee_wei) / Decimal(10**9)
    except Exception:
        return None


def subprocess_output_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _profit_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return amount_text(parsed) if parsed.is_finite() else None


def profit_metrics(route: Route, stdout: str, stderr: str = "") -> tuple[str | None, str | None]:
    """Extract guaranteed gross/net profit without depending on one engine format."""
    try:
        plan = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        plan = None
    if isinstance(plan, dict):
        gross = _profit_value(plan.get("grossProfit"))
        net = _profit_value(plan.get("predictedNetProfit"))
        if gross is not None or net is not None:
            return gross, net

    combined = "\n".join(part for part in (stdout, stderr) if part)
    patterns = {
        "gross": (
            r"Guaranteed gross result:\s*([-+\d.]+)",
            r"Gross Profit:\s*([-+\d.]+)",
        ),
        "net": (
            r"Guaranteed net result:\s*([-+\d.]+)",
            r"Predicted Net Profit:\s*([-+\d.]+)",
            r"guaranteed net\s+([-+\d.]+)",
        ),
    }

    def first_match(candidates: tuple[str, ...]) -> str | None:
        for pattern in candidates:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return _profit_value(match.group(1))
        return None

    return first_match(patterns["gross"]), first_match(patterns["net"])


class SniperDashboardFeed:
    """Thread-safe, atomic status feed consumed by the local web dashboard."""

    _PAUSED_CATEGORIES = {
        "no-route",
        "capacity",
        "unstable-capacity",
        "transient-stable",
        "transient-jupiter",
        "transient-matcha",
        "transient-rpc",
        "access-blocked-matcha",
    }

    def __init__(
        self,
        path: Path,
        routes: list[Route],
        *,
        live: bool,
        base_threshold: Decimal,
    ) -> None:
        self.path = path
        self._lock = threading.Lock()
        started_at = self._now()
        route_states: dict[str, dict[str, object]] = {}
        for route in routes:
            route_states[route.key] = self._route_record(
                route,
                route_execution_floor(route, base_threshold),
                state="WAITING",
                detail="Waiting for the first check",
            )
        self._state: dict[str, object] = {
            "schema_version": 1,
            "session": {
                "status": "running",
                "status_label": "Sniper running",
                "mode": "live" if live else "dry-run",
                "pid": os.getpid(),
                "started_at": started_at,
                "updated_at": started_at,
                "route_count": len(routes),
                "chains": sorted({route.chain for route in routes}),
            },
            "summary": {
                "checks": 0,
                "ready": 0,
                "confirmed": 0,
                "submitted": 0,
                "no_trade": 0,
                "paused": 0,
                "errors": 0,
            },
            "active": {chain: None for chain in sorted({route.chain for route in routes})},
            "routes": route_states,
            "recent_results": [],
            "last_execution": None,
        }
        with self._lock:
            self._write_locked()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _route_record(
        route: Route,
        execution_floor: Decimal,
        *,
        state: str,
        detail: str,
    ) -> dict[str, object]:
        return {
            "key": route.key,
            "chain": route.chain,
            "pair": route.pair,
            "swap_order": route.swap_order,
            "loan_token": route.loan,
            "counter_token": route.intermediate,
            "flow": route.display,
            "execution_floor": amount_text(execution_floor),
            "profit_token": route.loan,
            "state": state,
            "detail": detail,
            "checked_at": None,
            "started_at": None,
            "duration_seconds": None,
            "gross_profit": None,
            "net_profit": None,
            "transaction": None,
            "cooldown_until": None,
            "cooldown_reason": None,
        }

    def _write_locked(self) -> None:
        session = self._state["session"]
        assert isinstance(session, dict)
        session["updated_at"] = self._now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self._state, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError:
            # Dashboard observability must never interrupt route execution.
            temporary.unlink(missing_ok=True)

    def begin_check(self, route: Route, execution_floor: Decimal) -> None:
        with self._lock:
            routes = self._state["routes"]
            active = self._state["active"]
            summary = self._state["summary"]
            assert isinstance(routes, dict) and isinstance(active, dict) and isinstance(summary, dict)
            record = self._route_record(
                route,
                execution_floor,
                state="CHECKING",
                detail="Requesting and simulating both atomic swap legs",
            )
            record["started_at"] = self._now()
            previous = routes.get(route.key)
            if isinstance(previous, dict):
                record["checked_at"] = previous.get("checked_at")
            routes[route.key] = record
            active[route.chain] = dict(record)
            summary["checks"] = int(summary.get("checks", 0)) + 1
            self._write_locked()

    def record_result(self, route: Route, execution_floor: Decimal, outcome: Outcome) -> None:
        label = outcome_label(outcome)
        with self._lock:
            routes = self._state["routes"]
            active = self._state["active"]
            summary = self._state["summary"]
            recent = self._state["recent_results"]
            assert isinstance(routes, dict) and isinstance(active, dict)
            assert isinstance(summary, dict) and isinstance(recent, list)
            record = self._route_record(
                route,
                execution_floor,
                state=label,
                detail=outcome.detail,
            )
            record.update(
                {
                    "checked_at": self._now(),
                    "duration_seconds": outcome.elapsed_seconds,
                    "gross_profit": outcome.gross_profit,
                    "net_profit": outcome.net_profit,
                    "profit_token": outcome.profit_token or route.loan,
                    "transaction": outcome.transaction,
                    "category": outcome.category,
                }
            )
            routes[route.key] = record
            active[route.chain] = None
            counter = (
                "confirmed" if outcome.category == "confirmed" else
                "submitted" if outcome.category == "submitted" else
                "ready" if outcome.category == "eligible" else
                "no_trade" if outcome.category == "unprofitable" else
                "paused" if outcome.category in self._PAUSED_CATEGORIES else
                "errors"
            )
            summary[counter] = int(summary.get(counter, 0)) + 1
            recent.insert(0, dict(record))
            del recent[100:]
            if outcome.category in {
                "confirmed", "submitted", "reverted", "expired", "dropped"
            }:
                self._state["last_execution"] = dict(record)
            self._write_locked()

    def record_cooldown(self, route: Route, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        with self._lock:
            routes = self._state["routes"]
            assert isinstance(routes, dict)
            record = routes.get(route.key)
            if isinstance(record, dict):
                record["cooldown_until"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=seconds)
                ).isoformat(timespec="seconds")
                record["cooldown_reason"] = reason
            self._write_locked()

    def stop(self, label: str = "Sniper stopped") -> None:
        with self._lock:
            session = self._state["session"]
            active = self._state["active"]
            assert isinstance(session, dict) and isinstance(active, dict)
            session["status"] = "stopped"
            session["status_label"] = label
            for chain in active:
                active[chain] = None
            self._write_locked()


def run_route(
    route: Route,
    *,
    live: bool,
    execution_floor: Decimal,
    timeout_seconds: float,
) -> Outcome:
    invocation = build_route_invocation(
        route,
        live=live,
        execution_floor=execution_floor,
    )
    started = time.monotonic()
    started_wall = time.time()
    try:
        result = subprocess.run(
            invocation.command,
            cwd=PROJECT_ROOT,
            env=invocation.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        captured = "\n".join(
            (
                subprocess_output_text(exc.stdout),
                subprocess_output_text(exc.stderr),
            )
        )
        gross_profit, net_profit = profit_metrics(route, captured)
        transaction_status, transaction = execution_reference(route, captured)
        plan_path = PLAN_DIR / f"{route.key}.json"
        try:
            if (
                transaction_status is None
                and plan_path.stat().st_mtime >= started_wall
            ):
                transaction_status, transaction = execution_reference(
                    route,
                    plan_path.read_text(encoding="utf-8"),
                )
        except OSError:
            pass
        if transaction_status == "confirmed" and transaction:
            return Outcome(
                True,
                f"confirmed before the engine timed out after {timeout_seconds:g}s: "
                f"{transaction}",
                "confirmed",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=timeout_seconds,
                transaction=transaction,
            )
        if transaction_status == "submitted" and transaction:
            return Outcome(
                False,
                f"submitted but the engine timed out after {timeout_seconds:g}s: "
                f"{transaction}",
                "submitted",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=timeout_seconds,
                transaction=transaction,
            )
        if transaction_status == "reverted" and transaction:
            return Outcome(
                False,
                f"reverted before the engine timed out after {timeout_seconds:g}s: "
                f"{transaction}",
                "reverted",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=timeout_seconds,
                transaction=transaction,
            )
        if transaction_status == "expired" and transaction:
            return Outcome(
                False,
                f"expired without landing before the engine timed out after "
                f"{timeout_seconds:g}s: {transaction}; continuing",
                "expired",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=timeout_seconds,
                transaction=transaction,
            )
        if transaction_status == "dropped" and transaction:
            return Outcome(
                False,
                f"not found with an unused nonce before the engine timed out after "
                f"{timeout_seconds:g}s: {transaction}; continuing",
                "dropped",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=timeout_seconds,
                transaction=transaction,
            )
        return Outcome(
            False,
            f"quote timed out after {timeout_seconds:g}s",
            "transient-rpc",
            gross_profit=gross_profit,
            net_profit=net_profit,
            profit_token=route.loan,
            elapsed_seconds=timeout_seconds,
        )
    except OSError as exc:
        return Outcome(
            False,
            f"could not start engine: {exc}",
            elapsed_seconds=time.monotonic() - started,
            profit_token=route.loan,
        )

    elapsed = time.monotonic() - started
    gross_profit, net_profit = profit_metrics(
        route,
        result.stdout or "",
        result.stderr or "",
    )
    transaction_status, transaction = execution_reference(route, result.stdout or "")

    def completed_outcome(executed: bool, detail: str, category: str) -> Outcome:
        return Outcome(
            executed,
            detail,
            category,
            gross_profit=gross_profit,
            net_profit=net_profit,
            profit_token=route.loan,
            elapsed_seconds=elapsed,
            transaction=transaction,
        )

    if transaction_status == "confirmed" and transaction:
        return completed_outcome(
            True,
            f"confirmed in {elapsed:.1f}s: {transaction}",
            "confirmed",
        )
    if transaction_status == "submitted" and transaction:
        failure = (
            concise_failure(result.stdout or "", result.stderr or "", result.returncode)
            if result.returncode
            else "receipt confirmation was not observed"
        )
        return completed_outcome(
            False,
            f"submitted in {elapsed:.1f}s but confirmation is ambiguous: "
            f"{transaction} ({failure})",
            "submitted",
        )
    if transaction_status == "reverted" and transaction:
        return completed_outcome(
            False,
            f"reverted in {elapsed:.1f}s: {transaction}",
            "reverted",
        )
    if transaction_status == "expired" and transaction:
        return completed_outcome(
            False,
            f"expired without landing in {elapsed:.1f}s: {transaction}; continuing",
            "expired",
        )
    if transaction_status == "dropped" and transaction:
        return completed_outcome(
            False,
            f"not found and nonce remains unused after {elapsed:.1f}s: "
            f"{transaction}; continuing",
            "dropped",
        )
    if result.returncode == 0:
        if live:
            return completed_outcome(
                False,
                f"engine succeeded in {elapsed:.1f}s without a transaction reference",
                "failure",
            )
        return completed_outcome(
            False,
            dry_run_detail(route, result.stdout or "", elapsed),
            "eligible",
        )

    detail = concise_failure(result.stdout or "", result.stderr or "", result.returncode)
    category = failure_category(detail)
    return completed_outcome(
        False,
        readable_failure(route, detail, category),
        category,
    )


def configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("crosschain-sniper")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOG_DIR / "crosschain-sniper.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = open_process(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        close_handle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_sniper_pid() -> int | None:
    try:
        pid = int(PID_PATH.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError):
        return None
    return pid if process_is_running(pid) else None


@contextmanager
def single_instance() -> Iterator[None]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("a+b")
    lock.seek(0, os.SEEK_END)
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        raise SniperError("another cross-chain sniper instance is already running") from exc

    STOP_PATH.unlink(missing_ok=True)
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="ascii")
    try:
        yield
    finally:
        try:
            PID_PATH.unlink(missing_ok=True)
            STOP_PATH.unlink(missing_ok=True)
        finally:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def worker(
    chain: str,
    routes: list[Route],
    *,
    live: bool,
    base_threshold: Decimal,
    interval_seconds: float,
    cooldown_seconds: float,
    timeout_seconds: float,
    cooldown_policy: CooldownPolicy,
    backoff: AdaptiveBackoff,
    once: bool,
    stop: threading.Event,
    logger: logging.Logger,
    dashboard: SniperDashboardFeed | None = None,
    eth_max_base_fee_gwei: Decimal | None = None,
    eth_rpc_url: str = "https://ethereum-rpc.publicnode.com",
) -> None:
    route_deadlines: dict[str, float] = {}
    while not stop.is_set():
        if chain == "ethereum" and eth_max_base_fee_gwei is not None:
            current_base_fee = fetch_ethereum_base_fee_gwei(eth_rpc_url, timeout=5.0)
            if current_base_fee is not None and current_base_fee > eth_max_base_fee_gwei:
                logger.info(
                    "PAUSE   | %-17s | %.0fs | current base fee (%.3f Gwei) exceeds limit (%.3f Gwei); waiting for lower gas...",
                    "Ethereum gas",
                    interval_seconds,
                    current_base_fee,
                    eth_max_base_fee_gwei,
                )
                if once:
                    return
                if stop.wait(interval_seconds):
                    return
                continue
        for route in routes:
            if stop.is_set():
                return
            now = time.monotonic()
            if route_deadlines.get(route.key, 0.0) > now:
                continue
            if backoff.remaining(dependency_keys(route)) > 0:
                continue
            floor = route_execution_floor(route, base_threshold)
            logger.info("CHECK   | %-8s | %s", chain.title(), route.display)
            if dashboard:
                dashboard.begin_check(route, floor)
            outcome = run_route(
                route,
                live=live,
                execution_floor=floor,
                timeout_seconds=timeout_seconds,
            )
            if outcome.category in {"submitted", "reverted", "failure"}:
                level = logging.ERROR
            elif outcome.executed:
                level = logging.WARNING
            else:
                level = logging.INFO
            logger.log(
                level,
                "RESULT  | %-9s | %-8s | %s | %s",
                outcome_label(outcome),
                chain.title(),
                route.display,
                outcome.detail,
            )
            if dashboard:
                dashboard.record_result(route, floor, outcome)
            if outcome.category == "submitted":
                logger.error(
                    "PAUSE   | %-17s | %.0fs | submission is unresolved; "
                    "the script and other chain continue",
                    f"{chain.title()} submissions",
                    cooldown_policy.transient_base_seconds,
                )
                backoff.block(
                    f"rpc:{route.chain}",
                    cooldown_policy.transient_base_seconds,
                )
                if dashboard:
                    dashboard.record_cooldown(
                        route,
                        cooldown_policy.transient_base_seconds,
                        "Submission confirmation is unresolved",
                    )
                continue
            if outcome.category.startswith("transient-"):
                dependency = {
                    "transient-stable": f"stable:{route.chain}",
                    "transient-jupiter": "jupiter:solana",
                    "transient-matcha": "metamatcha:ethereum",
                    "transient-rpc": f"rpc:{route.chain}",
                }[outcome.category]
                delay = backoff.fail(
                    dependency,
                    cooldown_policy.transient_base_seconds,
                    cooldown_policy.transient_max_seconds,
                )
                status = re.search(r"HTTP\s+(\d{3})", outcome.detail, re.IGNORECASE)
                suffix = f" (HTTP {status.group(1)})" if status else ""
                logger.info(
                    "PAUSE   | %-17s | %.0fs | temporary provider failure%s",
                    dependency_label(dependency),
                    delay,
                    suffix,
                )
                if dashboard:
                    dashboard.record_cooldown(
                        route,
                        delay,
                        f"Temporary {dependency_label(dependency)} failure{suffix}",
                    )
            else:
                backoff.succeed(f"stable:{route.chain}")
                backoff.succeed(f"rpc:{route.chain}")
                if route.chain == "solana":
                    backoff.succeed("jupiter:solana")
                else:
                    backoff.succeed("metamatcha:ethereum")

            if outcome.category == "no-route":
                dependency = dex_market_key(route)
                backoff.block(dependency, cooldown_policy.no_route_seconds)
                logger.info(
                    "PAUSE   | %-17s | %.0fs | return market unavailable",
                    dependency_label(dependency),
                    cooldown_policy.no_route_seconds,
                )
                if dashboard:
                    dashboard.record_cooldown(
                        route,
                        cooldown_policy.no_route_seconds,
                        "Return market is unavailable",
                    )
            elif outcome.category == "access-blocked-matcha":
                dependency = "metamatcha:ethereum"
                backoff.block(dependency, cooldown_policy.provider_access_seconds)
                logger.info(
                    "PAUSE   | %-17s | %.0fs | provider denied this machine's access",
                    dependency_label(dependency),
                    cooldown_policy.provider_access_seconds,
                )
                if dashboard:
                    dashboard.record_cooldown(
                        route,
                        cooldown_policy.provider_access_seconds,
                        "MetaMatcha denied this machine's access",
                    )
            elif outcome.category == "capacity":
                route_deadlines[route.key] = (
                    time.monotonic() + cooldown_policy.capacity_seconds
                )
                logger.info(
                    "PAUSE   | %-17s | %.0fs | insufficient Stable.com capacity",
                    route.display,
                    cooldown_policy.capacity_seconds,
                )
                if dashboard:
                    dashboard.record_cooldown(
                        route,
                        cooldown_policy.capacity_seconds,
                        "Insufficient Stable.com capacity",
                    )
            elif outcome.category == "unstable-capacity":
                route_deadlines[route.key] = (
                    time.monotonic() + cooldown_policy.unstable_capacity_seconds
                )
                logger.info(
                    "PAUSE   | %-17s | %.0fs | Stable.com capacity is changing",
                    route.display,
                    cooldown_policy.unstable_capacity_seconds,
                )
                if dashboard:
                    dashboard.record_cooldown(
                        route,
                        cooldown_policy.unstable_capacity_seconds,
                        "Stable.com capacity is changing",
                    )
            elif outcome.category == "reverted":
                route_deadlines[route.key] = (
                    time.monotonic() + cooldown_policy.reverted_seconds
                )
                logger.info(
                    "PAUSE   | %-17s | %.0fs | transaction reverted",
                    route.display,
                    cooldown_policy.reverted_seconds,
                )
                if dashboard:
                    dashboard.record_cooldown(
                        route,
                        cooldown_policy.reverted_seconds,
                        "Transaction reverted",
                    )
            if outcome.executed and dashboard:
                dashboard.record_cooldown(
                    route,
                    cooldown_seconds,
                    "Post-execution cooldown",
                )
            if outcome.executed and stop.wait(cooldown_seconds):
                return
        if once:
            return
        if stop.wait(interval_seconds):
            return


def watch_for_stop_request(stop: threading.Event, logger: logging.Logger) -> None:
    while not stop.wait(0.5):
        if STOP_PATH.exists():
            logger.info("SAFETY  | STOPPING  | cooperative stop requested")
            stop.set()
            return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold-usd",
        type=Decimal,
        default=decimal_setting("SNIPER_PROFIT_THRESHOLD_USD", "5"),
        help=(
            "execute only above this net starting-token profit "
            "(default 5 for Ethereum; 1 for Solana)"
        ),
    )
    parser.add_argument(
        "--chains",
        nargs="+",
        choices=("ethereum", "solana"),
        default=["ethereum", "solana"],
    )
    parser.add_argument(
        "--pairs",
        "--routes",
        dest="pairs",
        nargs="+",
        choices=DEFAULT_ROUTE_PAIRS,
        default=list(DEFAULT_ROUTE_PAIRS),
        metavar="LOAN/COUNTER",
        help=(
            "flash-loan and counter-token pair (default: all six ordered "
            "USDC/USDG/PYUSD pairs)"
        ),
    )
    parser.add_argument(
        "--orders",
        "--swap-orders",
        dest="swap_orders",
        nargs="+",
        choices=DEFAULT_SWAP_ORDERS,
        default=list(DEFAULT_SWAP_ORDERS),
        help="venue order to check (default: both dex-first and stable-first)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=float(os.getenv("SNIPER_INTERVAL_SECONDS", "2")),
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_COOLDOWN_SECONDS", "15")),
    )
    parser.add_argument(
        "--route-timeout-seconds",
        type=float,
        default=float(os.getenv("SNIPER_ROUTE_TIMEOUT_SECONDS", "300")),
    )
    parser.add_argument(
        "--transient-backoff-seconds",
        type=float,
        default=float(os.getenv("SNIPER_TRANSIENT_BACKOFF_SECONDS", "30")),
    )
    parser.add_argument(
        "--max-transient-backoff-seconds",
        type=float,
        default=float(os.getenv("SNIPER_MAX_TRANSIENT_BACKOFF_SECONDS", "300")),
    )
    parser.add_argument(
        "--provider-access-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_PROVIDER_ACCESS_COOLDOWN_SECONDS", "3600")),
    )
    parser.add_argument(
        "--no-route-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_NO_ROUTE_COOLDOWN_SECONDS", "300")),
    )
    parser.add_argument(
        "--capacity-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_CAPACITY_COOLDOWN_SECONDS", "300")),
    )
    parser.add_argument(
        "--unstable-capacity-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_UNSTABLE_CAPACITY_COOLDOWN_SECONDS", "30")),
    )
    parser.add_argument(
        "--reverted-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_REVERTED_COOLDOWN_SECONDS", "60")),
    )
    parser.add_argument(
        "--eth-max-base-fee-gwei",
        "--eth-max-gas-gwei",
        dest="eth_max_base_fee_gwei",
        type=parse_gas_fee_gwei,
        default=parse_gas_fee_gwei(
            os.getenv("ETH_MAX_BASE_FEE_GWEI")
            or os.getenv("SNIPER_ETH_MAX_BASE_FEE_GWEI")
            or os.getenv("ETH_ARB_MAX_BASE_FEE_GWEI")
            or os.getenv("ETH_MAX_GAS_FEE")
        ),
        help=(
            "maximum allowed Ethereum base fee in Gwei (or Wei) before pausing Ethereum trading "
            "(e.g. 1.0, 1.5, or '1000000000 wei')"
        ),
    )
    parser.add_argument("--once", action="store_true", help="check each route once")
    parser.add_argument("--live", action="store_true", help="allow guarded broadcasts")
    parser.add_argument("--confirm-live")
    parser.add_argument(
        "--request-stop",
        action="store_true",
        help="ask the running sniper to stop after its active route checks",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    args = parse_args(argv)
    if args.request_stop:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        pid = running_sniper_pid()
        if pid is None:
            PID_PATH.unlink(missing_ok=True)
            STOP_PATH.unlink(missing_ok=True)
            raise SniperError("no running sniper process was found")
        STOP_PATH.write_text(f"{os.getpid()}\n", encoding="ascii")
        print(
            f"Stop requested for sniper PID {pid}; it will exit after active "
            "route checks finish."
        )
        return 0
    if not math.isfinite(args.interval_seconds) or args.interval_seconds < 0.25:
        raise SniperError("--interval-seconds must be at least 0.25")
    if not math.isfinite(args.cooldown_seconds) or args.cooldown_seconds < 0:
        raise SniperError("--cooldown-seconds cannot be negative")
    if not math.isfinite(args.route_timeout_seconds) or args.route_timeout_seconds < 30:
        raise SniperError("--route-timeout-seconds must be at least 30")
    cooldown_values = {
        "--transient-backoff-seconds": args.transient_backoff_seconds,
        "--max-transient-backoff-seconds": args.max_transient_backoff_seconds,
        "--provider-access-cooldown-seconds": args.provider_access_cooldown_seconds,
        "--no-route-cooldown-seconds": args.no_route_cooldown_seconds,
        "--capacity-cooldown-seconds": args.capacity_cooldown_seconds,
        "--unstable-capacity-cooldown-seconds": args.unstable_capacity_cooldown_seconds,
        "--reverted-cooldown-seconds": args.reverted_cooldown_seconds,
    }
    for name, value in cooldown_values.items():
        if not math.isfinite(value) or value < 0:
            raise SniperError(f"{name} must be finite and non-negative")
    if args.max_transient_backoff_seconds < args.transient_backoff_seconds:
        raise SniperError(
            "--max-transient-backoff-seconds cannot be below "
            "--transient-backoff-seconds"
        )
    if args.live and args.confirm_live != LIVE_CONFIRMATION:
        raise SniperError(
            f"--live requires --confirm-live {LIVE_CONFIRMATION}"
        )

    routes = selected_routes(args.chains, args.pairs, args.swap_orders)
    for route in routes:
        route_execution_floor(route, args.threshold_usd)
    logger = configure_logging()
    unresolved = unresolved_submission(routes)
    if unresolved:
        route, path, reference = unresolved
        route_label = (
            f"{route.chain}:{route.pair}:{route.swap_order}"
            if route
            else "a prior or unknown route"
        )
        logger.error(
            "RECOVER | CONTINUE  | unresolved prior submission for %s: %s (%s); "
            "the script will not stop",
            route_label,
            reference,
            path,
        )
    logger.info(
        "BOT     | %-9s | %d atomic route checks across both venue orders",
        "LIVE" if args.live else "DRY RUN",
        len(routes),
    )
    logger.info(
        "RULE    | Ethereum | guaranteed net >= %s per completed cycle (strict floor)",
        amount_text(route_execution_floor(Route("ethereum", "USDC/USDG"), args.threshold_usd)),
    )
    logger.info(
        "RULE    | Solana   | guaranteed net >= %s per completed cycle (strict floor)",
        amount_text(route_execution_floor(Route("solana", "USDC/USDG"), args.threshold_usd)),
    )
    eth_rpc_url = (
        os.getenv("ETH_RPC_URL")
        or "https://ethereum-rpc.publicnode.com"
    )
    if args.eth_max_base_fee_gwei is not None:
        logger.info(
            "RULE    | Ethereum | pause trading if base fee > %.3f Gwei",
            args.eth_max_base_fee_gwei,
        )
    for route in routes:
        logger.info("ROUTE   | %-8s | %s", route.chain.title(), route.display)

    with single_instance():
        stop = threading.Event()
        dashboard = SniperDashboardFeed(
            DASHBOARD_PATH,
            routes,
            live=args.live,
            base_threshold=args.threshold_usd,
        )
        backoff = AdaptiveBackoff()
        cooldown_policy = CooldownPolicy(
            transient_base_seconds=args.transient_backoff_seconds,
            transient_max_seconds=args.max_transient_backoff_seconds,
            provider_access_seconds=args.provider_access_cooldown_seconds,
            no_route_seconds=args.no_route_cooldown_seconds,
            capacity_seconds=args.capacity_cooldown_seconds,
            unstable_capacity_seconds=args.unstable_capacity_cooldown_seconds,
            reverted_seconds=args.reverted_cooldown_seconds,
        )
        watcher = threading.Thread(
            target=watch_for_stop_request,
            name="sniper-stop-watcher",
            args=(stop, logger),
            daemon=True,
        )
        watcher.start()
        threads = []
        for chain in args.chains:
            chain_routes = [route for route in routes if route.chain == chain]
            thread = threading.Thread(
                target=worker,
                name=f"sniper-{chain}",
                args=(chain, chain_routes),
                kwargs={
                    "live": args.live,
                    "base_threshold": args.threshold_usd,
                    "interval_seconds": args.interval_seconds,
                    "cooldown_seconds": args.cooldown_seconds,
                    "timeout_seconds": args.route_timeout_seconds,
                    "cooldown_policy": cooldown_policy,
                    "backoff": backoff,
                    "once": args.once,
                    "stop": stop,
                    "logger": logger,
                    "dashboard": dashboard,
                    "eth_max_base_fee_gwei": args.eth_max_base_fee_gwei,
                    "eth_rpc_url": eth_rpc_url,
                },
            )
            thread.start()
            threads.append(thread)
        try:
            for thread in threads:
                thread.join()
        except KeyboardInterrupt:
            logger.info("SAFETY  | STOPPING  | keyboard shutdown requested")
            stop.set()
            for thread in threads:
                thread.join()
        stop.set()
        watcher.join(timeout=1)
        dashboard.stop()
    return 0
if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SniperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
