#!/usr/bin/env python3
"""Fetch an executable Solana quote from meta.matcha.xyz.

The TypeScript flash-arbitrage engine invokes this helper because Matcha's
Cloudflare edge rejects Node's native HTTP fingerprint.  The helper accepts one
JSON request on stdin and writes exactly one JSON response on stdout.  It never
loads or handles wallet private keys.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

try:
    from curl_cffi import requests
except ImportError as exc:  # pragma: no cover - exercised by deployment checks
    raise SystemExit(
        "curl_cffi is required for Solana MetaMatcha quotes; run "
        "`python -m pip install -r requirements.txt`"
    ) from exc


CHAIN_ID = 1_399_811_149
DEFAULT_BASE_URL = "https://meta.matcha.xyz"
DEFAULT_AGGREGATORS = ("0x", "OKX")
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": "https://matcha.xyz",
    "referer": "https://matcha.xyz/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _session() -> requests.Session:
    session = requests.Session(impersonate="chrome124")
    session.headers.update(HEADERS)
    cookie_text = os.environ.get("SOL_FLASH_ARB_MATCHA_COOKIES", "").strip()
    for item in cookie_text.split(";"):
        if "=" in item:
            name, value = item.strip().split("=", 1)
            session.cookies.set(name.strip(), value.strip())
    return session


def _post_json(url: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    response = _session().post(url, json=payload, timeout=timeout_seconds)
    if response.status_code != 200:
        body = response.text[:500].replace("\n", " ")
        raise RuntimeError(f"HTTP {response.status_code}: {body}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("MetaMatcha returned a non-object response")
    return value


def _direct_result(response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    direct = response.get("direct")
    if not isinstance(direct, dict):
        raise RuntimeError("MetaMatcha response omitted direct result")
    quote = direct.get("quote")
    simulation = direct.get("simulation")
    if not isinstance(quote, dict) or not isinstance(simulation, dict):
        raise RuntimeError("MetaMatcha response omitted quote or simulation")
    return quote, simulation


def _simulation_succeeded(simulation: dict[str, Any]) -> bool:
    result = simulation.get("result")
    return result == "success" or result is True


def select_best_quote(
    responses: dict[str, dict[str, Any]],
    *,
    sell_amount: int,
    taker: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    candidates: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    errors: list[str] = []
    for aggregator, response in responses.items():
        try:
            quote, simulation = _direct_result(response)
            if not _simulation_succeeded(simulation):
                raise RuntimeError(f"simulation result was {simulation.get('result')!r}")
            if str(quote.get("sellAmount")) != str(sell_amount):
                raise RuntimeError("sell amount changed")
            if str(quote.get("taker")) != taker:
                raise RuntimeError("taker changed")
            transaction = quote.get("transaction")
            if not isinstance(transaction, str) or not transaction:
                raise RuntimeError("transaction was omitted")
            base64.b64decode(transaction, validate=True)
            buy_amount = int(str(quote.get("buyAmount", "0")))
            if buy_amount <= 0:
                raise RuntimeError("buy amount was not positive")
            candidates.append((buy_amount, aggregator, quote, simulation))
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            errors.append(f"{aggregator}: {exc}")

    if not candidates:
        detail = "; ".join(errors) or "no aggregator returned a response"
        raise RuntimeError(f"MetaMatcha returned no simulated executable quote ({detail})")
    _, aggregator, quote, simulation = max(candidates, key=lambda item: item[0])
    return aggregator, quote, simulation


def fetch_quote(request: dict[str, Any]) -> dict[str, Any]:
    input_mint = str(request["inputMint"])
    output_mint = str(request["outputMint"])
    taker = str(request["taker"])
    sell_amount = int(str(request["amount"]))
    slippage_bps = int(request.get("slippageBps", 0))
    timeout_seconds = max(1.0, float(request.get("timeoutMs", 15_000)) / 1_000)
    base_url = str(request.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    aggregators = request.get("aggregators") or list(DEFAULT_AGGREGATORS)

    if sell_amount <= 0:
        raise ValueError("amount must be positive")
    if not 0 <= slippage_bps < 10_000:
        raise ValueError("slippageBps must be between 0 and 9999")
    if not isinstance(aggregators, list) or not aggregators:
        raise ValueError("aggregators must be a non-empty list")
    clean_aggregators = [str(value).strip() for value in aggregators if str(value).strip()]
    if not clean_aggregators:
        raise ValueError("aggregators must contain at least one provider")

    competition_payload = {
        "chainId": CHAIN_ID,
        "sellTokenAddress": input_mint,
        "buyTokenAddress": output_mint,
        "sellAmount": str(sell_amount),
        "sellTokenDecimals": 6,
        "buyTokenDecimals": 6,
        "slippageBps": slippage_bps,
        "taker": taker,
    }
    competition = _post_json(
        f"{base_url}/api/competitions", competition_payload, timeout_seconds
    )
    competition_id = competition.get("id") or competition.get("competitionId")
    if not competition_id:
        raise RuntimeError("MetaMatcha returned no competition ID")

    def query(aggregator: str) -> tuple[str, dict[str, Any]]:
        payload = {"competitionId": competition_id, "aggregator": aggregator}
        response = _post_json(
            f"{base_url}/api/quotes?aggregator={aggregator}", payload, timeout_seconds
        )
        return aggregator, response

    responses: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=min(4, len(clean_aggregators))) as pool:
        futures = {pool.submit(query, name): name for name in clean_aggregators}
        for future in as_completed(futures):
            name = futures[future]
            try:
                aggregator, response = future.result()
                responses[aggregator] = response
            except Exception as exc:  # each competitor may fail independently
                failures.append(f"{name}: {exc}")

    try:
        aggregator, quote, simulation = select_best_quote(
            responses, sell_amount=sell_amount, taker=taker
        )
    except RuntimeError as exc:
        if failures:
            raise RuntimeError(f"{exc}; request failures: {'; '.join(failures)}") from exc
        raise

    buy_amount = int(str(quote["buyAmount"]))
    minimum_out = buy_amount * (10_000 - slippage_bps) // 10_000
    sources = quote.get("sources") if isinstance(quote.get("sources"), list) else []
    return {
        "provider": "MetaMatcha",
        "aggregator": aggregator,
        "inputMint": input_mint,
        "outputMint": output_mint,
        "inAmount": str(sell_amount),
        "outAmount": str(buy_amount),
        "otherAmountThreshold": str(minimum_out),
        "swapMode": "ExactIn",
        "slippageBps": slippage_bps,
        "routePlan": [{"swapInfo": {"label": f"{aggregator}: {', '.join(map(str, sources))}"}}],
        "serializedTransaction": quote["transaction"],
        "computeUnitLimit": quote.get("computeUnitLimit"),
        "priorityFee": quote.get("priorityFee"),
        "lastValidBlockHeight": quote.get("lastValidBlockHeight"),
        "simulation": simulation,
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        json.dump(fetch_quote(request), sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(f"MetaMatcha quote failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
