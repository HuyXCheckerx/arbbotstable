"""Quote-only Ethereum browser probe in a separate, short-lived browser.

Does not load .env, connect to the live bridge, sign, or submit transactions.
"""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.engines.matcha_browser_bridge import QUOTE_SCRIPT

TOKENS = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDG": "0xe343167631d89b6ffc58b88d6b7fb0228795491d",
    "PYUSD": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
}


def main():
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sell", choices=TOKENS, default="USDG")
    parser.add_argument("--buy", choices=TOKENS, default="PYUSD")
    parser.add_argument("--taker", default="0x000000000000000000000000000000000000dead")
    args = parser.parse_args()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.taker):
        parser.error("--taker must be a public Ethereum address")
    report = {"connection": "direct", "purpose": "API/quote probe only; does not prove executable trading",
              "pair": f"{args.sell}/{args.buy}"}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--no-proxy-server",
                                     "--disable-blink-features=AutomationControlled"])
        try:
            page = browser.new_page()
            Stealth().apply_stealth_sync(page)
            page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            payload = {"chainId": 1, "isAllowanceHolderFlow": True, "gasPrice": "1000000000",
                       "sellTokenAddress": TOKENS[args.sell], "buyTokenAddress": TOKENS[args.buy],
                       "sellTokenDecimals": 6, "buyTokenDecimals": 6, "sellAmount": "1000000",
                       "slippageBps": 10, "slippagePpm": 1000, "taker": args.taker}
            result = page.evaluate(QUOTE_SCRIPT, {"chain": "ethereum", "payload": payload, "aggregators": ["0x"]})
            if result.get("provider_error"):
                report["provider_error"] = result["provider_error"]
            elif result.get("error"):
                report["error"] = result["error"]
            else:
                report["competition_created"] = bool(result.get("competitionId"))
                report["quote_status"] = {name: "provider_error" if value.get("error") else "returned"
                                          for name, value in result.get("quotes", {}).items() if isinstance(value, dict)}
        except Exception as exc:
            report["browser_error"] = type(exc).__name__
        finally:
            browser.close()
    print(json.dumps(report, indent=2))
    return 1 if any(key in report for key in ("provider_error", "error", "browser_error")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
