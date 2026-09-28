#!/usr/bin/env python3
import json
import time
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

def run_eth_quote():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        Stealth().apply_stealth_sync(page)
        print("Navigating to meta.matcha.xyz/ethereum...")
        page.goto("https://meta.matcha.xyz/ethereum", timeout=45000, wait_until="domcontentloaded")
        for i in range(15):
            time.sleep(1)
            title = page.title()
            if "checkpoint" not in title.lower() and len(title) > 0:
                print(f"Cleared checkpoint! Page Title: {title}")
                break

        time.sleep(2)
        js_code = """async () => {
            let gasPrice = "30000000000";
            try {
                let gRes = await fetch('https://meta.matcha.xyz/api/gas?chainId=1');
                let gData = await gRes.json();
                gasPrice = String(gData.gasPrice || gData.standard || gData.price || gasPrice);
            } catch(e) {}

            const payload = {
                chainId: 1,
                sellTokenAddress: '0x6c3ea9036406852006290770bedfcaba0e23a0e8', // PYUSD
                buyTokenAddress: '0xe343167631d89b6ffc58b88d6b7fb0228795491d',  // USDG
                sellAmount: '100000000000', // 100,000 (6 dec)
                sellTokenDecimals: 6,
                buyTokenDecimals: 6,
                slippageBps: 10,
                taker: '0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045',
                isAllowanceHolderFlow: true,
                gasPrice: gasPrice
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

            const aggregators = ['0x', '1inch', 'KyberSwap', 'ParaSwap', 'Bebop', 'Barter', 'Enso', 'OKX'];
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
        print("Raw ETH quote results:")
        print(json.dumps(res, indent=2))
        browser.close()

if __name__ == "__main__":
    run_eth_quote()
