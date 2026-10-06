#!/usr/bin/env python3
"""Inspect x-is-human token on meta.matcha.xyz with proper warmup and title wait."""
import json
import time
from urllib.parse import urlparse
from patchright.sync_api import sync_playwright

PROXY_URL = "http://COlBMQ:bCYTai@14.224.225.135:45376"
p = urlparse(PROXY_URL)
p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=False, args=["--headless=new", "--no-sandbox"], proxy=p_cfg)
    c = b.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    page = c.new_page()
    
    captured_requests = []
    page.on("request", lambda r: captured_requests.append({
        "url": r.url,
        "method": r.method,
        "headers": dict(r.headers),
        "post_data": r.post_data
    }) if "/api/" in r.url else None)
    
    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=40000)
    
    cleared = False
    for sec in range(35):
        time.sleep(1)
        t = (page.title() or "").lower()
        if any(w in t for w in ("swap", "matcha", "ethereum")) and not any(k in t for k in ("checkpoint", "403", "forbidden")):
            print(f"Cleared into app in {sec+1}s! Title: {page.title()}")
            cleared = True
            break
        if sec % 5 == 0:
            print(f"  [{sec}s] Waiting for clearance... Current title: '{page.title()}'")
            
    if not cleared:
        print("Final title:", page.title())
        b.close()
        exit(1)
        
    time.sleep(4)
    
    # Check if window.fetch has been patched by BotID
    is_patched = page.evaluate("() => !/\[native code\]/.test(window.fetch.toString())")
    print(f"Is window.fetch patched by BotID: {is_patched}")
    
    # Human mouse movements across swap widget
    page.mouse.move(200, 200)
    time.sleep(0.3)
    page.mouse.move(500, 400, steps=12)
    time.sleep(0.5)
    page.mouse.click(500, 400)
    time.sleep(1.0)
    
    # Type into input
    inputs = page.locator("input")
    if inputs.count() > 0:
        inputs.first.click()
        inputs.first.type("100", delay=80)
        time.sleep(3.0)
        
    print(f"\nCaptured {len(captured_requests)} API requests during session:")
    for req in captured_requests:
        print(f"\n--> {req['method']} {req['url']}")
        h = req["headers"]
        if "x-is-human" in h:
            print(f"    x-is-human: {h['x-is-human']}")
        if "x-kpsdk-ct" in h:
            print(f"    x-kpsdk-ct: {h['x-kpsdk-ct'][:40]}...")
            
    # Now evaluate a competition call using the patched fetch
    print("\n--> Evaluating fetch('/api/competitions')...")
    payload = {
        "chainId": 1,
        "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
        "sellAmount": "100000000",
        "sellTokenDecimals": 6,
        "buyTokenDecimals": 6,
        "slippageBps": 50,
        "taker": "0x50da32e628b45abb1335924086ca0013b9d4ec1c"
    }
    
    comp_eval = page.evaluate("""async (pl) => {
        try {
            const r = await fetch('https://meta.matcha.xyz/api/competitions', {
                method: 'POST',
                headers: {'content-type': 'application/json'},
                body: JSON.stringify(pl)
            });
            return {
                status: r.status,
                mit: r.headers.get('x-vercel-mitigated'),
                text: await r.text()
            };
        } catch(e) { return {error: e.message}; }
    }""", payload)
    
    print("Competition evaluate result:", json.dumps(comp_eval, indent=2))
    
    b.close()
