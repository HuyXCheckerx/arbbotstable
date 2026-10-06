#!/usr/bin/env python3
"""Direct benchmark of in-page API execution on cleared Patchright session."""
import json
import time
from urllib.parse import urlparse
from patchright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
PROXY_URL = "http://COlBMQ:bCYTai@14.224.225.135:45376"

p = urlparse(PROXY_URL)
p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}

payload = {
    "chainId": 1,
    "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
    "sellAmount": "100000000",
    "sellTokenDecimals": 6,
    "buyTokenDecimals": 6,
    "slippageBps": 50,
    "isAllowanceHolderFlow": True,
    "taker": "0x50da32e628b45abb1335924086ca0013b9d4ec1c"
}

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=False, args=["--headless=new", "--no-sandbox"], proxy=p_cfg)
    c = b.new_context(user_agent=UA)
    page = c.new_page()
    
    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=35000)
    
    for sec in range(25):
        time.sleep(1)
        t = (page.title() or "").lower()
        if "checkpoint" not in t and len(t) > 0:
            print(f"Cleared in {sec+1}s! Title: {page.title()}")
            break
            
    time.sleep(4)
    
    # 1. Test gas
    gas_res = page.evaluate("""async () => {
        try {
            const r = await fetch('https://meta.matcha.xyz/api/gas?chainId=1');
            return {status: r.status, mit: r.headers.get('x-vercel-mitigated'), body: await r.text()};
        } catch(e) { return {error: e.message}; }
    }""")
    print("\n--- GAS TEST ---")
    print(json.dumps(gas_res, indent=2))
    
    # 2. Test competitions
    comp_res = page.evaluate("""async (pl) => {
        try {
            const r = await fetch('https://meta.matcha.xyz/api/competitions', {
                method: 'POST',
                headers: {'content-type': 'application/json', 'x-taker': pl.taker},
                credentials: 'include',
                body: JSON.stringify(pl)
            });
            return {status: r.status, mit: r.headers.get('x-vercel-mitigated'), body: await r.text()};
        } catch(e) { return {error: e.message}; }
    }""", payload)
    print("\n--- COMPETITION TEST ---")
    print("Status:", comp_res.get("status"))
    print("Mitigation:", comp_res.get("mit"))
    print("Body:", comp_res.get("body", "")[:300])
    
    # If competition succeeded, test quotes!
    if comp_res.get("status") == 200:
        try:
            comp_data = json.loads(comp_res.get("body"))
            comp_id = comp_data.get("id") or comp_data.get("competitionId")
            print(f"Acquired competitionId: {comp_id}")
            
            quote_res = page.evaluate("""async (cid) => {
                const aggs = ['0x', 'KyberSwap', 'Velora'];
                const res = {};
                for (const a of aggs) {
                    const r = await fetch(`https://meta.matcha.xyz/api/quotes?aggregator=${a}`, {
                        method: 'POST',
                        headers: {'content-type': 'application/json'},
                        credentials: 'include',
                        body: JSON.stringify({competitionId: cid, aggregator: a})
                    });
                    res[a] = {status: r.status, body: await r.text()};
                }
                return res;
            }""", comp_id)
            print("\n--- QUOTES TEST ---")
            print(json.dumps(quote_res, indent=2))
        except Exception as e:
            print("Quote parse error:", e)
            
    b.close()
