#!/usr/bin/env python3
"""Capture all network calls when interacting with the real UI on meta.matcha.xyz."""

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

proxy_url = os.getenv("MATCHA_PROXY", "http://COlBMQ:bCYTai@14.224.225.135:45376").strip()
print(f"Using proxy: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")
p = urlparse(proxy_url)
proxy_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password} if proxy_url else None

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=False,
        args=["--headless=new", "--no-sandbox", "--enable-webgl"],
        proxy=proxy_cfg
    )
    context = browser.new_context(user_agent=UA, viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    
    network_events = []
    
    def on_request(req):
        if "meta.matcha.xyz/api" in req.url:
            network_events.append({
                "type": "REQ",
                "method": req.method,
                "url": req.url,
                "headers": dict(req.headers),
                "post_data": req.post_data
            })
            
    def on_response(res):
        if "meta.matcha.xyz/api" in res.url:
            network_events.append({
                "type": "RES",
                "status": res.status,
                "url": res.url,
                "headers": dict(res.headers),
            })
            
    page.on("request", on_request)
    page.on("response", on_response)
    
    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=35000)
    
    # Wait for page title
    for sec in range(25):
        time.sleep(1)
        t = (page.title() or "").lower()
        if "checkpoint" not in t and len(t) > 0:
            print(f"Cleared checkpoint in {sec+1}s: {page.title()}")
            break
            
    time.sleep(4)
    
    inputs = page.locator("input")
    print("Found inputs:", inputs.count())
    
    # Click and type into the amount input
    if inputs.count() > 0:
        inp = inputs.first
        print("Clicking first input and typing 1000...")
        inp.click()
        time.sleep(0.5)
        inp.fill("1000")
        time.sleep(5.0)  # Wait for debounce and quote fetch
        
    print(f"\nCaptured {len(network_events)} API network events:")
    for ev in network_events:
        if ev["type"] == "REQ":
            print(f"--> [REQ] {ev['method']} {ev['url'][:80]}")
            if "x-is-human" in ev.get("headers", {}):
                print(f"    x-is-human: {ev['headers']['x-is-human'][:60]}")
        else:
            print(f"<-- [RES] {ev['status']} {ev['url'][:80]} | Mit: {ev['headers'].get('x-vercel-mitigated', 'none')}")
                
    browser.close()
