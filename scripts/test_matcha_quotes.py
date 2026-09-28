#!/usr/bin/env python3
import json
import time
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

def run_solana_quote():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        Stealth().apply_stealth_sync(page)
        print("Navigating to meta.matcha.xyz/solana...")
        page.goto("https://meta.matcha.xyz/solana", timeout=45000, wait_until="domcontentloaded")
        for i in range(15):
            time.sleep(1)
            title = page.title()
            if "checkpoint" not in title.lower() and len(title) > 0:
                print(f"Cleared checkpoint! Page Title: {title}")
                break

        time.sleep(2)
        js_code = """async () => {
            const payload = {
                chainId: 1399811149,
                sellTokenAddress: '2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo',
                buyTokenAddress: '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH',
                sellAmount: '100000000000',
                sellTokenDecimals: 6,
                buyTokenDecimals: 6,
                slippageBps: 50,
                taker: '11111111111111111111111111111111'
            };
            const cRes = await fetch('https://meta.matcha.xyz/api/competitions', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const comp = await cRes.json();
            const compId = comp.id || comp.competitionId;
            if (!compId) {
                return { error: 'Competition failed', compResponse: comp };
            }

            const aggregators = ['0x', 'DFlow', 'Jupiter', 'OKX'];
            const quotes = {};
            for (const agg of aggregators) {
                try {
                    const qRes = await fetch('https://meta.matcha.xyz/api/quotes?aggregator=' + agg, {
                        method: 'POST',
                        headers: { 'content-type': 'application/json' },
                        body: JSON.stringify({ competitionId: compId, aggregator: agg })
                    });
                    quotes[agg] = await qRes.json();
                } catch(e) {
                    quotes[agg] = { error: String(e) };
                }
            }
            return { compId, quotes };
        }"""
        res = page.evaluate(js_code)
        print("Raw quote results:")
        print(json.dumps(res, indent=2))
        browser.close()

if __name__ == "__main__":
    run_solana_quote()
