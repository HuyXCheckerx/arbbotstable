#!/usr/bin/env python3
"""Diagnose MetaMatcha connectivity and Vercel WAF blocking on VM vs PC."""

import json
import sys
import time

print("=== 1. Checking Public IP & ASN ===")
try:
    import urllib.request
    req = urllib.request.Request("https://ifconfig.me/all.json", headers={"User-Agent": "curl/7.68.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        ip_data = json.loads(resp.read().decode())
        print(f"IP: {ip_data.get('ip_addr')} | Host: {ip_data.get('host')} | Country: {ip_data.get('country_code')}")
except Exception:
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            print(f"IP: {resp.read().decode()}")
    except Exception as e:
        print(f"Could not fetch IP: {e}")

print("\n=== 2. Testing curl_cffi (Chrome 124 Impersonation) ===")
try:
    from curl_cffi import requests
    s = requests.Session(impersonate="chrome124")
    r = s.get(
        "https://meta.matcha.xyz/api/gas?chainId=1",
        headers={
            "origin": "https://meta.matcha.xyz",
            "referer": "https://meta.matcha.xyz/ethereum",
            "sec-fetch-site": "same-origin",
        },
        timeout=10,
    )
    mitigated = r.headers.get("x-vercel-mitigated", "none")
    request_id = r.headers.get("x-vercel-id", "none")
    print(f"curl_cffi HTTP Status: {r.status_code}")
    print(f"x-vercel-mitigated: {mitigated} | request-id: {request_id}")
    print(f"Response: {r.text[:150]}")
except Exception as e:
    print(f"curl_cffi failed: {e}")

print("\n=== 3. Testing Real Headless Chromium (Playwright) ===")
try:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        
        nav = page.goto("https://meta.matcha.xyz/solana", timeout=20000, wait_until="domcontentloaded")
        print(f"Playwright navigation status: {nav.status if nav else 'unknown'}")
        print(f"Playwright page title: {page.title()}")
        
        # Test in-page fetch
        fetch_res = page.evaluate("""async () => {
            try {
                const r = await fetch('/api/gas?chainId=1');
                return {status: r.status, text: await r.text()};
            } catch (err) {
                return {status: 0, error: String(err)};
            }
        }""")
        print(f"Playwright in-browser fetch: {fetch_res}")
        browser.close()
except Exception as e:
    print(f"Playwright test failed: {e}")

print("\n=== 4. Testing ProxyISP Rotating Residential Proxy ===")
import os
proxy_url = os.getenv("MATCHA_PROXY", "http://160.250.166.37:10452")
rotate_url = os.getenv("MATCHA_ROTATE_URL", "http://rotate.proxyisp.net/rotate?key=IbOlVbvxUQzYxtyOWMWypO")
print(f"Target Proxy: {proxy_url}")
print(f"Rotate URL:   {rotate_url}")

# Test rotate endpoint
try:
    import urllib.request
    req = urllib.request.Request(rotate_url, headers={"User-Agent": "curl/7.68.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        rot_data = json.loads(resp.read().decode())
        print(f"Rotation API Response: status={rot_data.get('status')} | msg='{rot_data.get('message')}' | IP={rot_data.get('ip')} | Expires={rot_data.get('Token expiration date')}")
except Exception as exc:
    print(f"Rotation API query failed: {exc}")

# Test proxy connection
try:
    from curl_cffi import requests
    s = requests.Session(impersonate="chrome124")
    r = s.get("https://httpbin.org/ip", proxies={"http": proxy_url, "https": proxy_url}, timeout=10)
    print(f"[OK] Proxy working! Outbound IP via proxy: {r.json().get('origin')}")
except Exception as exc:
    err_str = str(exc)
    print(f"[FAIL] Proxy request failed: {err_str}")
    if "Connection was reset" in err_str or "10054" in err_str or "curl: (55)" in err_str or "curl: (56)" in err_str:
        print("\n" + "!" * 70)
        print(">>> CRITICAL DIAGNOSIS: PROXY RESET CONNECTION (TCP RST) <<<")
        print("ProxyISP uses IP Whitelisting authentication.")
        print(f"Your VPS public IP is: {ip_data.get('ip_addr', 'Unknown')}")
        print("Action needed:")
        print(f"  1. Go to your ProxyISP dashboard: Proxy xoay dan cu - ORD-20260910-0009")
        print(f"  2. In the 'Whitelist IP *' input field, paste: {ip_data.get('ip_addr', 'YOUR_VPS_IP')}")
        print("  3. Click 'Save'")
        print("  4. Wait 10 seconds and rerun this test.")
        print("!" * 70)

