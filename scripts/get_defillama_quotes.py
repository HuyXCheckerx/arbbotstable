"""Get quotes through the normal LlamaSwap browser UI, without your API key.

Keeps one browser session open in watch mode. Never connects a wallet or trades.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys


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
    parser.add_argument("--slippage", default="0.1", help="Percentage, e.g. 0.1 = 0.1 percent")
    parser.add_argument("--output", type=Path, help="Save the latest quote report to JSON")
    parser.add_argument("--json", action="store_true", help="Print quote reports as JSON")
    parser.add_argument("--watch", action="store_true", help="Keep the browser alive; the site refreshes quotes automatically")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--headful", dest="headful", action="store_true", help="Show the browser (default; verified working)")
    display.add_argument("--headless", dest="headful", action="store_false", help="Hide the browser; may encounter access challenges")
    parser.set_defaults(headful=True)
    parser.add_argument("--interval", type=int, default=30, help="Seconds between watch reports (minimum 10)")
    parser.add_argument("--timeout", type=int, default=45, help="Seconds to collect initial quote responses")
    args = parser.parse_args(argv)
    if args.interval < 10 or args.timeout < 1:
        parser.error("Interval must be at least 10 seconds and timeout must be positive")
    try:
        raw_amount(args.amount)
        slippage = Decimal(args.slippage)
        if not slippage.is_finite() or not 0 < slippage <= 50:
            raise ValueError("Slippage must be greater than 0 and at most 50 percent")
    except (ValueError, InvalidOperation) as exc:
        parser.error(str(exc) if str(exc) else "Invalid slippage")
    if args.sell == args.buy:
        parser.error("Choose different sell and buy tokens")
    try:
        from scripts.defillama_browser import BrowserQuotes
    except ModuleNotFoundError:
        from defillama_browser import BrowserQuotes
    protocols = list(dict.fromkeys(args.protocol or ["1inch", "Matcha/0x v2"]))
    success = False
    try:
        with BrowserQuotes(tokens=TOKENS, sell=args.sell, buy=args.buy, amount=args.amount,
                           slippage=slippage, protocols=protocols,
                           state_path=ROOT / ".local/llamaswap/browser-state.json",
                           headful=args.headful) as browser:
            delay = args.timeout
            while True:
                quotes = browser.snapshot(delay)
                success = any(q["status"] == "quoted" for q in quotes)
                report = {"chain": "ethereum", "sell": args.sell, "buy": args.buy,
                          "amount_in": args.amount, "quotes": quotes,
                          "note": "Unconnected-wallet website quotes; no transactions signed or submitted"}
                rendered = json.dumps(report, indent=2)
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(rendered + "\n", encoding="utf-8")
                if args.json:
                    print(rendered, flush=True)
                else:
                    print(f"Ethereum | {args.amount} {args.sell} -> {args.buy} | browser session | quotes only", flush=True)
                    for quote in quotes:
                        result = (f"{quote['amount_out']} {args.buy}" if quote['status'] == 'quoted'
                                  else quote.get('error', 'No quote returned'))
                        print(f"{quote['protocol']:<18} {quote['status']:<10} {result}"
                              f" | {quote.get('updated_at', 'waiting')}"
                              f"{' | refreshing' if quote.get('refreshing') else ''}", flush=True)
                if not args.watch:
                    break
                delay = args.interval
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Browser errors can contain URLs with the site's internal integration key.
        print(f"Browser session failed ({type(exc).__name__}); try --headful to inspect the website", file=sys.stderr)
        return 1
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
