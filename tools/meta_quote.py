#!/usr/bin/env python3
"""Meta-aggregator quote in the style of DefiLlama's LlamaSwap.

LlamaSwap has no public quote API of its own: its frontend asks each DEX
aggregator's public API directly and shows the best result. This script does
the same, from the command line, using only the standard library.

Quote only: it never signs or sends anything. Aggregators that require a key
(1inch, 0x) are skipped unless the key is set; an aggregator that refuses the
request is reported with its error rather than worked around.

Examples:
    python tools/meta_quote.py --sell USDC --buy USDT --amount 1000
    python tools/meta_quote.py --sell PYUSD --buy USDG --amount 50000 --json
    python tools/meta_quote.py --chain base --sell 0x8335...2913 --sell-decimals 6 \\
        --buy 0x4200000000000000000000000000000000000006 --buy-decimals 18 --amount 100
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import os
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = "meta-quote/1.0 (+quote-only CLI)"
# Recipient used when no --taker is given; quotes only, nothing is ever sent.
PLACEHOLDER_TAKER = "0x000000000000000000000000000000000000dEaD"

CHAINS = {
    "ethereum": {"id": 1, "kyber": "ethereum", "cow": "mainnet"},
    "arbitrum": {"id": 42161, "kyber": "arbitrum", "cow": "arbitrum_one"},
    "base": {"id": 8453, "kyber": "base", "cow": "base"},
    "optimism": {"id": 10, "kyber": "optimism", "cow": None},
    "polygon": {"id": 137, "kyber": "polygon", "cow": None},
    "bsc": {"id": 56, "kyber": "bsc", "cow": None},
}

# symbol -> (address, decimals); other tokens can be passed by address.
TOKENS = {
    "ethereum": {
        "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        "USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
        "PYUSD": ("0x6c3ea9036406852006290770BEdFcAbA0e23A0e8", 6),
        "USDG": ("0xe343167631d89B6Ffc58B88d6b7fB0228795491D", 6),
        "DAI": ("0x6B175474E89094C44Da98b954EedeAC495271d0F", 18),
        "WETH": ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),
    },
}


class QuoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class Token:
    address: str
    decimals: int
    label: str


@dataclass(frozen=True)
class QuoteRequest:
    chain: str
    chain_id: int
    sell: Token
    buy: Token
    amount_raw: int
    taker: str
    slippage_bps: int
    timeout: float


@dataclass
class Quote:
    aggregator: str
    buy_amount_raw: int
    gas_usd: float | None = None
    detail: str = ""


def http_json(
    method: str,
    url: str,
    *,
    timeout: float,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        text = exc.read().decode(errors="replace")
        if "<html" in text[:200].lower():
            if "just a moment" in text.lower():
                text = "Cloudflare bot check (not bypassed)"
            else:
                text = "HTML error page"
        raise QuoteError(f"HTTP {exc.code}: {text[:160]}") from None
    except (URLError, TimeoutError) as exc:
        raise QuoteError(f"network error: {getattr(exc, 'reason', exc)}") from None
    except json.JSONDecodeError:
        raise QuoteError("response was not JSON") from None


def optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def velora(q: QuoteRequest) -> Quote:
    params = {
        "srcToken": q.sell.address, "srcDecimals": q.sell.decimals,
        "destToken": q.buy.address, "destDecimals": q.buy.decimals,
        "amount": q.amount_raw, "side": "SELL", "network": q.chain_id, "version": "6.2",
    }
    route = http_json("GET", f"https://api.paraswap.io/prices?{urlencode(params)}", timeout=q.timeout)
    price = route.get("priceRoute") or {}
    if "destAmount" not in price:
        raise QuoteError(str(route)[:160])
    return Quote("Velora", int(price["destAmount"]), optional_float(price.get("gasCostUSD")))


def lifi(q: QuoteRequest) -> Quote:
    params = {
        "fromChain": q.chain_id, "toChain": q.chain_id,
        "fromToken": q.sell.address, "toToken": q.buy.address,
        "fromAmount": q.amount_raw, "fromAddress": q.taker,
        "slippage": q.slippage_bps / 10_000,
    }
    result = http_json("GET", f"https://li.quest/v1/quote?{urlencode(params)}", timeout=q.timeout)
    estimate = result.get("estimate") or {}
    if "toAmount" not in estimate:
        raise QuoteError(result.get("message") or str(result)[:160])
    gas = sum(optional_float(cost.get("amountUSD")) or 0.0 for cost in estimate.get("gasCosts") or [])
    return Quote("LI.FI", int(estimate["toAmount"]), gas or None, f"via {result.get('tool', '?')}")


def cow(q: QuoteRequest) -> Quote:
    network = CHAINS[q.chain]["cow"]
    if not network:
        raise QuoteError(f"CoW Swap does not support {q.chain}")
    body = {
        "sellToken": q.sell.address, "buyToken": q.buy.address,
        "from": q.taker, "receiver": q.taker, "kind": "sell",
        "sellAmountBeforeFee": str(q.amount_raw), "signingScheme": "eip712",
        "onchainOrder": False, "priceQuality": "optimal",
    }
    result = http_json("POST", f"https://api.cow.fi/{network}/api/v1/quote", body=body, timeout=q.timeout)
    quote = result.get("quote") or {}
    if "buyAmount" not in quote:
        raise QuoteError(result.get("description") or str(result)[:160])
    # The network fee is taken from the sell amount, so buyAmount is already net.
    return Quote("CoW Swap", int(quote["buyAmount"]), 0.0, "gasless; fee taken in sell token")


def kyberswap(q: QuoteRequest) -> Quote:
    params = {"tokenIn": q.sell.address, "tokenOut": q.buy.address, "amountIn": q.amount_raw}
    result = http_json(
        "GET",
        f"https://aggregator-api.kyberswap.com/{CHAINS[q.chain]['kyber']}/api/v1/routes?{urlencode(params)}",
        headers={"x-client-id": os.getenv("KYBERSWAP_CLIENT_ID", "meta-quote")},
        timeout=q.timeout,
    )
    summary = (result.get("data") or {}).get("routeSummary") or {}
    if "amountOut" not in summary:
        raise QuoteError(result.get("message") or str(result)[:160])
    return Quote("KyberSwap", int(summary["amountOut"]), optional_float(summary.get("gasUsd")))


def odos(q: QuoteRequest) -> Quote:
    body = {
        "chainId": q.chain_id,
        "inputTokens": [{"tokenAddress": q.sell.address, "amount": str(q.amount_raw)}],
        "outputTokens": [{"tokenAddress": q.buy.address, "proportion": 1}],
        "userAddr": q.taker, "slippageLimitPercent": q.slippage_bps / 100, "compact": True,
    }
    result = http_json("POST", "https://api.odos.xyz/sor/quote/v2", body=body, timeout=q.timeout)
    amounts = result.get("outAmounts") or []
    if not amounts:
        raise QuoteError(result.get("detail") or str(result)[:160])
    return Quote("Odos", int(amounts[0]), optional_float(result.get("gasEstimateValue")))


def openocean(q: QuoteRequest) -> Quote:
    params = {
        "inTokenAddress": q.sell.address, "outTokenAddress": q.buy.address,
        "amountDecimals": q.amount_raw, "gasPriceDecimals": 1_000_000_000,
    }
    result = http_json("GET", f"https://open-api.openocean.finance/v4/{q.chain_id}/quote?{urlencode(params)}", timeout=q.timeout)
    data = result.get("data") or {}
    if "outAmount" not in data:
        raise QuoteError(result.get("error") or str(result)[:160])
    return Quote("OpenOcean", int(data["outAmount"]))


def oneinch(q: QuoteRequest) -> Quote:
    key = os.getenv("ONEINCH_API_KEY")
    if not key:
        raise QuoteError("skipped: set ONEINCH_API_KEY")
    params = {"src": q.sell.address, "dst": q.buy.address, "amount": q.amount_raw}
    result = http_json(
        "GET", f"https://api.1inch.dev/swap/v6.0/{q.chain_id}/quote?{urlencode(params)}",
        headers={"Authorization": f"Bearer {key}"}, timeout=q.timeout,
    )
    if "dstAmount" not in result:
        raise QuoteError(result.get("description") or str(result)[:160])
    return Quote("1inch", int(result["dstAmount"]))


def zero_ex(q: QuoteRequest) -> Quote:
    key = os.getenv("ZERO_EX_API_KEY")
    if not key:
        raise QuoteError("skipped: set ZERO_EX_API_KEY")
    params = {
        "chainId": q.chain_id, "sellToken": q.sell.address, "buyToken": q.buy.address,
        "sellAmount": q.amount_raw, "taker": q.taker,
    }
    result = http_json(
        "GET", f"https://api.0x.org/swap/allowance-holder/price?{urlencode(params)}",
        headers={"0x-api-key": key, "0x-version": "v2"}, timeout=q.timeout,
    )
    if "buyAmount" not in result:
        raise QuoteError(result.get("message") or str(result)[:160])
    return Quote("0x", int(result["buyAmount"]))


AGGREGATORS: dict[str, Callable[[QuoteRequest], Quote]] = {
    "velora": velora,
    "lifi": lifi,
    "cow": cow,
    "kyberswap": kyberswap,
    "odos": odos,
    "openocean": openocean,
    "1inch": oneinch,
    "0x": zero_ex,
}


def resolve_token(chain: str, value: str, decimals: int | None) -> Token:
    known = TOKENS.get(chain, {}).get(value.upper())
    if known:
        return Token(known[0], known[1], value.upper())
    if value.startswith("0x") and len(value) == 42:
        if decimals is None:
            raise SystemExit(f"--sell-decimals/--buy-decimals is required for address {value}")
        return Token(value, decimals, value[:6] + "…" + value[-4:])
    raise SystemExit(f"unknown token {value!r} on {chain}; pass its address and decimals")


def to_raw(amount: str, decimals: int) -> int:
    raw = Decimal(amount) * (Decimal(10) ** decimals)
    if raw != raw.to_integral_value() or raw <= 0:
        raise SystemExit(f"--amount must be positive with at most {decimals} decimals")
    return int(raw)


def from_raw(raw: int, decimals: int) -> str:
    return f"{Decimal(raw) / (Decimal(10) ** decimals):f}"


def collect(request: QuoteRequest, names: list[str]) -> tuple[list[Quote], dict[str, str]]:
    quotes: list[Quote] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {pool.submit(AGGREGATORS[name], request): name for name in names}
        for future in as_completed(futures):
            name = futures[future]
            try:
                quotes.append(future.result())
            except QuoteError as exc:
                errors[name] = str(exc)
            except Exception as exc:  # one broken adapter must not hide the others
                errors[name] = f"{type(exc).__name__}: {exc}"
    quotes.sort(key=lambda quote: quote.buy_amount_raw, reverse=True)
    return quotes, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chain", choices=sorted(CHAINS), default="ethereum")
    parser.add_argument("--sell", required=True, help="token symbol (Ethereum) or address")
    parser.add_argument("--buy", required=True, help="token symbol (Ethereum) or address")
    parser.add_argument("--amount", required=True, help="sell amount in whole tokens, e.g. 1000")
    parser.add_argument("--sell-decimals", type=int)
    parser.add_argument("--buy-decimals", type=int)
    parser.add_argument("--taker", default=PLACEHOLDER_TAKER, help="address the route is quoted for")
    parser.add_argument("--slippage-bps", type=int, default=50)
    parser.add_argument("--only", help=f"comma-separated subset of: {','.join(AGGREGATORS)}")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args(argv)

    names = [name.strip().lower() for name in (args.only or ",".join(AGGREGATORS)).split(",") if name.strip()]
    unknown = [name for name in names if name not in AGGREGATORS]
    if unknown:
        parser.error(f"unknown aggregator(s): {', '.join(unknown)}")

    sell = resolve_token(args.chain, args.sell, args.sell_decimals)
    buy = resolve_token(args.chain, args.buy, args.buy_decimals)
    request = QuoteRequest(
        chain=args.chain,
        chain_id=CHAINS[args.chain]["id"],
        sell=sell,
        buy=buy,
        amount_raw=to_raw(args.amount, sell.decimals),
        taker=args.taker,
        slippage_bps=args.slippage_bps,
        timeout=args.timeout,
    )

    started = time.monotonic()
    quotes, errors = collect(request, names)
    elapsed = time.monotonic() - started

    if args.json:
        print(json.dumps({
            "chain": args.chain,
            "sell": asdict(sell), "buy": asdict(buy),
            "sellAmountRaw": str(request.amount_raw),
            "quotes": [
                {**asdict(quote), "buy_amount_raw": str(quote.buy_amount_raw),
                 "buy_amount": from_raw(quote.buy_amount_raw, buy.decimals)}
                for quote in quotes
            ],
            "errors": errors,
            "elapsedSeconds": round(elapsed, 3),
        }, indent=2))
        return 0 if quotes else 1

    print(f"{args.amount} {sell.label} -> {buy.label} on {args.chain}  ({elapsed:.1f}s)\n")
    if quotes:
        best = quotes[0].buy_amount_raw
        print(f"{'Aggregator':<11} {'Receive':>22} {'vs best':>9} {'Gas USD':>8}  Notes")
        for quote in quotes:
            difference_bps = (quote.buy_amount_raw - best) * 10_000 / best if best else 0
            gas = f"{quote.gas_usd:.2f}" if quote.gas_usd is not None else "--"
            print(
                f"{quote.aggregator:<11} {from_raw(quote.buy_amount_raw, buy.decimals):>22} "
                f"{difference_bps:>7.1f}bp {gas:>8}  {quote.detail}"
            )
    else:
        print("No aggregator returned a quote.")
    for name, error in sorted(errors.items()):
        print(f"  {name}: {error}")
    return 0 if quotes else 1


if __name__ == "__main__":
    sys.exit(main())
