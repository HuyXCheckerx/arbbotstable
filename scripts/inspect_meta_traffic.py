#!/usr/bin/env python3
"""Inspect exact request headers sent by Next.js app on meta.matcha.xyz."""
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
    
    captured_reqs = []
    page.on("request", lambda r: captured_reqs.append({
        "url": r.url,
        "method": r.method,
        "headers": dict(r.headers),
        "post_data": r.post_data
    }) if "/api/" in r.url else None)
    
    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="networkidle", timeout=35000)
    print("Title:", page.title())
    time.sleep(3)
    
    print(f"\nCaptured {len(captured_reqs)} API requests from Next.js app:")
    for req in captured_reqs[:5]:
        print("\nURL:", req["url"])
        print("Method:", req["method"])
        print("Headers:")
        for k, v in req["headers"].items():
            print(f"  {k}: {v[:80] if len(v) > 80 else v}")
            
    b.close()
