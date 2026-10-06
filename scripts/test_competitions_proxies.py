#!/usr/bin/env python3
"""Benchmark headless=False on Windows VPS with Proxy 19."""
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

print(f"Launching Real Non-Headless Browser with Proxy 19...")
with sync_playwright() as pw:
    b = pw.chromium.launch(
        headless=False,
        args=[
            "--no-sandbox",
            "--window-size=1920,1080",
            "--start-maximized",
        ],
        proxy=p_cfg
    )
    c = b.new_context(
        user_agent=UA,
        viewport={"width": 1920, "height": 1080},
        screen={"width": 1920, "height": 1080},
    )
    page = c.new_page()
    
    wire_reqs = []
    page.on("request", lambda r: wire_reqs.append((r.method, r.url, dict(r.headers))) if "/api/" in r.url else None)
    
    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=40000)
    
    cleared = False
    for sec in range(30):
        time.sleep(1)
        t = (page.title() or "").lower()
        if not any(k in t for k in ("checkpoint", "challenge", "403", "forbidden")) and len(t) > 0:
            print(f"Page cleared in {sec+1}s! Title: {page.title()}")
            cleared = True
            break
            
    if not cleared:
        print(f"Page title after 30s: {page.title()}")
        
    time.sleep(4)
    
    # Behavioral warmup
    page.mouse.move(300, 300)
    time.sleep(0.3)
    page.mouse.move(600, 400, steps=10)
    time.sleep(0.5)
    page.mouse.click(600, 400)
    time.sleep(1.0)
    
    inputs = page.locator("input")
    print(f"Found {inputs.count()} inputs.")
    if inputs.count() > 0:
        inputs.first.click()
        time.sleep(0.3)
        inputs.first.fill("100")
        time.sleep(4.0)
        
    # Check gas endpoint
    gas_res = page.evaluate("""async () => {
        try {
            const r = await fetch('https://meta.matcha.xyz/api/gas?chainId=1');
            return {status: r.status, mit: r.headers.get('x-vercel-mitigated'), body: (await r.text()).substring(0, 100)};
        } catch(e) { return {error: e.message}; }
    }""")
    print("\n--- GAS TEST ---")
    print(json.dumps(gas_res, indent=2))
    
    # Check competition endpoint
    comp_res = page.evaluate("""async (pl) => {
        try {
            const r = await fetch('https://meta.matcha.xyz/api/competitions', {
                method: 'POST',
                headers: {'content-type': 'application/json', 'x-taker': pl.taker},
                credentials: 'include',
                body: JSON.stringify(pl)
            });
            return {status: r.status, mit: r.headers.get('x-vercel-mitigated'), body: (await r.text()).substring(0, 150)};
        } catch(e) { return {error: e.message}; }
    }""", payload)
    print("\n--- COMPETITION TEST ---")
    print(json.dumps(comp_res, indent=2))
    
    # Check wire requests
    print(f"\nCaptured {len(wire_reqs)} wire requests:")
    for m, u, h in wire_reqs[-6:]:
        print(f"  {m} {u.split('?')[0]}")
        if "x-is-human" in h:
            print(f"    x-is-human: {h['x-is-human']}")
            
    b.close()
