#!/usr/bin/env python3
"""Probe public MetaMatcha API reachability without wallet keys or trading."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


# Direct execution from any working directory must use this checkout's module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.engines.provider_http import (  # noqa: E402
    access_block_detail,
    rate_limit_detail,
    response_headers,
    retry_after_seconds,
    safe_endpoint,
)


BASE_URL = "https://meta.matcha.xyz"
SELECTED_HEADERS = (
    "server", "content-type", "x-vercel-mitigated", "x-vercel-id", "retry-after", "date",
)
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://matcha.xyz",
    "referer": "https://matcha.xyz/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}


def competition_payload(chain: str, gas: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sellAmount": "1000000", "sellTokenDecimals": 6,
        "buyTokenDecimals": 6, "slippageBps": 10,
    }
    if chain == "ethereum":
        gas_price = next(
            (gas[key] for key in ("price", "gasPrice", "fast", "standard") if key in gas),
            None,
        )
        if gas_price is None or not str(gas_price).isdigit():
            raise ValueError("gas endpoint omitted a numeric gas price")
        payload.update({
            "chainId": 1,
            "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "buyTokenAddress": "0xe343167631d89b6ffc58b88d6b7fb0228795491d",
            "taker": "0x000000000000000000000000000000000000dEaD",
            "isAllowanceHolderFlow": True,
            "gasPrice": str(gas_price),
            "slippagePpm": 1000,
        })
    else:
        payload.update({
            "chainId": 1_399_811_149,
            "sellTokenAddress": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "buyTokenAddress": "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",
            "taker": "11111111111111111111111111111111",
        })
    return payload


def response_record(response: Any, url: str, method: str) -> dict[str, Any]:
    headers = response_headers(response)
    detail = access_block_detail(response, url)
    if detail is None and response.status_code == 429:
        detail = rate_limit_detail(response, url)
    record: dict[str, Any] = {
        "method": method,
        "endpoint": safe_endpoint(url),
        "status": response.status_code,
        "headers": {
            name: " ".join(headers[name].split())[:200]
            for name in SELECTED_HEADERS if name in headers
        },
        "retry_after_present": "retry-after" in headers,
        "retry_after_seconds": retry_after_seconds(response),
        "diagnostic": detail or f"HTTP {response.status_code}",
    }
    # Only known edge-page titles are reported. Bodies, tokens and arbitrary
    # server-provided titles are intentionally absent from shareable reports.
    if detail and "Vercel Security Checkpoint" in detail:
        record["title"] = "Vercel Security Checkpoint"
    return record


def diagnose(chain: str, timeout: float) -> tuple[dict[str, Any], int]:
    from curl_cffi import requests

    report: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "chain": chain,
        "purpose": "API reachability only; does not prove executable quotes",
        "gas_probe_chain": "ethereum",
        "requests": [],
    }
    session = requests.Session(impersonate="chrome124")
    session.headers.update(HEADERS)
    try:
        gas_url = f"{BASE_URL}/api/gas?chainId=1"
        gas_response = session.get(gas_url, timeout=timeout, allow_redirects=False)
        report["requests"].append(response_record(gas_response, gas_url, "GET"))
        if gas_response.status_code != 200:
            report["stopped_reason"] = "gas probe failed; no further requests made"
            return report, 1
        gas = gas_response.json()
        if not isinstance(gas, dict):
            raise ValueError("gas endpoint returned a non-object response")
        payload = competition_payload(chain, gas)
        competition_url = f"{BASE_URL}/api/competitions"
        competition_response = session.post(
            competition_url, json=payload, timeout=timeout, allow_redirects=False,
        )
        report["requests"].append(
            response_record(competition_response, competition_url, "POST")
        )
        if competition_response.status_code != 200:
            report["stopped_reason"] = "competition probe failed; no retries made"
            return report, 1
        report["result"] = "Both probes succeeded; no executable quote was requested"
        return report, 0
    except Exception as exc:
        # Transport exceptions may contain proxy credentials or response bodies.
        report["stopped_reason"] = f"probe failed ({type(exc).__name__}); no retries made"
        return report, 1
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Test MetaMatcha API reachability with one gas GET and at most one "
            "read-only competition POST. This does not prove executable quotes. "
            "No .env, wallet keys, swaps, signatures, broadcasts or retries are used."
        ),
    )
    parser.add_argument("--chain", choices=("ethereum", "solana"), default="ethereum")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request seconds (default: 15)")
    parser.add_argument("--output", type=Path, help="Also save the sanitized JSON report here")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 120:
        parser.error("--timeout must be greater than zero and at most 120 seconds")
    try:
        report, status = diagnose(args.chain, args.timeout)
    except ImportError:
        parser.exit(2, "curl_cffi is required; install requirements.txt first\n")
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
