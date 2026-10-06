#!/usr/bin/env python3
"""Systematic benchmark of MetaMatcha bypass strategies on the VPS."""

import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass

def test_direct_aggregators():
    print("\n" + "=" * 60)
    print(" [BENCHMARK] Testing Direct Aggregators (KyberSwap, Velora)")
    print("=" * 60)
    import urllib.request
    
    # 1. KyberSwap
    pyusd = "0x6c3ea9036406852006290770bedfcaba0e23a0e8"
    usdg = "0xe343167631d89b6ffc58b88d6b7fb0228795491d"
    amount = "100000000000" # 100k
    
    url = f"https://aggregator-api.kyberswap.com/ethereum/api/v1/routes?tokenIn={pyusd}&tokenOut={usdg}&amountIn={amount}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "x-client-id": "arbbot"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            route = data.get("data", {}).get("routeSummary", {})
            out_amt = int(route.get("amountOut", 0)) / 1e6
            print(f"  [SUCCESS] KyberSwap: PYUSD -> USDG | 100k in -> {out_amt:.4f} out")
    except Exception as e:
        print(f"  [FAIL] KyberSwap: {e}")

    # 2. Velora (Paraswap)
    url_v = f"https://api.paraswap.io/prices/?srcToken={pyusd}&destToken={usdg}&amount={amount}&srcDecimals=6&destDecimals=6&side=SELL&network=1"
    req_v = urllib.request.Request(url_v, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req_v, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            out_amt = int(data.get("priceRoute", {}).get("destAmount", 0)) / 1e6
            print(f"  [SUCCESS] Velora: PYUSD -> USDG | 100k in -> {out_amt:.4f} out")
    except Exception as e:
        print(f"  [FAIL] Velora: {e}")


def test_browser_strategies():
    print("\n" + "=" * 60)
    print(" [BENCHMARK] Testing MetaMatcha Browser Evasion Strategies")
    print("=" * 60)
    
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    proxy_url = os.getenv("MATCHA_PROXY", "").strip()
    print(f"Active MATCHA_PROXY: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")
    
    p = urlparse(proxy_url)
    proxy_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password} if proxy_url else None
    
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    
    payload = {
        "chainId": 1,
        "isAllowanceHolderFlow": True,
        "gasPrice": "2000000000",
        "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
        "sellAmount": "100000000",
        "sellTokenDecimals": 6,
        "buyTokenDecimals": 6,
        "slippageBps": 50,
        "taker": "0x50da32e628b45abb1335924086ca0013b9d4ec1c",
    }
    
    with sync_playwright() as pw:
        chrome_exe = pw.chromium.executable_path
        print(f"Chromium Path: {chrome_exe}")
        
        # Launch with modern --headless=new and full desktop context
        browser = pw.chromium.launch(
            executable_path=chrome_exe,
            headless=False,
            args=[
                "--headless=new",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--enable-webgl",
                "--window-size=1920,1080",
            ],
            proxy=proxy_cfg
        )
        
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )
        
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        
        # Intercept wire requests for /api/competitions
        captured = []
        page.on("request", lambda req: captured.append((req.method, req.url, req.headers)) if "competitions" in req.url else None)
        
        print("\n--> [Step 1] Navigating to https://meta.matcha.xyz/ethereum...")
        t0 = time.time()
        page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=35000)
        
        # Poll for clearance
        cleared = False
        for sec in range(20):
            time.sleep(1)
            t = (page.title() or "").lower()
            if t and not any(k in t for k in ("checkpoint", "challenge", "just a moment")):
                cleared = True
                print(f"  [CLEAR] Page cleared in {sec+1}s! Title: {page.title()}")
                break
        
        if not cleared:
            print("  [FAIL] Did not clear Vercel Security Checkpoint.")
            browser.close()
            return
            
        cookies = context.cookies()
        print(f"  Cookies present: {[c['name'] for c in cookies]}")
        
        # Test Strategy 1: Immediate fetch with BotID wrapper
        print("\n--> [Strategy 1: Direct fetch via BotID wrapper]")
        res1 = page.evaluate("""async (pl) => {
            const r = await fetch('https://meta.matcha.xyz/api/competitions', {
                method: 'POST',
                headers: {'content-type': 'application/json', 'x-fetch-native': '1', 'x-taker': pl.taker},
                credentials: 'include',
                body: JSON.stringify(pl)
            });
            return {status: r.status, text: (await r.text()).substring(0, 150), mit: r.headers.get('x-vercel-mitigated')};
        }""", payload)
        print(f"  Result 1: status={res1['status']}, mit={res1.get('mit')}, body={res1['text']}")
        
        # Test Strategy 2: Human telemetry stimulation (mouse moves, scrolling, input focus)
        print("\n--> [Strategy 2: Simulating human mouse curves and focus]")
        # Simulate realistic mouse movements across the swap widget
        page.mouse.move(100, 100)
        time.sleep(0.2)
        page.mouse.move(400, 350, steps=10)
        time.sleep(0.3)
        page.mouse.move(600, 450, steps=15)
        time.sleep(0.2)
        page.mouse.click(600, 450)
        time.sleep(1.0)
        
        # Try finding the input field on the page
        inputs = page.locator("input")
        input_count = inputs.count()
        print(f"  Found {input_count} input elements on page")
        if input_count > 0:
            try:
                # Type into the first input
                first_input = inputs.first
                first_input.click()
                time.sleep(0.2)
                first_input.type("100", delay=100)
                print("  Typed '100' into swap input box")
                time.sleep(2.0)
            except Exception as e:
                print(f"  Input typing notice: {e}")
                
        # Now evaluate fetch again after human interaction
        print("\n--> [Strategy 2: Fetch after behavioral warmup]")
        res2 = page.evaluate("""async (pl) => {
            const r = await fetch('https://meta.matcha.xyz/api/competitions', {
                method: 'POST',
                headers: {'content-type': 'application/json', 'x-fetch-native': '1', 'x-taker': pl.taker},
                credentials: 'include',
                body: JSON.stringify(pl)
            });
            return {status: r.status, text: (await r.text()).substring(0, 150), mit: r.headers.get('x-vercel-mitigated')};
        }""", payload)
        print(f"  Result 2: status={res2['status']}, mit={res2.get('mit')}, body={res2['text']}")
        
        # Test Strategy 3: Check captured wire requests during user interaction
        print("\n--> [Strategy 3: Wire request inspection from UI]")
        print(f"  Captured {len(captured)} requests to /api/competitions:")
        for m, u, h in captured:
            print(f"    {m} {u}")
            print(f"    x-is-human: {h.get('x-is-human', 'none')[:60]}...")
            print(f"    x-kpsdk-ct: {h.get('x-kpsdk-ct', 'none')[:30]}...")

        browser.close()

if __name__ == "__main__":
    test_direct_aggregators()
    test_browser_strategies()
