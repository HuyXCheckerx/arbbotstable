#!/usr/bin/env python3
"""Automated MetaMatcha Multi-Aggregator Quote Engine.

Fetches and ranks live quotes from meta.matcha.xyz across all aggregators
(0x, Jupiter, DFlow, OKX on Solana; 0x, 1inch, KyberSwap, ParaSwap, Barter, Enso, OKX on Ethereum).
Bypasses Vercel & Kasada protections using headless Playwright stealth.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# Known token addresses
TOKENS = {
    "solana": {
        "PYUSD": {"address": "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", "decimals": 6},
        "USDG": {"address": "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", "decimals": 6},
        "USDC": {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "decimals": 6},
        "USDT": {"address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "decimals": 6},
    },
    "ethereum": {
        "PYUSD": {"address": "0x6c3ea9036406852006290770bedfcaba0e23a0e8", "decimals": 6},
        "USDG": {"address": "0xe343167631d89b6ffc58b88d6b7fb0228795491d", "decimals": 6},
        "USDC": {"address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6},
        "USDT": {"address": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6},
    },
}

CHAIN_CONFIG = {
    "solana": {
        "chainId": 1399811149,
        "url": "https://meta.matcha.xyz/solana",
        "aggregators": ["0x", "DFlow", "Jupiter", "OKX"],
        "default_taker": "11111111111111111111111111111111",
    },
    "ethereum": {
        "chainId": 1,
        "url": "https://meta.matcha.xyz/ethereum",
        "aggregators": ["0x", "1inch", "KyberSwap", "ParaSwap", "Barter", "Enso", "OKX"],
        "default_taker": "0xd8da6bf26964af9d7eed9e03e53415d37aa96045",
    },
}


def resolve_token(chain: str, symbol_or_addr: str, default_dec: int = 6) -> tuple[str, int, str]:
    chain_tokens = TOKENS.get(chain, {})
    upper = symbol_or_addr.upper()
    if upper in chain_tokens:
        info = chain_tokens[upper]
        return info["address"], info["decimals"], upper

    # Reverse lookup by address
    for sym, info in chain_tokens.items():
        if info["address"].lower() == symbol_or_addr.lower():
            return info["address"], info["decimals"], sym

    return symbol_or_addr, default_dec, symbol_or_addr[:8]


def fetch_matcha_quotes(
    chain: str = "solana",
    sell_token: str = "PYUSD",
    buy_token: str = "USDG",
    amount: float = 100000.0,
    slippage_bps: int = 50,
    taker: str | None = None,
    timeout_sec: int = 40,
) -> dict[str, Any]:
    chain_key = chain.lower()
    if chain_key not in CHAIN_CONFIG:
        raise ValueError(f"Unsupported chain: {chain}. Choose from: {list(CHAIN_CONFIG.keys())}")

    config = CHAIN_CONFIG[chain_key]
    sell_addr, sell_dec, sell_sym = resolve_token(chain_key, sell_token)
    buy_addr, buy_dec, buy_sym = resolve_token(chain_key, buy_token)
    sell_raw = str(int(round(amount * (10 ** sell_dec))))
    taker_addr = taker or config["default_taker"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        Stealth().apply_stealth_sync(page)

        page.goto(config["url"], timeout=timeout_sec * 1000, wait_until="domcontentloaded")

        # Wait for Vercel checkpoint clearance
        cleared = False
        for _ in range(25):
            time.sleep(1)
            title = page.title() or ""
            if "checkpoint" not in title.lower() and len(title) > 0:
                cleared = True
                break

        if not cleared:
            browser.close()
            raise RuntimeError("Failed to clear Vercel checkpoint within timeout")

        time.sleep(2.0)  # Let scripts and Kasada SDK settle

        js_eval = """async (params) => {
            const { chain, chainId, sellToken, buyToken, sellAmount, sellDec, buyDec, slippageBps, taker, aggregators } = params;
            
            let gasPrice = "30000000000";
            if (chain === 'ethereum') {
                try {
                    let gRes = await fetch('https://meta.matcha.xyz/api/gas?chainId=1');
                    let gData = await gRes.json();
                    gasPrice = String(gData.gasPrice || gData.standard || gData.price || gasPrice);
                } catch(e) {}
            }

            const compPayload = {
                chainId,
                sellTokenAddress: sellToken,
                buyTokenAddress: buyToken,
                sellAmount,
                sellTokenDecimals: sellDec,
                buyTokenDecimals: buyDec,
                slippageBps,
                taker,
            };

            if (chain === 'ethereum') {
                compPayload.isAllowanceHolderFlow = true;
                compPayload.gasPrice = gasPrice;
            }

            const cRes = await fetch('https://meta.matcha.xyz/api/competitions', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify(compPayload)
            });

            if (!cRes.ok) {
                return { error: `Competition HTTP ${cRes.status}: ${await cRes.text()}` };
            }

            const comp = await cRes.json();
            const compId = comp.id || comp.competitionId;
            if (!compId) {
                return { error: 'Competition response missing ID', raw: comp };
            }

            // Query all aggregators concurrently
            const quotes = {};
            await Promise.all(aggregators.map(async (agg) => {
                const t0 = performance.now();
                try {
                    const qRes = await fetch('https://meta.matcha.xyz/api/quotes?aggregator=' + agg, {
                        method: 'POST',
                        headers: { 'content-type': 'application/json' },
                        body: JSON.stringify({ competitionId: compId, aggregator: agg })
                    });
                    const data = await qRes.json();
                    const latency = Math.round(performance.now() - t0);
                    quotes[agg] = { ok: qRes.ok, status: qRes.status, data, latency };
                } catch(e) {
                    quotes[agg] = { ok: false, error: String(e), latency: Math.round(performance.now() - t0) };
                }
            }));

            return { compId, quotes };
        }"""

        res = page.evaluate(js_eval, {
            "chain": chain_key,
            "chainId": config["chainId"],
            "sellToken": sell_addr,
            "buyToken": buy_addr,
            "sellAmount": sell_raw,
            "sellDec": sell_dec,
            "buyDec": buy_dec,
            "slippageBps": slippage_bps,
            "taker": taker_addr,
            "aggregators": config["aggregators"],
        })

        browser.close()

    if "error" in res:
        raise RuntimeError(res["error"])

    # Parse quotes into structured ranking
    parsed = []
    for agg, item in res.get("quotes", {}).items():
        data = item.get("data", {})
        direct = data.get("direct", {})
        allowance = data.get("allowanceHolder", {})
        chosen = allowance if allowance.get("quote") else direct

        quote_obj = chosen.get("quote", {})
        sim_obj = chosen.get("simulation", direct.get("simulation", {}))

        buy_amount_raw = quote_obj.get("buyAmount")
        buy_amount = float(buy_amount_raw) / (10 ** buy_dec) if buy_amount_raw else 0.0

        sim_result = sim_obj.get("result", "unknown")
        sources = quote_obj.get("sources", [])
        gas_used = sim_obj.get("details", {}).get("gas") or quote_obj.get("gas")

        parsed.append({
            "aggregator": agg,
            "status": item.get("status", 0),
            "latency_ms": item.get("latency", 0),
            "buy_amount": buy_amount,
            "buy_amount_raw": buy_amount_raw,
            "sources": sources,
            "simulation": sim_result,
            "gas": gas_used,
            "raw_data": data,
        })

    # Sort: simulated success first, then highest output
    parsed.sort(key=lambda x: (x["simulation"] == "success", x["buy_amount"]), reverse=True)

    return {
        "chain": chain_key,
        "competition_id": res.get("compId"),
        "sell_token": sell_sym,
        "buy_token": buy_sym,
        "sell_amount": amount,
        "results": parsed,
    }


def print_quote_summary(data: dict[str, Any]):
    chain = data["chain"].upper()
    sell_amt = data["sell_amount"]
    sell_sym = data["sell_token"]
    buy_sym = data["buy_token"]
    results = data["results"]

    best = results[0] if results else None
    diff = (best["buy_amount"] - sell_amt) if best else 0.0
    spread_str = f"+${diff:.4f}" if diff >= 0 else f"-${abs(diff):.4f}"

    print("=" * 85)
    print(f"MetaMatcha Quote Engine | Chain: {chain} | Pair: {sell_sym} -> {buy_sym}")
    print(f"Sell Amount: {sell_amt:,.2f} {sell_sym} | Best Return: {best['buy_amount']:,.4f} {buy_sym} ({spread_str})")
    print(f"Competition ID: {data['competition_id']}")
    print("=" * 85)
    print(f"{'Aggregator':<12} | {'Output (' + buy_sym + ')':<18} | {'Spread':<10} | {'Sim':<9} | {'Latency':<8} | {'Sources'}")
    print("-" * 85)

    for r in results:
        out = r["buy_amount"]
        spread = out - sell_amt
        spr_str = f"+${spread:.4f}" if spread >= 0 else f"-${abs(spread):.4f}"
        sources = ", ".join(r["sources"]) if r["sources"] else "-"
        sim = r["simulation"]
        lat = f"{r['latency_ms']} ms"
        print(f"{r['aggregator']:<12} | {out:<18.4f} | {spr_str:<10} | {sim:<9} | {lat:<8} | {sources[:25]}")
    print("=" * 85)


def main():
    parser = argparse.ArgumentParser(description="Automate quotes from meta.matcha.xyz")
    parser.add_argument("--chain", default="solana", choices=["solana", "ethereum"], help="Blockchain (default: solana)")
    parser.add_argument("--amount", type=float, default=100000.0, help="Sell amount (default: 100000)")
    parser.add_argument("--from-token", default="PYUSD", help="Sell token symbol or address (default: PYUSD)")
    parser.add_argument("--to-token", default="USDG", help="Buy token symbol or address (default: USDG)")
    parser.add_argument("--taker", default=None, help="Taker address for quoting and simulation")
    parser.add_argument("--slippage-bps", type=int, default=50, help="Slippage tolerance in bps (default: 50)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON format")
    args = parser.parse_args()

    quotes = fetch_matcha_quotes(
        chain=args.chain,
        sell_token=args.from_token,
        buy_token=args.to_token,
        amount=args.amount,
        slippage_bps=args.slippage_bps,
        taker=args.taker,
    )

    if args.json:
        # Strip large binary transaction payloads for clean CLI JSON
        for r in quotes["results"]:
            raw = r.get("raw_data", {})
            for k in ("direct", "allowanceHolder"):
                if k in raw and "quote" in raw[k]:
                    raw[k]["quote"].pop("transaction", None)
                    raw[k]["quote"].pop("data", None)
        print(json.dumps(quotes, indent=2))
    else:
        print_quote_summary(quotes)


if __name__ == "__main__":
    main()
