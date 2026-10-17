"""Get quotes through LlamaSwap and direct DEX aggregators, with live arbitrage execution.

Acts as the USDC/USDT, PYUSD/USDT, and USDG/USDT engine for the sniper bot.
Monitors DEX quotes against Stable.com pools and can execute live flash-loan
arbitrage via the MorphoMatchaStableArbUsdc contract on Ethereum mainnet.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import time

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if load_dotenv is not None:
    load_dotenv(ROOT / ".env", override=False)

API = "https://swap-api.defillama.com/dexAggregatorQuote"
TOKENS = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDG": "0xe343167631d89b6ffc58b88d6b7fb0228795491d",
    "PYUSD": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
}
EXECUTOR_ADDRESS = os.getenv(
    "ETH_ARB_STABLECOIN_EXECUTOR",
    os.getenv("ETH_ARB_EXECUTOR_ADDRESS", "0x20f5Cc012bBb5323EfBFcC91A7d0C960175D3b2c")
)


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


def _get_stable_quote(stable_client, executor: str, asset_from: str, asset_to: str, amount_raw: int):
    """Helper to get a quote from Stable.com."""
    try:
        return stable_client.quote(executor, asset_from, asset_to, amount_raw)
    except Exception:
        return None


def _execute_live_trade(eth_engine, loan_token: str, intermediate_token: str, swap_order: str, amount_str: str, min_profit: str, live: bool, confirm: str):
    """Execute live or simulated arbitrage trade via eth_flash_arb_pyusd_usdc."""
    args_list = [
        "--loan-token", loan_token,
        "--intermediate-token", intermediate_token,
        "--swap-order", swap_order,
        "--amount", amount_str,
        "--base-amount", amount_str,
        "--min-profit", min_profit,
        "--min-net-profit", "0",
        "--slippage-bps", "0",
        "--quote-provider", "direct",
    ]
    if live:
        if confirm not in ("EXECUTE_PROFIT_SNIPER", "EXECUTE_ATOMIC_ARB", "LIVE"):
            print("[-] Live execution requested but confirmation key is invalid", file=sys.stderr)
            return None
        args_list.extend(["--send", "--confirm-mainnet", "EXECUTE_ATOMIC_ARB"])

    parsed = eth_engine.parser().parse_args(args_list)
    return eth_engine.run(parsed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sell", choices=TOKENS, default="USDG", help="Default sell token (e.g. USDC, PYUSD, USDG)")
    parser.add_argument("--buy", choices=TOKENS, default="PYUSD", help="Default buy token (e.g. USDT, PYUSD, USDC)")
    parser.add_argument("--amount", default="100", help="Human token amount, e.g. 100 or 100000")
    parser.add_argument("--protocol", action="append", help="Filter reported adapters; defaults to every displayed route")
    parser.add_argument("--slippage", default="0.1", help="Percentage, e.g. 0.1 = 0.1 percent")
    parser.add_argument("--output", type=Path, help="Save the latest quote report to JSON")
    parser.add_argument("--compact", action="store_true", help="Print compact single-line updates per sample, ideal for VPS logs")
    parser.add_argument("--json", action="store_true", help="Print quote reports as JSON")
    parser.add_argument("--watch", action="store_true", help="Keep the browser alive; the site refreshes quotes automatically")
    parser.add_argument("--samples", type=int, default=0, help="Stop watch mode after this many reports (0 = unlimited)")
    parser.add_argument("--hide-ip", action="store_true", help="Use LlamaSwap's relay instead of direct browser quotes")
    parser.add_argument("--channel", choices=["chrome", "chromium"], default="chromium", help="Browser to launch in an isolated session")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--headful", dest="headful", action="store_true", help="Show the browser (default on desktop)")
    display.add_argument("--headless", dest="headful", action="store_false", help="Hide the browser (default on VPS if DEFILLAMA_HEADLESS=1; compact for headless server)")
    default_headful = not (os.getenv("DEFILLAMA_HEADLESS", "").lower() in ("1", "true", "yes"))
    parser.set_defaults(headful=default_headful)
    parser.add_argument("--interval", type=int, default=30, help="Seconds between watch reports (minimum 10)")
    parser.add_argument("--timeout", type=int, default=15, help="Seconds to collect initial quote responses (default 15)")

    # Sniper Bot & Live Execution Extensions
    parser.add_argument("--all-usdt", "--scan-all", dest="all_usdt", action="store_true", help="Scan all 3 USDT pairs: USDC/USDT, PYUSD/USDT, USDG/USDT")
    parser.add_argument("--pairs", help="Comma-separated pairs to scan, e.g. USDC/USDT,PYUSD/USDT,USDG/USDT")
    parser.add_argument("--min-profit", default="0", help="Minimum gross profit threshold to trigger execution (default: 0)")
    parser.add_argument("--live", "--send", "--execute", dest="live", action="store_true", help="Execute profitable trades live on Ethereum mainnet")
    parser.add_argument("--confirm-live", default="", help="Confirmation string for live execution (EXECUTE_PROFIT_SNIPER or LIVE)")
    parser.add_argument("--confirm-mainnet", default="", help="Confirmation string for mainnet contract (EXECUTE_ATOMIC_ARB)")
    parser.add_argument("--direct", dest="direct_engine", action="store_true", help="Use direct aggregator engine instead of Playwright browser")

    args = parser.parse_args(argv)
    if args.interval < 10 or args.timeout < 1 or args.samples < 0:
        parser.error("Interval must be at least 10 seconds, timeout positive, and samples nonnegative")
    try:
        raw_amount(args.amount)
        slippage = Decimal(args.slippage)
        if not slippage.is_finite() or not 0 <= slippage <= 50:
            raise ValueError("Slippage must be between 0 and 50 percent")
    except (ValueError, InvalidOperation) as exc:
        parser.error(str(exc) if str(exc) else "Invalid slippage")
    if args.sell == args.buy and not (args.all_usdt or args.pairs):
        parser.error("Choose different sell and buy tokens")

    # If direct engine mode or multi-pair scanning with --all-usdt / --pairs is chosen:
    if args.direct_engine:
        from src.engines import eth_flash_arb_pyusd_usdc as eth
        from src.engines.direct_aggregators import DirectAggregatorClient
        pairs_to_scan = [tuple(p.strip().split("/", 1)) for p in args.pairs.split(",")] if args.pairs else [("USDC", "USDT"), ("PYUSD", "USDT"), ("USDG", "USDT")]
        direct_client = DirectAggregatorClient(
            executor=EXECUTOR_ADDRESS, operator=EXECUTOR_ADDRESS, timeout=float(args.timeout),
            api_modules={
                "ArbError": eth.ArbError, "RetryableArbError": eth.RetryableArbError,
                "ProviderRateLimitedError": eth.ProviderRateLimitedError, "MatchaQuote": eth.MatchaQuote,
                "is_address": eth.is_address, "is_hex_data": eth.is_hex_data, "parse_integer": eth.parse_integer,
            }
        )
        stable_client = eth.StableClient(eth.HttpJsonClient(float(args.timeout), "arbbot/1.0", proxy=""), "https://api-defi.stable.com")
        amount_raw = int(Decimal(args.amount) * 1_000_000)
        sample = 0
        while True:
            sample += 1
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            pair_results = []
            best_op = None
            for loan_sym, counter_sym in pairs_to_scan:
                try:
                    dex_quotes = direct_client.quotes(amount_raw, int(slippage * 100), sell_token_address=eth.LOAN_TOKENS[loan_sym], buy_token_address=eth.LOAN_TOKENS[counter_sym])
                    best_dex = dex_quotes[0]
                    sq = stable_client.quote(EXECUTOR_ADDRESS, counter_sym, loan_sym, best_dex.buy_amount)
                    gross = (sq.amount_out - amount_raw) / 1e6
                    op = {"pair": f"{loan_sym}/{counter_sym}", "loan": loan_sym, "counter": counter_sym, "gross": gross, "aggregator": best_dex.aggregator, "swap_order": "dex-first"}
                    pair_results.append(op)
                    if best_op is None or gross > best_op["gross"]:
                        best_op = op
                except Exception:
                    pass
            if args.compact:
                parts = [f"{p['pair']}: {p['gross']:+.2f} {p['loan']} ({p['aggregator']})" for p in pair_results]
                best_txt = f"Best: {best_op['pair']} ({best_op['gross']:+.2f})" if best_op else "No quote"
                print(f"[{now_str}] {' | '.join(parts)} | {best_txt}", flush=True)
            if not args.watch or (args.samples and sample >= args.samples):
                break
            time.sleep(args.interval)
        return 0

    # Default browser-based engine mode with multi-token scanning & Stable.com evaluation
    try:
        from scripts.defillama_browser import BrowserQuotes
    except ModuleNotFoundError:
        from defillama_browser import BrowserQuotes

    protocols = list(dict.fromkeys(args.protocol or []))
    success = False

    # Initialize Stable.com client for spread and profit computation
    try:
        from src.engines import eth_flash_arb_pyusd_usdc as eth
        stable_client = eth.StableClient(eth.HttpJsonClient(float(args.timeout), "arbbot/1.0", proxy=""), "https://api-defi.stable.com")
        direct_client = eth.DirectAggregatorClient(
            executor=EXECUTOR_ADDRESS, operator=EXECUTOR_ADDRESS, timeout=float(args.timeout),
            api_modules={
                "ArbError": eth.ArbError, "RetryableArbError": eth.RetryableArbError,
                "ProviderRateLimitedError": eth.ProviderRateLimitedError, "MatchaQuote": eth.MatchaQuote,
                "is_address": eth.is_address, "is_hex_data": eth.is_hex_data, "parse_integer": eth.parse_integer,
            }
        )
    except Exception:
        eth = None
        stable_client = None
        direct_client = None

    try:
        with BrowserQuotes(tokens=TOKENS, sell=args.sell, buy=args.buy, amount=args.amount,
                           slippage=slippage, protocols=protocols,
                           state_path=ROOT / ".local/llamaswap/browser-state.json",
                           headful=args.headful, hide_ip=args.hide_ip,
                           channel=None if args.channel == 'chromium' else args.channel) as browser:
            delay = args.timeout
            sample = 0
            while True:
                quotes = browser.snapshot(delay)
                sample += 1
                success = any(q["status"] == "quoted" for q in quotes)
                best_quote = max((q for q in quotes if q['status'] == 'quoted'),
                                 key=lambda q: Decimal(q['amount_out']), default=None)

                # Arbitrage computation against Stable.com
                arbitrage_info = None
                if best_quote and stable_client and eth:
                    try:
                        dex_out_raw = int(Decimal(best_quote["amount_out"]) * 1_000_000)
                        amount_in_raw = int(Decimal(args.amount) * 1_000_000)
                        sq = stable_client.quote(EXECUTOR_ADDRESS, args.buy, args.sell, dex_out_raw)
                        gross_profit = (sq.amount_out - amount_in_raw) / 1_000_000
                        arbitrage_info = {
                            "swap_order": "dex-first",
                            "loan_token": args.sell,
                            "intermediate_token": args.buy,
                            "dex_aggregator": best_quote["protocol"],
                            "dex_output": best_quote["amount_out"],
                            "stable_output": format(Decimal(sq.amount_out) / 1_000_000, "f"),
                            "gross_profit": float(gross_profit),
                        }
                    except Exception:
                        pass

                # Scan PYUSD and USDG too if requested or by default on watch
                extra_scans = []
                if (args.all_usdt or args.pairs or args.watch) and direct_client and stable_client and eth:
                    other_loans = [t for t in ("PYUSD", "USDG", "USDC") if t != args.sell]
                    for other in other_loans:
                        try:
                            amt_raw = int(Decimal(args.amount) * 1_000_000)
                            dq = direct_client.quotes(amt_raw, int(slippage * 100), sell_token_address=eth.LOAN_TOKENS[other], buy_token_address=eth.LOAN_TOKENS["USDT"])
                            if dq:
                                b_dex = dq[0]
                                sq_other = stable_client.quote(EXECUTOR_ADDRESS, "USDT", other, b_dex.buy_amount)
                                g_prof = (sq_other.amount_out - amt_raw) / 1e6
                                extra_scans.append({
                                    "pair": f"{other}/USDT",
                                    "gross_profit": float(g_prof),
                                    "loan_token": other,
                                    "aggregator": b_dex.aggregator,
                                })
                        except Exception:
                            pass

                report = {
                    "chain": "ethereum", "sell": args.sell, "buy": args.buy,
                    "amount_in": args.amount, "quotes": quotes,
                    "sample": sample, "hide_ip": args.hide_ip,
                    "best_quote": best_quote,
                    "arbitrage": arbitrage_info,
                    "extra_scans": extra_scans,
                    "note": "Multi-stablecoin sniper engine active. Quotes verified against Stable.com pools."
                }
                rendered = json.dumps(report, indent=2)
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    temporary = args.output.with_name(args.output.name + '.tmp')
                    temporary.write_text(rendered + "\n", encoding="utf-8")
                    temporary.replace(args.output)
                if args.json:
                    print(rendered, flush=True)
                elif args.compact:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    if best_quote:
                        arb_str = f" | Stable Gross: {arbitrage_info['gross_profit']:+.2f} {args.sell}" if arbitrage_info else ""
                        extras_str = " | " + " | ".join(f"{es['pair']}: {es['gross_profit']:+.2f} {es['loan_token']} ({es['aggregator']})" for es in extra_scans) if extra_scans else ""
                        print(f"[{now_str}] {args.amount} {args.sell} -> {args.buy} | Best: {best_quote['protocol']} ({best_quote['amount_out']} {args.buy}){arb_str}{extras_str}", flush=True)
                    else:
                        print(f"[{now_str}] {args.amount} {args.sell} -> {args.buy} | waiting for quotes...", flush=True)
                else:
                    print(f"Ethereum | {args.amount} {args.sell} -> {args.buy} | browser session | quotes & arbitrage", flush=True)
                    for quote in quotes:
                        result = (f"{quote['amount_out']} {args.buy}" if quote['status'] == 'quoted'
                                  else quote.get('error', 'No quote returned'))
                        print(f"{quote['protocol']:<18} {quote['status']:<10} {result}"
                              f" | {quote.get('observed_at', quote.get('updated_at', 'waiting'))}"
                              f"{' | refreshing' if quote.get('refreshing') else ''}", flush=True)
                    if arbitrage_info:
                        print(f"  Arbitrage: {arbitrage_info['gross_profit']:+.2f} {args.sell} via {arbitrage_info['dex_aggregator']} -> Stable.com", flush=True)
                    if extra_scans:
                        print("  Scanned Pairs: " + ", ".join(f"{es['pair']}: {es['gross_profit']:+.2f} {es['loan_token']} ({es['aggregator']})" for es in extra_scans), flush=True)

                # Live Execution Trigger
                if args.live and arbitrage_info and arbitrage_info["gross_profit"] >= float(args.min_profit):
                    confirm_key = args.confirm_live or args.confirm_mainnet
                    print(f"\n[SNIPER FIRING] Executing {args.sell}/{args.buy} live on Ethereum mainnet...", flush=True)
                    try:
                        plan = _execute_live_trade(
                            eth, args.sell, args.buy, arbitrage_info["swap_order"],
                            args.amount, args.min_profit, live=True, confirm=confirm_key
                        )
                        if plan:
                            tx_hash = plan.get("transactionHash")
                            print(f"[!] TRANSACTION CONFIRMED: https://etherscan.io/tx/0x{str(tx_hash).removeprefix('0x')}", flush=True)
                    except Exception as exec_err:
                        print(f"[-] Execution error: {exec_err}", file=sys.stderr)

                if not args.watch or (args.samples and sample >= args.samples):
                    break
                delay = args.interval
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Browser session failed ({type(exc).__name__}); try --headful to inspect the website", file=sys.stderr)
        return 1
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
