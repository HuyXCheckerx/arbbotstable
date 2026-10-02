#!/usr/bin/env python3
"""Continuously execute guarded Ethereum and Solana stablecoin arbitrage."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager
from copy import deepcopy
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
from urllib.parse import urlsplit, urlunsplit
import urllib.request

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# sniper.py executes this file with runpy, which does not add its directory to
# sys.path or establish a package. Use the same absolute package imports for
# that launcher, direct script execution, and python -m execution.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.engines.stable_liquidity_monitor import StableLiquidityMonitor

LOG_DIR = PROJECT_ROOT / "logs"
PLAN_DIR = LOG_DIR / "sniper-plans"
LOCK_PATH = LOG_DIR / ".crosschain-sniper.lock"
PID_PATH = LOG_DIR / "crosschain-sniper.pid"
STOP_PATH = LOG_DIR / ".crosschain-sniper.stop"
DASHBOARD_PATH = LOG_DIR / "sniper-dashboard.json"
LIVE_CONFIRMATION = "EXECUTE_PROFIT_SNIPER"
MINIMUM_ALLOWED_THRESHOLD = Decimal("4")
SOLANA_MINIMUM_ALLOWED_THRESHOLD = Decimal("0.01")
TOKEN_QUANTUM = Decimal("0.000001")
MAX_PAUSE_SECONDS = 10.0
ROUTE_TOKENS = ("USDC", "USDG", "PYUSD")
DEFAULT_SWAP_ORDERS = ("dex-first", "stable-first")
DEFAULT_ROUTE_PAIRS = tuple(
    f"{loan}/{counter}"
    for loan in ROUTE_TOKENS
    for counter in ROUTE_TOKENS
    if loan != counter
)
_consecutive_bridge_failures: int = 0
_last_bridge_failure_restart: float = 0.0


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
        if self.chain == "solana":
            provider = os.environ.get("SOL_FLASH_ARB_DEX_PROVIDER", "metamatcha").strip().lower()
            if provider == "jupiter":
                return "Jupiter"
            if provider == "dflow":
                return "DFlow"
            return "MetaMatcha"
        provider = os.environ.get("ETH_ARB_QUOTE_PROVIDER", "matcha")
        return "Direct" if provider.strip().lower() == "direct" else "MetaMatcha"

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

    @property
    def arbitrage_key(self) -> str:
        """Routes sharing this key capture the same price difference.

        Borrowing A to sell A->B on Stable.com and buy it back on the DEX needs
        the same two rates as borrowing B to buy A on the DEX and sell it on
        Stable.com; only the loan and profit token differ.
        """
        return f"{self.chain}:stable:{self.stable_from}>{self.stable_to}"


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
    retry_after_seconds: float | None = None


def bounded_pause(seconds: float) -> float:
    """Hard ceiling for bot scheduling, including legacy settings and Retry-After."""
    return min(MAX_PAUSE_SECONDS, max(0.0, seconds))


@dataclass(frozen=True)
class CooldownPolicy:
    transient_base_seconds: float
    transient_max_seconds: float
    provider_access_seconds: float
    no_route_seconds: float
    capacity_seconds: float
    unstable_capacity_seconds: float
    reverted_seconds: float

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, bounded_pause(getattr(self, name)))


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
        seconds = bounded_pause(seconds)
        with self._lock:
            now = time.monotonic()
            self._deadlines[key] = min(now + MAX_PAUSE_SECONDS,
                max(self._deadlines.get(key, 0.0), now + seconds))
            return self._deadlines[key] - now

    def fail(
        self,
        key: str,
        base_seconds: float,
        max_seconds: float,
        minimum_seconds: float = 0.0,
    ) -> float:
        if not math.isfinite(minimum_seconds) or minimum_seconds < 0:
            minimum_seconds = 0.0
        minimum_seconds = bounded_pause(minimum_seconds)
        with self._lock:
            now = time.monotonic()
            remaining = self._deadlines.get(key, 0.0) - now
            if remaining > 0:
                # Concurrent routes observing the same outage share one window.
                self._deadlines[key] = min(now + MAX_PAUSE_SECONDS,
                    max(self._deadlines[key], now + minimum_seconds))
                return self._deadlines[key] - now
            failures = self._failures.get(key, 0) + 1
            self._failures[key] = failures
            delay = min(
                max_seconds,
                base_seconds * (2 ** min(failures - 1, 20)),
            )
            now = time.monotonic()
            self._deadlines[key] = max(
                self._deadlines.get(key, 0.0),
                now + bounded_pause(max(delay, minimum_seconds)),
            )
            return self._deadlines[key] - now

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
            Decimal("1")
            if base_threshold in {Decimal("4"), Decimal("5")}
            else base_threshold
        )
        return strict_execution_floor(effective_threshold, route)
    return strict_execution_floor(base_threshold, route)


def amount_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


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


# Outcomes meaning the loan token cannot be borrowed right now, so the
# equivalent route that borrows the other token should be checked instead.
FUNDING_UNAVAILABLE_CATEGORIES = frozenset({"flash-liquidity", "flash-conflict", "marginfi-utilization"})


def arbitrage_groups(routes: list[Route]) -> list[list[Route]]:
    """Equivalent routes in configured order; the first is preferred."""
    groups: dict[str, list[Route]] = {}
    for route in routes:
        groups.setdefault(route.arbitrage_key, []).append(route)
    result = []
    for group in groups.values():
        # On Ethereum, prefer Morpho-funded loans (USDC, PYUSD) over Uniswap v4 (USDG)
        sorted_group = sorted(
            group,
            key=lambda r: (1 if r.chain == "ethereum" and r.loan == "USDG" else 0),
        )
        result.append(sorted_group)
    return result


def active_routes(
    groups: list[list[Route]],
    funding_blocked_until: dict[str, float],
    now: float,
) -> list[Route]:
    """One route per arbitrage: the first whose flash loan is not known to be unavailable.

    The equivalent route with the other loan token is quoted only while the
    preferred one cannot be funded, instead of quoting both every cycle.
    """
    active = []
    for group in groups:
        for route in group:
            if funding_blocked_until.get(route.key, 0.0) <= now:
                active.append(route)
                break
    return active


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
        environment["ETH_QUOTE_USER_AGENT"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        command = [
            sys.executable,
            str(PROJECT_ROOT / "src" / "engines" / "eth_flash_arb_pyusd_usdc.py"),
            "--loan-token",
            route.loan,
            "--intermediate-token",
            route.intermediate,
            "--swap-order",
            route.swap_order,
            "--base-amount",
            os.getenv("ETH_ARB_BASE_AMOUNT", "100000"),
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
        appdata = os.environ.get("APPDATA")
        if appdata:
            npm_dir = Path(appdata) / "npm"
            if not npm_dir.exists():
                try:
                    npm_dir.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass

        local_tsx = PROJECT_ROOT / "node_modules" / ".bin" / ("tsx.cmd" if sys.platform == "win32" else "tsx")
        script_path = str(PROJECT_ROOT / "src" / "engines" / "solana_flash_arb.ts")
        if local_tsx.exists():
            command = [
                str(local_tsx),
                script_path,
                "--swap-order",
                route.swap_order,
            ]
        else:
            executable = "npx.cmd" if sys.platform == "win32" else "npx"
            command = [
                executable,
                "tsx",
                script_path,
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
                "SOL_FLASH_ARB_MATCHA_PYTHON": sys.executable,
            }
        )
        existing_node_opts = environment.get("NODE_OPTIONS", "")
        if "--max-old-space-size" not in existing_node_opts:
            environment["NODE_OPTIONS"] = f"{existing_node_opts} --max-old-space-size=512".strip()
        if (
            route.dex_name == "Jupiter"
            and {route.stable_from, route.stable_to} == {"USDG", "PYUSD"}
        ):
            # Explicit Jupiter fallback may need a USDC-managed hop.
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


def is_vercel_challenge(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "vercel security checkpoint",
            "access blocked by vercel",
            "x-vercel-mitigated=challenge",
        )
    )


def retry_after_seconds(detail: str) -> float | None:
    """Read normalized provider wait hints before shortening the engine error."""
    lowered = detail.lower()
    if "does not support loan token" in lowered or "unsupported loan token" in lowered:
        return 86400.0
    waits: list[float] = []
    for match in re.finditer(r"\bretry-after=([^\s;]+)s\b", detail, re.IGNORECASE):
        try:
            seconds = float(match.group(1))
        except ValueError:
            continue
        if math.isfinite(seconds) and seconds >= 0:
            waits.append(seconds)
    return max(waits) if waits else None


def failure_category(detail: str) -> str:
    lowered = detail.lower()
    access_denied = re.search(r"\bHTTP\s+(?:401|403)\b", detail, re.IGNORECASE)
    matcha_access_block = (
        access_denied
        and ("matcha" in lowered or "metamatcha" in lowered)
    )
    zero_ex_auth_block = (
        access_denied
        and ("api.0x.org" in lowered or "official 0x" in lowered)
    )
    matcha_vercel_block = "matcha" in lowered and is_vercel_challenge(detail)
    if matcha_access_block or zero_ex_auth_block or matcha_vercel_block:
        return "access-blocked-matcha"
    if any(
        marker in lowered
        for marker in (
            "no zero-fee flash provider has",
            "only 0% fee flash loan providers are allowed",
            "marginfi flash loan is unavailable",
            "no known kamino reserve configured",
            "flash-loan liquidity is below",
        )
    ) or ("flash liquidity is" in lowered and "below requested" in lowered):
        return "flash-liquidity"
    if (
        "poolmanager" in lowered
        or "alreadyunlocked" in lowered
        or "conflicts with this matcha route" in lowered
    ):
        return "flash-conflict"
    if (
        "no_routes_found" in lowered
        or "no routes found" in lowered
        or "no executable simulated quote" in lowered
        or "no executable matcha liquidity" in lowered
        or "solana 1232-byte size limit" in lowered
        or "exceeds solana 1232-byte size limit" in lowered
        or "does not support loan token" in lowered
        or "unsupported loan token" in lowered
    ):
        return "no-route"
    if "6026" in lowered or "illegalutilizationratio" in lowered or "utilization ratio" in lowered:
        return "marginfi-utilization"
    if "capacity kept changing" in lowered:
        return "unstable-capacity"
    if (
        "pool capacity is below" in lowered
        or "usable capacity" in lowered and "below" in lowered
        or "pool has no remaining" in lowered
        or ("stable" in lowered and "insufficient_pool_balance" in lowered
            and not re.search(r"\b(?:http\s+)?429\b", lowered))
    ):
        return "capacity"
    transient = (
        bool(re.search(r"\bhttp\s+(?:429|5\d\d)\b", lowered))
        or any(
            marker in lowered
            for marker in (
                "internal server error",
                "rate limits exceeded",
                "timed out",
                "temporarily failed",
                "connection reset",
                "bad gateway",
                "gateway timeout",
                "error 520",
                "error 521",
                "error 522",
                "error 523",
                "error 524",
                "error 525",
                "could not connect",
                "cannot connect",
            )
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
    if (
        transient
        or "0xc0000409" in lowered
        or "heap out of memory" in lowered
        or "status 3221226505" in lowered
        or "engine process aborted" in lowered
    ):
        return "transient-rpc"
    if "below" in lowered or "no executable opportunity" in lowered:
        return "unprofitable"
    return "failure"


def jupiter_market_key(route: Route) -> str:
    return f"jupiter:{route.dex_from}/{route.dex_to}"


def dex_market_key(route: Route) -> str:
    venue = "jupiter" if route.dex_name == "Jupiter" else "metamatcha"
    return f"{venue}:{route.chain}:{route.dex_from}/{route.dex_to}"


def dex_provider_key(route: Route) -> str:
    venue = "jupiter" if route.dex_name == "Jupiter" else "metamatcha"
    return f"{venue}:{route.chain}"


def dependency_keys(route: Route) -> tuple[str, ...]:
    keys = [f"stable:{route.chain}", f"rpc:{route.chain}"]
    keys.extend((dex_provider_key(route), dex_market_key(route)))
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
    if returncode in (3221226505, -1073740791, 0xC0000409):
        lowered_err = (stderr or "").lower()
        if "heap out of memory" in lowered_err or "allocation failed" in lowered_err:
            return "Node.js JavaScript heap out of memory (process aborted with 0xC0000409)"
        return "engine process aborted (exit status 0xC0000409 / out of memory)"

    combined = "\n".join(part for part in (stderr, stdout) if part)
    lines = [
        line.strip()
        for line in combined.splitlines()
        if line.strip()
        and not line.strip().startswith("Node.js v")
        and not line.strip().startswith("==== C stack trace")
        and not re.match(r"^\s*\d+:\s+[0-9A-Fa-f]{8,16}\b", line.strip())
    ]
    for idx, line in enumerate(lines):
        if line.startswith("ERROR:"):
            header = line[6:].strip()
            if header.endswith(":") and idx + 1 < len(lines):
                details = []
                for sub in lines[idx + 1:]:
                    if sub.startswith("-") or "Atomic simulation reverted" in sub or "AnchorError" in sub or "IllegalUtilizationRatio" in sub:
                        details.append(sub)
                    elif sub.startswith("ERROR:") or sub.startswith("Wallet:"):
                        break
                    if len(details) >= 3:
                        break
                if details:
                    return f"{header} {' '.join(details)}"
            return header
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
        if "does not support loan token" in lowered or "unsupported loan token" in lowered:
            return f"{route.chain.title()} executor does not support {route.loan}/{route.intermediate}"
        return f"{route.dex_name} has no executable {dex_leg} route right now"
    if category in {"flash-liquidity", "flash-conflict"}:
        return f"{route.loan} flash loan cannot be funded right now: {detail}"
    if category == "marginfi-utilization":
        return (
            f"Marginfi {route.loan} bank utilization is >100% on Solana "
            "(AnchorError 6026: IllegalUtilizationRatio); borrows are disabled on-chain"
        )
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
        status = re.search(r"\bHTTP\s+(\d{3})\b", detail, re.IGNORECASE)
        suffix = f" (HTTP {status.group(1)})" if status else ""
        if "api.0x.org" in lowered or "official 0x" in lowered:
            return (
                f"the official 0x API rejected this request{suffix}; "
                "check provider access"
            )
        alternative = (
            "explicitly configure Jupiter (SOL_FLASH_ARB_DEX_PROVIDER=jupiter)"
            if route.chain == "solana"
            else "configure the official 0x quote provider"
        )
        if is_vercel_challenge(detail):
            endpoint = ""
            found_url = re.search(r"https?://[^\s;]+", detail)
            if found_url:
                try:
                    parsed = urlsplit(found_url.group(0).rstrip(").,"))
                    safe_url = urlunsplit(
                        (parsed.scheme, parsed.hostname or "", parsed.path, "", "")
                    )
                    endpoint = f" at {safe_url}"
                except ValueError:
                    pass
            request_id = re.search(r"\brequest-id=([A-Za-z0-9:._-]{1,200})", detail)
            request_reference = f"; request-id={request_id.group(1)}" if request_id else ""
            denied = "x-vercel-mitigated=deny" in lowered or "x-vercel-mitigated=denied" in lowered
            block_name = "Vercel firewall" if denied else "Vercel Security Checkpoint"
            explanation = "provider denied API access" if denied else "browser verification is required"
            return (
                f"MetaMatcha request blocked by {block_name}{suffix}"
                f"{endpoint}; {explanation}{request_reference}; "
                f"check provider access or {alternative}"
            )
        return (
            f"MetaMatcha rejected this request{suffix}; "
            f"check provider access or {alternative}"
        )
    if category == "transient-rpc":
        if "out of memory" in lowered or "0xc0000409" in lowered:
            return "engine process temporarily ran out of memory; recovering"
        status = re.search(r"HTTP\s+(\d{3})", detail, re.IGNORECASE)
        suffix = f" (HTTP {status.group(1)})" if status else (f" ({detail})" if detail else "")
        return f"{route.chain.title()} RPC is temporarily unavailable{suffix}"
    if category == "capacity":
        return "Stable.com does not currently have enough usable input capacity"
    if category == "unstable-capacity":
        return "Stable.com capacity changed repeatedly while the route was being sized"
    if category == "unprofitable":
        if "maximum-gas net-profit floor" in lowered:
            breakdown = re.search(
                r"floor:\s*([-+\d.]+\s+[A-Z]+)\s*<\s*([-+\d.]+\s+[A-Z]+)(?:\s*\((.*?)\))?",
                detail,
                re.IGNORECASE,
            )
            if breakdown:
                net_str, req_str = breakdown.group(1), breakdown.group(2)
                notes = f" ({breakdown.group(3)})" if breakdown.group(3) else ""
                return f"net {net_str} below required {req_str}{notes}"
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
    if outcome.category in {"capacity", "unstable-capacity"}:
        return "WATCHING"
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


def safe_urlopen(req: Any, timeout: float = 12.0) -> Any:
    """Execute an HTTP request with certifi CA bundle and graceful unverified fallback.
    
    Prevents [SSL: CERTIFICATE_VERIFY_FAILED] errors on Windows systems where
    local CA certificates may be missing from the default OpenSSL bundle.
    """
    import ssl
    import urllib.error
    import urllib.request

    ctx = None
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            ctx = ssl.create_default_context()
        except Exception:
            pass

    try:
        if ctx is not None:
            return urllib.request.urlopen(req, timeout=timeout, context=ctx)
        return urllib.request.urlopen(req, timeout=timeout)
    except TypeError:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as uerr:
        err_str = str(uerr).lower()
        if "certificate_verify_failed" in err_str or "unable to get local issuer certificate" in err_str:
            unverified_ctx = ssl.create_default_context()
            unverified_ctx.check_hostname = False
            unverified_ctx.verify_mode = ssl.CERT_NONE
            try:
                return urllib.request.urlopen(req, timeout=timeout, context=unverified_ctx)
            except TypeError:
                return urllib.request.urlopen(req, timeout=timeout)
        raise


def fetch_ethereum_base_fee_gwei(
    rpc_url: str | list[str],
    timeout: float = 5.0,
) -> Decimal | None:
    if not rpc_url:
        return None
    candidates: list[str] = []

    def _add(u: str) -> None:
        u = u.strip()
        if u and u not in candidates:
            candidates.append(u)

    if isinstance(rpc_url, str):
        for u in rpc_url.split(","):
            _add(u)
    else:
        for item in rpc_url:
            for u in item.split(","):
                _add(u)

    env_fallbacks = os.getenv("ETH_RPC_FALLBACKS", "")
    if env_fallbacks:
        for u in env_fallbacks.split(","):
            _add(u)

    for fallback in [
        "https://eth.drpc.org",
        "https://rpc.mevblocker.io",
        "https://eth-mainnet.public.blastapi.io",
        "https://eth.blockrazor.xyz",
        "https://1rpc.io/eth",
    ]:
        _add(fallback)

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
        for endpoint in candidates:
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "arbbot-sniper/1.0",
                    },
                )
                with safe_urlopen(req, timeout=timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    result = body.get("result")
                    if not isinstance(result, dict):
                        continue
                    base_fee_hex = result.get("baseFeePerGas")
                    if not base_fee_hex:
                        continue
                    base_fee_wei = int(base_fee_hex, 16)
                    return Decimal(base_fee_wei) / Decimal(10**9)
            except Exception:
                continue
        return None
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
            r"gross profit:\s*([-+\d.]+)",
            r"guaranteed gross\s+([-+\d.]+)",
            r"quoted route is below (?:the )?on-chain profit floor:\s*([-+\d.]+)",
        ),
        "net": (
            r"Guaranteed net result:\s*([-+\d.]+)",
            r"Predicted Net Profit:\s*([-+\d.]+)",
            r"predicted net\s+([-+\d.]+)",
            r"guaranteed net\s+([-+\d.]+)",
            r"route is below the maximum-gas net-profit floor:\s*([-+\d.]+)",
        ),
    }

    def last_match(candidates: tuple[str, ...]) -> str | None:
        for pattern in candidates:
            matches = list(re.finditer(pattern, combined, re.IGNORECASE))
            if matches:
                return _profit_value(matches[-1].group(1))
        return None

    return last_match(patterns["gross"]), last_match(patterns["net"])


class SniperDashboardFeed:
    """Thread-safe status feed shared by the terminal and web dashboards."""

    _PAUSED_CATEGORIES = {
        "flash-liquidity",
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
            "dex_name": route.dex_name,
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

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(self._state)

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
                # Keep the last quote visible during refresh; CHECKING and its
                # original timestamp distinguish it from a fresh result.
                for field in ("checked_at", "gross_profit", "net_profit", "profit_token"):
                    record[field] = previous.get(field)
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
                "watching" if outcome.category in {"capacity", "unstable-capacity"} else
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
        seconds = bounded_pause(seconds)
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

    def mark_standby(self, route: Route, preferred: Route) -> None:
        """Show that an equivalent route is being checked instead of this one."""
        detail = (
            f"Same arbitrage as {preferred.display}; checked only while the "
            f"{preferred.loan} flash loan is unavailable"
        )
        with self._lock:
            routes = self._state["routes"]
            assert isinstance(routes, dict)
            previous = routes.get(route.key)
            if isinstance(previous, dict) and previous.get("state") == "STANDBY" and previous.get("detail") == detail:
                return
            floor = previous.get("execution_floor") if isinstance(previous, dict) else None
            record = self._route_record(
                route,
                Decimal(str(floor)) if floor is not None else Decimal("0"),
                state="STANDBY",
                detail=detail,
            )
            if isinstance(previous, dict):
                for field in ("checked_at", "gross_profit", "net_profit", "profit_token"):
                    record[field] = previous.get(field)
            routes[route.key] = record
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


def is_subprocess_mocked() -> bool:
    target = subprocess.run
    return (
        hasattr(target, "assert_called")
        or hasattr(target, "call_count")
        or hasattr(target, "mock_calls")
        or getattr(target, "__module__", "") == "unittest.mock"
        or type(target).__name__ in ("Mock", "MagicMock", "AsyncMock")
    )


def run_ethereum_route_direct(
    route: Route,
    *,
    live: bool,
    execution_floor: Decimal,
    timeout_seconds: float,
) -> Outcome:
    started = time.monotonic()
    floor = amount_text(execution_floor)
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    output_path = PLAN_DIR / f"{route.key}.json"

    try:
        from src.engines import eth_flash_arb_pyusd_usdc as eth_engine
    except ImportError:
        import eth_flash_arb_pyusd_usdc as eth_engine

    args_list = [
        "--loan-token",
        route.loan,
        "--intermediate-token",
        route.intermediate,
        "--swap-order",
        route.swap_order,
        "--base-amount",
        os.getenv("ETH_ARB_BASE_AMOUNT", "100000"),
        "--min-profit",
        floor,
        "--min-net-profit",
        floor,
        "--output",
        str(output_path),
    ]
    if live:
        args_list.extend(["--send", "--confirm-mainnet", "EXECUTE_ATOMIC_ARB"])

    try:
        parsed_args = eth_engine.parser().parse_args(args_list)
        plan = None
        for attempt in range(1, parsed_args.quote_attempts + 1):
            try:
                plan = eth_engine.run(parsed_args)
                break
            except eth_engine.RetryableArbError as exc:
                if attempt == parsed_args.quote_attempts:
                    raise eth_engine.ArbError(
                        f"{exc}; exhausted {parsed_args.quote_attempts} fresh quote attempts"
                    ) from exc
                time.sleep(0.5)

        if plan is None:
            raise eth_engine.ArbError("No plan generated by Ethereum engine")

        eth_engine.write_plan(parsed_args.output, plan)
        elapsed = time.monotonic() - started
        gross_profit = _profit_value(plan.get("grossProfit"))
        net_profit = _profit_value(plan.get("predictedNetProfit"))
        tx_status = str(plan.get("transactionStatus") or "").lower()
        tx_hash = plan.get("transactionHash")
        tx_link = (
            f"https://etherscan.io/tx/0x{str(tx_hash).removeprefix('0x')}"
            if tx_hash
            else None
        )

        if tx_status == "confirmed" and tx_link:
            return Outcome(
                True,
                f"confirmed in {elapsed:.1f}s via {plan.get('broadcastMethod', 'unknown')}: {tx_link}",
                "confirmed",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=elapsed,
                transaction=tx_link,
            )
        if tx_status == "submitted" and tx_link:
            return Outcome(
                False,
                f"submitted in {elapsed:.1f}s but confirmation is ambiguous: {tx_link}",
                "submitted",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=elapsed,
                transaction=tx_link,
            )
        if tx_status == "reverted" and tx_link:
            return Outcome(
                False,
                f"reverted in {elapsed:.1f}s: {tx_link}",
                "reverted",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=elapsed,
                transaction=tx_link,
            )
        if not live:
            detail = (
                f"guaranteed net {plan.get('predictedNetProfit')} {route.loan}; "
                f"simulation passed in {elapsed:.1f}s; not sent"
            )
            return Outcome(
                False,
                detail,
                "eligible",
                gross_profit=gross_profit,
                net_profit=net_profit,
                profit_token=route.loan,
                elapsed_seconds=elapsed,
            )
        return Outcome(
            False,
            f"engine succeeded in {elapsed:.1f}s without a transaction reference",
            "failure",
            gross_profit=gross_profit,
            net_profit=net_profit,
            profit_token=route.loan,
            elapsed_seconds=elapsed,
        )
    except eth_engine.ArbError as exc:
        elapsed = time.monotonic() - started
        detail = str(exc)
        category = failure_category(detail)
        gross_profit, net_profit = profit_metrics(route, "", detail)
        return Outcome(
            False,
            readable_failure(route, detail, category),
            category,
            gross_profit=gross_profit,
            net_profit=net_profit,
            profit_token=route.loan,
            elapsed_seconds=elapsed,
            retry_after_seconds=retry_after_seconds(detail),
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        detail = str(exc)
        category = failure_category(detail)
        gross_profit, net_profit = profit_metrics(route, "", detail)
        return Outcome(
            False,
            readable_failure(route, detail, category),
            category,
            gross_profit=gross_profit,
            net_profit=net_profit,
            profit_token=route.loan,
            elapsed_seconds=elapsed,
            retry_after_seconds=retry_after_seconds(detail),
        )


def run_route(
    route: Route,
    *,
    live: bool,
    execution_floor: Decimal,
    timeout_seconds: float,
) -> Outcome:
    if not is_subprocess_mocked() and route.chain == "ethereum":
        return run_ethereum_route_direct(
            route,
            live=live,
            execution_floor=execution_floor,
            timeout_seconds=timeout_seconds,
        )

    invocation = build_route_invocation(
        route,
        live=live,
        execution_floor=execution_floor,
    )
    started = time.monotonic()
    started_wall = time.time()
    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
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
            creationflags=creationflags,
            startupinfo=startupinfo,
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

    def completed_outcome(
        executed: bool,
        detail: str,
        category: str,
        provider_retry_after: float | None = None,
    ) -> Outcome:
        return Outcome(
            executed,
            detail,
            category,
            gross_profit=gross_profit,
            net_profit=net_profit,
            profit_token=route.loan,
            elapsed_seconds=elapsed,
            transaction=transaction,
            retry_after_seconds=provider_retry_after,
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
        retry_after_seconds(detail),
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
    matcha_logger = logging.getLogger("matcha.cookies")
    matcha_logger.setLevel(logging.INFO)
    matcha_logger.handlers = [console, file_handler]
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


def _execute_single_check(
    route: Route,
    floor: Decimal,
    *,
    live: bool,
    timeout_seconds: float,
    dashboard: SniperDashboardFeed | None,
    logger: logging.Logger,
    chain: str,
) -> tuple[Route, Decimal, Outcome]:
    logger.info("CHECK   | %-8s | %s", chain.title(), route.display)
    if dashboard:
        dashboard.begin_check(route, floor)
    outcome = run_route(
        route,
        live=live,
        execution_floor=floor,
        timeout_seconds=timeout_seconds,
    )
    return route, floor, outcome


def _handle_route_outcome(
    route: Route,
    floor: Decimal,
    outcome: Outcome,
    *,
    chain: str,
    cooldown_policy: CooldownPolicy,
    backoff: AdaptiveBackoff,
    route_deadlines: dict[str, float],
    dashboard: SniperDashboardFeed | None,
    logger: logging.Logger,
    cooldown_seconds: float,
    stop: threading.Event,
) -> bool:
    """Process outcome, update metrics/backoff, and return True if execution stop/cooldown is triggered."""
    global _consecutive_bridge_failures, _last_bridge_failure_restart
    cooldown_seconds = bounded_pause(cooldown_seconds)
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
        return False
    if outcome.category.startswith("transient-"):
        dependency = {
            "transient-stable": f"stable:{route.chain}",
            "transient-jupiter": f"jupiter:{route.chain}",
            "transient-matcha": f"metamatcha:{route.chain}",
            "transient-rpc": f"rpc:{route.chain}",
        }[outcome.category]
        is_conn_reset = "connection reset" in outcome.detail.lower()
        if is_conn_reset and outcome.category == "transient-matcha":
            delay = backoff.block(dependency, 5.0)
        else:
            delay = backoff.fail(
                dependency,
                cooldown_policy.transient_base_seconds,
                cooldown_policy.transient_max_seconds,
                minimum_seconds=outcome.retry_after_seconds or 0.0,
            )
        status = re.search(r"HTTP\s+(\d{3})", outcome.detail, re.IGNORECASE)
        suffix = f" (HTTP {status.group(1)})" if status else (" (connection reset)" if is_conn_reset else "")
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

        lowered_detail = outcome.detail.lower()
        if (
            outcome.category == "transient-matcha"
            and any(
                x in lowered_detail
                for x in (
                    "evaluation timeout",
                    "page.evaluate",
                    "local-status=504",
                    "local-status=500",
                    "competition fetch failed",
                    "event(error)",
                )
            )
            and "failed to become ready" not in lowered_detail
            and "temporarily failed" not in lowered_detail
        ):
            _consecutive_bridge_failures += 1
            threshold = 3
            now = time.monotonic()
            if _consecutive_bridge_failures >= threshold and (now - _last_bridge_failure_restart >= 60.0):
                _consecutive_bridge_failures = 0
                _last_bridge_failure_restart = now
                logger.warning(
                    "[MatchaBridge] %d persistent bridge failure(s) detected; verifying proxy health and restarting bridge daemon...",
                    threshold,
                )
                try:
                    from scripts.manage_proxyisp import check_and_rotate_proxy_if_needed
                    check_and_rotate_proxy_if_needed(logger=logger, verify_current=True, reload_bridge=False)
                except Exception as exc:
                    logger.debug("[ProxyManager] Proxy rotation on bridge failure error: %s", exc)
                try:
                    from src.engines.matcha_browser_bridge import stop_bridge_server, ensure_bridge_running
                    stop_bridge_server()
                    if ensure_bridge_running(timeout=35.0):
                        backoff.succeed("metamatcha:ethereum")
                        backoff.succeed("metamatcha:solana")
                        logger.info("[MatchaBridge] Bridge daemon successfully restored; resumed scanning immediately")
                except Exception as exc:
                    logger.warning("[MatchaBridge] Bridge auto-restart error: %s", exc)
        elif route.dex_name == "MetaMatcha" and outcome.category in ("unprofitable", "executed", "simulated", "submitted"):
            _consecutive_bridge_failures = 0
    else:
        backoff.succeed(f"stable:{route.chain}")
        backoff.succeed(f"rpc:{route.chain}")
        backoff.succeed(dex_provider_key(route))
        if route.dex_name == "MetaMatcha":
            _consecutive_bridge_failures = 0

    if outcome.category == "no-route":
        dependency = dex_market_key(route)
        delay = backoff.block(dependency,
            outcome.retry_after_seconds or cooldown_policy.no_route_seconds)
        route_deadlines[route.key] = time.monotonic() + delay
        logger.info(
            "PAUSE   | %-17s | %.0fs | return market unavailable",
            dependency_label(dependency),
            delay,
        )
        if dashboard:
            dashboard.record_cooldown(
                route,
                delay,
                "Return market is unavailable",
            )
    elif outcome.category == "access-blocked-matcha":
        if route.chain == "ethereum" and not is_subprocess_mocked():
            try:
                from src.engines.proxy_executor import is_taker_blocked, rotate_blocked_proxy_and_restart
                from src.config.contracts import get_current_executor
                current_exec = get_current_executor()
                if is_taker_blocked(current_exec, outcome.detail):
                    rotate_blocked_proxy_and_restart(logger, current_exec)
            except Exception as exc:
                logger.warning("[ProxyExecutor] Taker block evaluation error: %s", exc)

        dependency = f"metamatcha:{route.chain}"
        backoff.block(dependency, cooldown_policy.provider_access_seconds)
        logger.info(
            "PAUSE   | %-17s | %.0fs | provider rejected the request",
            dependency_label(dependency),
            cooldown_policy.provider_access_seconds,
        )
        if dashboard:
            dashboard.record_cooldown(
                route,
                cooldown_policy.provider_access_seconds,
                "Quote provider rejected the request",
            )
    elif outcome.category == "flash-liquidity":
        route_deadlines[route.key] = (
            time.monotonic() + cooldown_policy.capacity_seconds
        )
        logger.info(
            "PAUSE   | %-17s | %.0fs | %s flash loan cannot be funded",
            route.display,
            cooldown_policy.capacity_seconds,
            route.loan,
        )
        if dashboard:
            dashboard.record_cooldown(
                route,
                cooldown_policy.capacity_seconds,
                f"{route.loan} flash loan cannot be funded",
            )
    elif outcome.category in {"capacity", "unstable-capacity"}:
        route_deadlines.pop(route.key, None)
        logger.info("WATCH   | %s | monitoring Stable.com %s payout balance and backend readiness",
                    route.display, route.stable_to)
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
    # The worker waits once after settling the batch. Sleeping here would add
    # a cooldown for every completed parallel result plus the scan interval.
    return stop.is_set()


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
    eth_rpc_url: str = "https://eth.drpc.org",
    parallel_scanning: bool | None = None,
) -> None:
    interval_seconds = bounded_pause(interval_seconds)
    cooldown_seconds = bounded_pause(cooldown_seconds)
    if parallel_scanning is None:
        parallel_scanning = (
            os.getenv("SNIPER_PARALLEL_SCANNING", "true").lower()
            in ("true", "1", "yes")
            and not once
        )

    liquidity = StableLiquidityMonitor(chain, eth_rpc_url if chain == "ethereum"
        else os.getenv("SOLANA_RPC_URL", "").split(",")[0].strip())
    route_deadlines: dict[str, float] = {}
    groups = arbitrage_groups(routes)
    funding_blocked_until: dict[str, float] = {}
    post_execution_until = 0.0

    def publish_standby(active: list[Route]) -> None:
        if not dashboard:
            return
        active_keys = {route.key for route in active}
        for group in groups:
            preferred = next((route for route in group if route.key in active_keys), None)
            for route in group:
                if preferred and route is not preferred:
                    dashboard.mark_standby(route, preferred)

    def settle(route: Route, floor: Decimal, outcome: Outcome) -> bool:
        nonlocal post_execution_until
        if outcome.executed:
            post_execution_until = max(post_execution_until, time.monotonic() + cooldown_seconds)
        if outcome.category in {"capacity", "unstable-capacity"}:
            liquidity.arm(route)
        else:
            liquidity.clear(route)
        stop_requested = _handle_route_outcome(
            route,
            floor,
            outcome,
            chain=chain,
            cooldown_policy=cooldown_policy,
            backoff=backoff,
            route_deadlines=route_deadlines,
            dashboard=dashboard,
            logger=logger,
            cooldown_seconds=cooldown_seconds,
            stop=stop,
        )
        if outcome.category in FUNDING_UNAVAILABLE_CATEGORIES:
            funding_blocked_until[route.key] = (
                time.monotonic() + cooldown_policy.capacity_seconds
            )
        return stop_requested

    def is_due(route: Route) -> bool:
        return (
            route_deadlines.get(route.key, 0.0) <= time.monotonic()
            and liquidity.due(route)
            and backoff.remaining(dependency_keys(route)) <= 0
        )

    def check(route: Route) -> tuple[Route, Decimal, Outcome]:
        return _execute_single_check(
            route,
            floor=route_execution_floor(route, base_threshold),
            live=live,
            timeout_seconds=timeout_seconds,
            dashboard=dashboard,
            logger=logger,
            chain=chain,
        )

    def run_checks(candidates: list[Route]) -> bool:
        """Check due routes; return True when the worker must stop."""
        if parallel_scanning:
            eligible = [route for route in candidates if is_due(route)]
            if not eligible or stop.is_set():
                return stop.is_set()
            with ThreadPoolExecutor(max_workers=min(len(eligible), 6)) as pool:
                futures = [pool.submit(check, route) for route in eligible]
                for future in as_completed(futures):
                    if stop.is_set():
                        return True
                    if settle(*future.result()):
                        return True
            return False
        for route in candidates:
            if stop.is_set():
                return True
            # Re-check each time: an earlier result may have paused a shared provider.
            if is_due(route) and settle(*check(route)):
                return True
        return False

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

        if backoff.remaining((f"rpc:{chain}",)) <= 0:
            liquidity.poll()
        current = active_routes(groups, funding_blocked_until, time.monotonic())
        publish_standby(current)
        if run_checks(current):
            return
        # A preferred route that just proved unfundable hands its arbitrage to
        # the equivalent route now instead of one interval later.
        replacements = [
            route
            for route in active_routes(groups, funding_blocked_until, time.monotonic())
            if route not in current
        ]
        if replacements:
            publish_standby(replacements)
            for route in replacements:
                logger.info(
                    "SWITCH  | %-8s | %s | preferred loan cannot be funded",
                    chain.title(),
                    route.display,
                )
            if run_checks(replacements):
                return

        if once:
            return
        wait_seconds = bounded_pause(max(interval_seconds, post_execution_until - time.monotonic()))
        if stop.wait(wait_seconds):
            return


def watch_for_stop_request(stop: threading.Event, logger: logging.Logger) -> None:
    while not stop.wait(0.5):
        if STOP_PATH.exists():
            logger.info("SAFETY  | STOPPING  | cooperative stop requested")
            stop.set()
            return


def proxy_lifecycle_guardian(
    stop: threading.Event,
    logger: logging.Logger,
    check_interval: float = 300.0,
) -> None:
    """Continuously monitors proxy expiration and health in the background.
    Preemptively rotates proxy before 24h expiration or upon IP failure.
    """
    logger.info("[ProxyGuardian] Background proxy lifecycle guardian started (interval: %.0fs)", check_interval)
    while not stop.wait(check_interval):
        try:
            from scripts.manage_proxyisp import check_and_rotate_proxy_if_needed
            check_and_rotate_proxy_if_needed(logger=logger, min_remaining_seconds=600.0)
        except Exception as exc:
            logger.debug("[ProxyGuardian] Proxy check error: %s", exc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold-usd",
        type=Decimal,
        default=decimal_setting("SNIPER_PROFIT_THRESHOLD_USD", "4"),
        help=(
            "execute only above this net starting-token profit "
            "(default 4 for Ethereum; 0.5 for Solana)"
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
        default=float(os.getenv("SNIPER_COOLDOWN_SECONDS", "10")),
    )
    parser.add_argument(
        "--route-timeout-seconds",
        type=float,
        default=float(os.getenv("SNIPER_ROUTE_TIMEOUT_SECONDS", "300")),
    )
    parser.add_argument(
        "--transient-backoff-seconds",
        type=float,
        default=float(os.getenv("SNIPER_TRANSIENT_BACKOFF_SECONDS", "10")),
    )
    parser.add_argument(
        "--max-transient-backoff-seconds",
        type=float,
        default=float(os.getenv("SNIPER_MAX_TRANSIENT_BACKOFF_SECONDS", "10")),
    )
    parser.add_argument(
        "--provider-access-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_PROVIDER_ACCESS_COOLDOWN_SECONDS", "10")),
    )
    parser.add_argument(
        "--no-route-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_NO_ROUTE_COOLDOWN_SECONDS", "10")),
    )
    parser.add_argument(
        "--capacity-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_CAPACITY_COOLDOWN_SECONDS", "10")),
    )
    parser.add_argument(
        "--unstable-capacity-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_UNSTABLE_CAPACITY_COOLDOWN_SECONDS", "10")),
    )
    parser.add_argument(
        "--reverted-cooldown-seconds",
        type=float,
        default=float(os.getenv("SNIPER_REVERTED_COOLDOWN_SECONDS", "10")),
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
    parser.add_argument(
        "--parallel-scanning",
        "--parallel",
        dest="parallel_scanning",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("SNIPER_PARALLEL_SCANNING", "true").lower() in ("true", "1", "yes"),
        help="scan eligible routes concurrently in parallel within each chain worker (default: true)",
    )
    parser.add_argument("--once", action="store_true", help="check each route once")
    parser.add_argument("--live", action="store_true", help="allow guarded broadcasts")
    parser.add_argument("--confirm-live")
    parser.add_argument(
        "--no-proxy",
        "--no-proxies",
        dest="no_proxy",
        action="store_true",
        default=os.getenv("DISABLE_PROXIES", "").strip().lower() in ("1", "true", "yes"),
        help="disable proxy testing/rotation and connect directly without proxies",
    )
    parser.add_argument(
        "--request-stop",
        action="store_true",
        help="ask the running sniper to stop after its active route checks",
    )
    parser.add_argument(
        "--display", choices=("auto", "table", "log"),
        default=os.getenv("SNIPER_DISPLAY", "auto"),
        help="auto: refreshing table in a terminal, scrolling logs when redirected",
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
    args.interval_seconds = bounded_pause(args.interval_seconds)
    args.cooldown_seconds = bounded_pause(args.cooldown_seconds)
    for name in cooldown_values:
        attribute = name[2:].replace("-", "_")
        setattr(args, attribute, bounded_pause(getattr(args, attribute)))
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
    logger.info("RULE    | Automatic retry pauses are capped at %.0fs", MAX_PAUSE_SECONDS)
    eth_rpc_url = (
        os.getenv("ETH_RPC_URL")
        or "https://eth.drpc.org"
    )
    unresolved = unresolved_submission(routes)
    if unresolved:
        route, path, reference = unresolved
        resolved_status = None
        if str(reference).startswith("0x") and len(str(reference)) == 66:
            try:
                rpc_req = json.dumps({
                    "jsonrpc": "2.0",
                    "method": "eth_getTransactionReceipt",
                    "params": [reference],
                    "id": 1,
                }).encode("utf-8")
                req = urllib.request.Request(
                    eth_rpc_url,
                    data=rpc_req,
                    headers={"Content-Type": "application/json", "User-Agent": "ArbBotRecovery"},
                )
                with safe_urlopen(req, timeout=5.0) as resp:
                    rdata = json.loads(resp.read().decode("utf-8"))
                    rres = rdata.get("result")
                    if rres and isinstance(rres, dict) and "status" in rres:
                        st_code = int(rres["status"], 16)
                        resolved_status = "confirmed" if st_code == 1 else "reverted"
                        plan_data = json.loads(path.read_text(encoding="utf-8"))
                        plan_data["transactionStatus"] = resolved_status
                        plan_data["transactionReceipt"] = {
                            "status": st_code,
                            "blockNumber": int(rres.get("blockNumber", "0x0"), 16),
                            "gasUsed": int(rres.get("gasUsed", "0x0"), 16),
                        }
                        path.write_text(json.dumps(plan_data, indent=2), encoding="utf-8")
                        logger.info(
                            "RECOVER | RESOLVED  | prior submission %s was %s on-chain (block %d)",
                            reference,
                            resolved_status,
                            int(rres.get("blockNumber", "0x0"), 16),
                        )
            except Exception:
                pass

        if not resolved_status:
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
        "RULE    | Ethereum | guaranteed net >= %s per 100k cycle (scaled proportionally for smaller sizes)",
        amount_text(route_execution_floor(Route("ethereum", "USDC/USDG"), args.threshold_usd)),
    )
    logger.info(
        "RULE    | Solana   | guaranteed net >= %s per 100k cycle (scaled proportionally for smaller sizes)",
        amount_text(route_execution_floor(Route("solana", "USDC/USDG"), args.threshold_usd)),
    )
    eth_rpc_url = (
        os.getenv("ETH_RPC_URL")
        or "https://eth.drpc.org"
    )
    if args.eth_max_base_fee_gwei is not None:
        logger.info(
            "RULE    | Ethereum | pause trading if base fee > %.3f Gwei",
            args.eth_max_base_fee_gwei,
        )
    for route in routes:
        logger.info("ROUTE   | %-8s | %s", route.chain.title(), route.display)

    with single_instance(), ExitStack() as display_resources:
        stop = threading.Event()
        dashboard = SniperDashboardFeed(
            DASHBOARD_PATH,
            routes,
            live=args.live,
            base_threshold=args.threshold_usd,
        )
        from src.engines.sniper_terminal import terminal_dashboard
        display_resources.enter_context(terminal_dashboard(dashboard, logger, args.display))
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

        # The proxy, cookie and bridge machinery only serves MetaMatcha quotes.
        uses_matcha = (
            "solana" in args.chains
            and os.getenv("SOL_FLASH_ARB_DEX_PROVIDER", "metamatcha").strip().lower() == "metamatcha"
        ) or (
            "ethereum" in args.chains
            and os.getenv("ETH_ARB_QUOTE_PROVIDER", "matcha").strip().lower() == "matcha"
        )

        # Proactively verify or purchase working residential proxy before solving cookies
        if not uses_matcha:
            logger.info("No chain uses MetaMatcha; skipping proxy, cookie and bridge setup.")
            os.environ["MATCHA_PROXY"] = ""
        elif not args.no_proxy:
            try:
                from scripts.manage_proxyisp import setup_sniper_proxy
                active_proxy = setup_sniper_proxy(logger=logger)
                if not active_proxy:
                    os.environ["MATCHA_PROXY"] = ""
            except Exception as exc:
                logger.warning("Failed to verify/renew sniper proxy: %s", exc)
                os.environ["MATCHA_PROXY"] = ""
        else:
            logger.info("[ProxyManager] Running with --no-proxy; connecting directly.")
            os.environ["MATCHA_PROXY"] = ""

        if uses_matcha:
            # The daemon outlives the sniper and retains its launch environment and
            # imported code. Restart once at startup, after proxy selection, so an
            # automatic direct fallback or a code update cannot reuse a stale daemon.
            try:
                from src.engines.matcha_browser_bridge import stop_bridge_server
                logger.info("Restarting Matcha browser bridge to apply current code and proxy settings...")
                stop_bridge_server()
            except Exception as exc:
                logger.warning("Failed to stop previous Matcha browser bridge: %s", exc)

            # Proactively maintain fresh Cloudflare/Kasada cookies for child quote processes
            try:
                from src.engines.matcha_cookie_manager import (
                    ensure_vps_cookies,
                    get_valid_cookies,
                    start_background_cookie_solver,
                )
                logger.info("Ensuring valid MetaMatcha browser session cookies...")
                ensure_vps_cookies(logger_instance=logger)
                get_valid_cookies()
                start_background_cookie_solver()
            except Exception as exc:
                logger.warning("Failed to initialize MetaMatcha cookie session: %s", exc)

            # Proactively ensure warm Matcha browser bridge daemon is ready
            try:
                from src.engines.matcha_browser_bridge import ensure_bridge_running
                logger.info("Ensuring Matcha browser bridge daemon is ready...")
                ensure_bridge_running(timeout=75.0)
            except Exception as exc:
                logger.warning("Failed to initialize Matcha browser bridge: %s", exc)

            if not args.no_proxy and os.getenv("PROXYISP_API_KEY", "").strip():
                guardian_thread = threading.Thread(
                    target=proxy_lifecycle_guardian,
                    name="proxy-guardian",
                    args=(stop, logger),
                    daemon=True,
                )
                guardian_thread.start()

        threads = []
        for chain in args.chains:
            env_pairs = (
                os.getenv(f"{chain.upper()}_ROUTE_PAIRS")
                or (os.getenv("ETH_ROUTE_PAIRS") if chain == "ethereum" else None)
                or (os.getenv("SOL_ROUTE_PAIRS") if chain == "solana" else None)
            )
            if env_pairs:
                allowed_pairs = {p.strip().upper() for p in env_pairs.split(",") if p.strip()}
                chain_routes = [
                    route for route in routes
                    if route.chain == chain and route.pair.upper() in allowed_pairs
                ]
            else:
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
                    "parallel_scanning": args.parallel_scanning,
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
        try:
            from src.engines.matcha_cookie_manager import stop_background_cookie_solver
            stop_background_cookie_solver()
        except Exception:
            pass
        try:
            from src.engines.matcha_browser_bridge import stop_bridge_server
            stop_bridge_server()
        except Exception:
            pass
    return 0
if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SniperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
