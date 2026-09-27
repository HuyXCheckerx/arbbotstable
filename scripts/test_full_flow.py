import time
import json
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    Stealth().apply_stealth_sync(page)
    page.goto('https://meta.matcha.xyz/ethereum', wait_until='domcontentloaded')
    for _ in range(20):
        time.sleep(1)
        try:
            t = page.title() or ""
            if "checkpoint" not in t.lower() and len(t) > 0:
                break
        except Exception:
            pass

    time.sleep(3)

    res = page.evaluate("""async () => {
        const gasRes = await fetch('https://meta.matcha.xyz/api/gas?chainId=1');
        const gas = await gasRes.json();
        
        const takerAddr = "0x50da32e628b45abb1335924086ca0013b9d4ec1c"; // LOWERCASE
        const payload = {
            'chainId': 1,
            'sellTokenAddress': '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',
            'buyTokenAddress': '0x6c3ea9036406852006290770bedfcaba0e23a0e8',
            'sellAmount': '100000000',
            'sellTokenDecimals': 6,
            'buyTokenDecimals': 6,
            'slippageBps': 50,
            'isAllowanceHolderFlow': true,
            'gasPrice': gas.price || '2000000000',
            'taker': takerAddr
        };

        const compRes = await fetch('https://meta.matcha.xyz/api/competitions', {
            method: 'POST',
            headers: {
                'content-type': 'application/json',
                'x-fetch-native': '1',
                'x-taker': takerAddr
            },
            body: JSON.stringify(payload)
        });

        const compData = await compRes.json();
        if (!compData.id) {
            return { error: compData };
        }

        const compId = compData.id;
        const aggregators = ['0x', 'KyberSwap', 'Velora'];
        const quoteResults = [];

        for (const agg of aggregators) {
            const qRes = await fetch(`https://meta.matcha.xyz/api/quotes?aggregator=${agg}`, {
                method: 'POST',
                headers: {
                    'content-type': 'application/json',
                    'x-fetch-native': '1',
                    'x-taker': takerAddr
                },
                body: JSON.stringify({ competitionId: compId, aggregator: agg })
            });
            quoteResults.push({ agg: agg, status: qRes.status, body: await qRes.json() });
        }

        return { competitionId: compId, quotes: quoteResults };
    }""")

    print(json.dumps(res, indent=2))
    browser.close()
