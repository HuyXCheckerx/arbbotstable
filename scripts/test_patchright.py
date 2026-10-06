#!/usr/bin/env python3
"""Test patchright evasion on meta.matcha.xyz on the VPS."""

import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from patchright.sync_api import sync_playwright

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass

proxy_url = os.getenv("MATCHA_PROXY", "").strip()
print(f"Proxy: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")

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
    print(f"Launching Patchright Chromium...")
    browser = pw.chromium.launch(
        headless=False,
        args=[
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--enable-webgl",
        ],
        proxy=proxy_cfg
    )
    context = browser.new_context(
        user_agent=UA,
        viewport={"width": 1920, "height": 1080},
        extra_http_headers={
            "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    )
    page = context.new_page()
    
    wire_reqs = []
    page.on("request", lambda req: wire_reqs.append((req.method, req.url, dict(req.headers))) if "competitions" in req.url else None)
    
    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=35000)
    
    for sec in range(20):
        time.sleep(1)
        t = (page.title() or "").lower()
        if t and not any(k in t for k in ("checkpoint", "challenge", "just a moment")):
            print(f"Cleared in {sec+1}s: {page.title()}")
            break
            
    time.sleep(2.0)
    
    # Interactivity
    page.mouse.move(200, 200)
    time.sleep(0.2)
    page.mouse.move(500, 400, steps=8)
    time.sleep(0.5)
    page.mouse.click(500, 400)
    time.sleep(1.0)
    
    print("\n--> Testing POST /api/competitions with Patchright...")
    res = page.evaluate("""async (pl) => {
        const r = await fetch('https://meta.matcha.xyz/api/competitions', {
            method: 'POST',
            headers: {'content-type': 'application/json', 'x-fetch-native': '1', 'x-taker': pl.taker},
            credentials: 'include',
            body: JSON.stringify(pl)
        });
        return {
            status: r.status,
            mit: r.headers.get('x-vercel-mitigated'),
            text: (await r.text()).substring(0, 200)
        };
    }""", payload)
    
    print("STATUS:", res["status"])
    print("MITIGATION:", res.get("mit"))
    print("RESPONSE BODY:", res.get("text"))
    
    if wire_reqs:
        print("\nLast captured wire request:")
        m, u, h = wire_reqs[-1]
        print("x-is-human:", h.get("x-is-human", "none")[:80])
        print("x-kpsdk-ct:", h.get("x-kpsdk-ct", "none")[:40])
        
    browser.close()
