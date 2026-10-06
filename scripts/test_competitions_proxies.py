#!/usr/bin/env python3
import json
import sys
import time
from urllib.parse import urlparse
from patchright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

test_targets = [
    ("Proxy 19 (ORD-20260930-0014)", "http://COlBMQ:bCYTai@14.224.225.135:45376"),
    ("Direct VPS (No proxy)", None),
]

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

for name, p_url in test_targets:
    print(f"\n==========================================")
    print(f"Testing {name}: {p_url}")
    print(f"==========================================")
    
    proxy_cfg = None
    if p_url:
        p = urlparse(p_url)
        proxy_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}
        
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(
                headless=False,
                args=["--headless=new", "--no-sandbox", "--enable-webgl"],
                proxy=proxy_cfg
            )
            c = b.new_context(
                user_agent=UA,
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                }
            )
            page = c.new_page()
            
            api_events = []
            page.on("response", lambda r: api_events.append((r.status, r.url, r.headers.get("x-vercel-mitigated", "none"))) if "/api/" in r.url else None)
            
            print("Navigating to https://meta.matcha.xyz/ethereum...")
            page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=25000)
            
            for _ in range(15):
                time.sleep(1)
                t = (page.title() or "").lower()
                if "checkpoint" not in t and len(t) > 0:
                    break
            print(f"Page title: {page.title()}")
            time.sleep(2)
            
            # Print initial API calls made by the page
            print(f"Initial API calls made by page: {len(api_events)}")
            for st, u, mit in api_events[:8]:
                print(f"  {st} (mit: {mit}) -> {u.split('?')[0]}")
                
            # Now simulate typing amount into input
            inputs = page.locator("input")
            if inputs.count() > 0:
                print(f"Typing 100 into input 0...")
                inputs.first.click()
                time.sleep(0.3)
                inputs.first.fill("100")
                time.sleep(3.0)
                
            # Check newly triggered API calls
            print(f"Total API calls after input: {len(api_events)}")
            for st, u, mit in api_events[8:]:
                print(f"  {st} (mit: {mit}) -> {u.split('?')[0]}")
                
            b.close()
    except Exception as e:
        print(f"  Execution Error: {e}")
