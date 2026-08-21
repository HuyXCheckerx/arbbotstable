#!/usr/bin/env python3
"""Continuously execute guarded Ethereum and Solana stablecoin arbitrage."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import logging
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
LIVE_CONFIRMATION = "EXECUTE_PROFIT_SNIPER"
MINIMUM_ALLOWED_THRESHOLD = Decimal("5")
SOLANA_CROSS_TOKEN_MINIMUM_ALLOWED_THRESHOLD = Decimal("1")
TOKEN_QUANTUM = Decimal("0.000001")


class SniperError(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    chain: str
    pair: str

    @property
    def intermediate(self) -> str:
        return self.pair.split("/", 1)[0]

    @property
    def loan(self) -> str:
        return self.pair.split("/", 1)[1]

    @property
    def key(self) -> str:
        return f"{self.chain}-{self.intermediate.lower()}-{self.loan.lower()}"


@dataclass(frozen=True)
class Invocation:
    command: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True)
class Outcome:
    executed: bool
    detail: str


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
    if (
        route
        and route.chain == "solana"
        and route.pair in ("USDG/PYUSD", "PYUSD/USDG")
    ):
        return SOLANA_CROSS_TOKEN_MINIMUM_ALLOWED_THRESHOLD
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
    if route.chain == "solana" and route.pair in ("USDG/PYUSD", "PYUSD/USDG"):
        effective_threshold = (
            Decimal("1") if base_threshold == Decimal("5") else base_threshold
        )
        return strict_execution_floor(effective_threshold, route)
    return strict_execution_floor(base_threshold, route)


def amount_text(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".")


def selected_routes(chains: list[str], pairs: list[str]) -> list[Route]:
    return [Route(chain, pair) for chain in chains for pair in pairs]


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
        executable = "npx.cmd" if sys.platform == "win32" else "npx"
        command = [
            executable,
            "tsx",
            str(PROJECT_ROOT / "src" / "engines" / "solana_flash_arb.ts"),
        ]
        environment.update(
            {
                "SOL_FLASH_ARB_LOAN_TOKEN": route.loan,
                "SOL_FLASH_ARB_INTERMEDIATE_TOKEN": route.intermediate,
                "SOL_FLASH_ARB_MIN_GROSS_PROFIT_USDC": floor,
                "SOL_FLASH_ARB_MIN_NET_PROFIT_USDC": floor,
                f"SOL_FLASH_ARB_MIN_GROSS_PROFIT_{route.loan}": floor,
                f"SOL_FLASH_ARB_MIN_NET_PROFIT_{route.loan}": floor,
                "SOL_FLASH_ARB_OUTPUT_PATH": output_path,
            }
        )
        if route.loan != "USDC":
            environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"] = "false"
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


def execution_detail(route: Route, stdout: str) -> str | None:
    if route.chain == "ethereum":
        try:
            plan = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        tx_hash = plan.get("transactionHash") if isinstance(plan, dict) else None
        return f"https://etherscan.io/tx/0x{str(tx_hash).removeprefix('0x')}" if tx_hash else None
    match = re.search(r"Confirmed:\s*([1-9A-HJ-NP-Za-km-z]+)", stdout)
    return f"https://solscan.io/tx/{match.group(1)}" if match else None


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
    except subprocess.TimeoutExpired:
        return Outcome(False, f"quote timed out after {timeout_seconds:g}s")
    except OSError as exc:
        return Outcome(False, f"could not start engine: {exc}")

    elapsed = time.monotonic() - started
    transaction = execution_detail(route, result.stdout or "")
    if result.returncode == 0:
        if live and not transaction:
            return Outcome(
                False,
                f"engine succeeded in {elapsed:.1f}s without a transaction reference",
            )
        mode = "executed" if transaction else "eligible dry run"
        suffix = f": {transaction}" if transaction else ""
        return Outcome(bool(transaction), f"{mode} in {elapsed:.1f}s{suffix}")

    return Outcome(
        False,
        concise_failure(result.stdout or "", result.stderr or "", result.returncode),
    )


def configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("crosschain-sniper")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(
        LOG_DIR / "crosschain-sniper.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


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
    once: bool,
    stop: threading.Event,
    logger: logging.Logger,
) -> None:
    while not stop.is_set():
        for route in routes:
            if stop.is_set():
                return
            floor = route_execution_floor(route, base_threshold)
            logger.info("checking %s %s", chain.title(), route.pair)
            outcome = run_route(
                route,
                live=live,
                execution_floor=floor,
                timeout_seconds=timeout_seconds,
            )
            level = logging.WARNING if outcome.executed else logging.INFO
            logger.log(level, "%s %s: %s", chain.title(), route.pair, outcome.detail)
            if outcome.executed and stop.wait(cooldown_seconds):
                return
        if once:
            return
        if stop.wait(interval_seconds):
            return


def watch_for_stop_request(stop: threading.Event, logger: logging.Logger) -> None:
    while not stop.wait(0.5):
        if STOP_PATH.exists():
            logger.info("cooperative stop requested")
            stop.set()
            return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold-usd",
        type=Decimal,
        default=decimal_setting("SNIPER_PROFIT_THRESHOLD_USD", "5"),
        help="execute only above this net loan-stablecoin profit (default 5; 1 for Solana USDG/PYUSD & PYUSD/USDG)",
    )
    parser.add_argument(
        "--chains",
        nargs="+",
        choices=("ethereum", "solana"),
        default=["ethereum", "solana"],
    )
    parser.add_argument(
        "--pairs",
        nargs="+",
        choices=("PYUSD/USDC", "USDG/USDC", "USDG/PYUSD", "PYUSD/USDG"),
        default=["PYUSD/USDC", "USDG/USDC", "USDG/PYUSD", "PYUSD/USDG"],
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
        if not PID_PATH.exists():
            raise SniperError("no running sniper PID file was found")
        STOP_PATH.write_text(f"{os.getpid()}\n", encoding="ascii")
        print("Stop requested; the sniper will exit after active route checks finish.")
        return 0
    if args.interval_seconds < 0.25:
        raise SniperError("--interval-seconds must be at least 0.25")
    if args.cooldown_seconds < 0:
        raise SniperError("--cooldown-seconds cannot be negative")
    if args.route_timeout_seconds < 30:
        raise SniperError("--route-timeout-seconds must be at least 30")
    if args.live and args.confirm_live != LIVE_CONFIRMATION:
        raise SniperError(
            f"--live requires --confirm-live {LIVE_CONFIRMATION}"
        )

    routes = selected_routes(args.chains, args.pairs)
    for route in routes:
        route_execution_floor(route, args.threshold_usd)

    logger = configure_logging()
    logger.info(
        "starting %s sniper; execute only when guaranteed net profit is above route execution floor",
        "LIVE" if args.live else "DRY-RUN",
    )
    logger.info("routes: %s", ", ".join(f"{r.chain}:{r.pair}" for r in routes))

    with single_instance():
        stop = threading.Event()
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
                    "once": args.once,
                    "stop": stop,
                    "logger": logger,
                },
            )
            thread.start()
            threads.append(thread)
        try:
            for thread in threads:
                thread.join()
        except KeyboardInterrupt:
            logger.info("shutdown requested")
            stop.set()
            for thread in threads:
                thread.join()
        stop.set()
        watcher.join(timeout=1)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SniperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
