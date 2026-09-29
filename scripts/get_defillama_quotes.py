"""Get Ethereum stablecoin quotes from LlamaSwap. Never signs or submits swaps.

Request contract observed in swap.defillama.com's frontend: POST
/dexAggregatorQuote with protocol/chain/from/to/amount/api_key query parameters
and the adapter's extra options as JSON. This is a frontend API, not the Pro data API.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
API = "https://swap-api.defillama.com/dexAggregatorQuote"
TOKENS = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDG": "0xe343167631d89b6ffc58b88d6b7fb0228795491d",
    "PYUSD": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
}


def raw_amount(value: str) -> str:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000000"):
            raise ValueError
        raw = amount * 1_000_000
        if raw != raw.to_integral_value():
            raise ValueError
        return str(int(raw))
    except (InvalidOperation, ValueError):
        raise ValueError("Amount must be positive, at most 1 trillion, with at most 6 decimal places") from None


def redact(value, key):
    if isinstance(value, str):
        return value.replace(key, "<redacted>") if key else value
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, dict):
        return {redact(str(k), key): redact(v, key) for k, v in value.items()}
    return value


def get_quote(session, *, key, protocol, sell, buy, amount_raw, taker, slippage, timeout=20):
    params = {"protocol": protocol, "chain": "ethereum", "from": TOKENS[sell],
              "to": TOKENS[buy], "amount": amount_raw, "api_key": key}
    body = {"userAddress": taker, "slippage": float(slippage), "amountOut": "0",
            "fromToken": {"address": TOKENS[sell], "decimals": 6, "symbol": sell},
            "toToken": {"address": TOKENS[buy], "decimals": 6, "symbol": buy}}
    record = {"protocol": protocol, "status": "error"}
    try:
        response = session.post(API, params=params, json=body, timeout=timeout, allow_redirects=False)
    except Exception as exc:
        # Transport exception strings may contain the authenticated URL.
        return {**record, "error": f"Transport failure ({type(exc).__name__})"}
    record["http_status"] = response.status_code
    headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    for header in ("cf-ray", "cf-mitigated", "retry-after"):
        if headers.get(header):
            record[header] = redact(headers[header][:160], key)
    content = response.text
    if headers.get("cf-mitigated") == "challenge" or "just a moment" in content.lower():
        return {**record, "error": "Cloudflare challenge blocked the quote request; API key acceptance is unverified"}
    try:
        data = response.json()
    except ValueError:
        return {**record, "error": f"Non-JSON response (HTTP {response.status_code}); no quote returned"}
    data = redact(data, key)
    if response.status_code != 200:
        return {**record, "error": f"HTTP {response.status_code}", "response": data}
    if not isinstance(data, dict):
        return {**record, "status": "no_quote", "response": data}
    try:
        amount = Decimal(str(data.get("amountReturned")))
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise InvalidOperation
    except InvalidOperation:
        return {**record, "status": "no_quote", "response": data}
    return {**record, "status": "quoted", "amount_out_raw": str(int(amount)),
            "amount_out": format(amount / 1_000_000, "f"),
            "estimated_gas": data.get("estimatedGas"), "response": data}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sell", choices=TOKENS, default="USDG")
    parser.add_argument("--buy", choices=TOKENS, default="PYUSD")
    parser.add_argument("--amount", default="100", help="Human token amount, e.g. 100 or 0.5")
    parser.add_argument("--protocol", action="append", help='Repeat to compare adapters; defaults: "1inch", "Matcha/0x v2"')
    parser.add_argument("--taker", help="Public Ethereum address; defaults to ETH_EXECUTOR_ADDRESS or a dummy address")
    parser.add_argument("--slippage", default="0.1", help="Percentage, e.g. 0.1 = 0.1 percent")
    parser.add_argument("--output", type=Path, help="Save quotes and unsigned response data to JSON")
    parser.add_argument("--json", action="store_true", help="Print the complete sanitized report")
    args = parser.parse_args(argv)
    try:
        amount_raw = raw_amount(args.amount)
        slippage = Decimal(args.slippage)
        if not slippage.is_finite() or not 0 < slippage <= 50:
            raise ValueError("Slippage must be greater than 0 and at most 50 percent")
    except (ValueError, InvalidOperation) as exc:
        parser.error(str(exc) if str(exc) else "Invalid slippage")
    if args.sell == args.buy:
        parser.error("Choose different sell and buy tokens")
    local = dotenv_values(ROOT / ".env.defillama")
    general = dotenv_values(ROOT / ".env")
    key = os.getenv("DEFILLAMA_SWAP_API_KEY") or local.get("DEFILLAMA_SWAP_API_KEY") or general.get("DEFILLAMA_SWAP_API_KEY")
    if not key:
        parser.error("Set DEFILLAMA_SWAP_API_KEY in .env.defillama or your environment")
    taker = args.taker or general.get("ETH_EXECUTOR_ADDRESS") or "0x000000000000000000000000000000000000dead"
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", taker):
        parser.error("Taker must be a public Ethereum address")
    from curl_cffi import requests
    session = requests.Session(impersonate="chrome", trust_env=False, proxies={"http": "", "https": ""})
    session.headers.update({"accept": "application/json", "origin": "https://swap.defillama.com",
                            "referer": "https://swap.defillama.com/"})
    quotes = []
    try:
        for protocol in dict.fromkeys(args.protocol or ["1inch", "Matcha/0x v2"]):
            quotes.append(get_quote(session, key=key, protocol=protocol, sell=args.sell, buy=args.buy,
                                    amount_raw=amount_raw, taker=taker, slippage=slippage))
    finally:
        session.close()
    report = {"chain": "ethereum", "sell": args.sell, "buy": args.buy, "amount_in": args.amount,
              "taker": taker, "quotes": quotes, "note": "Quotes only; no transactions signed or submitted"}
    rendered = json.dumps(redact(report, key), indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if args.json:
        print(rendered)
    else:
        print(f"Ethereum | {args.amount} {args.sell} -> {args.buy} | quotes only")
        for quote in quotes:
            result = f"{quote['amount_out']} {args.buy}" if quote['status'] == 'quoted' else quote.get('error', 'No quote returned')
            print(f"{quote['protocol']:<18} {quote['status']:<10} {result}")
    return 0 if any(q["status"] == "quoted" for q in quotes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
