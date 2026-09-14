#!/usr/bin/env python3
"""Diagnose MetaMatcha connectivity, Vercel WAF clearance, and proxy setup for 24/7 operation."""

import json
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env if present
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_file, override=False)
    except ImportError:
        pass

print("=" * 65)
print(" METAMATCHA 24/7 CONNECTIVITY & WAF CLEARANCE DIAGNOSTIC")
print("=" * 65)

# 1. Check Public IP & Proxy
print("\n[1/4] Checking Network Egress & IP Classification...")
direct_ip = "unknown"
try:
    import urllib.request
    req = urllib.request.Request("https://ifconfig.me/all.json", headers={"User-Agent": "curl/7.68.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        ip_data = json.loads(resp.read().decode())
        direct_ip = ip_data.get("ip_addr", "unknown")
        print(f"  Direct IP:   {direct_ip}")
        print(f"  Host / ISP:  {ip_data.get('host')}")
        print(f"  Country:     {ip_data.get('country_code')}")
except Exception as exc:
    print(f"  Could not fetch IP info: {exc}")

matcha_proxy = os.getenv("MATCHA_PROXY", "").strip()
if matcha_proxy:
    masked_proxy = matcha_proxy.split("@")[-1] if "@" in matcha_proxy else matcha_proxy
    print(f"  MATCHA_PROXY configured: {masked_proxy}")
    try:
        from curl_cffi import requests
        s = requests.Session(trust_env=False, proxies={"http": matcha_proxy, "https": matcha_proxy})
        p_resp = s.get("https://api.ipify.org", timeout=10)
        print(f"  Proxy Egress IP: {p_resp.text.strip()} (Proxy ACTIVE & CONNECTING)")
    except Exception as p_exc:
        print(f"  WARNING: Failed to connect through MATCHA_PROXY: {p_exc}")
else:
    print("  MATCHA_PROXY: None (Connecting directly from host IP)")

# 2. Test Headless Browser Clearance Solver
print("\n[2/4] Testing Playwright Vercel Security Checkpoint Solver...")
from src.engines import matcha_cookie_manager as cm

t0 = time.perf_counter()
try:
    cookies = cm.get_valid_cookies(force_refresh=True, target_url="https://meta.matcha.xyz/solana")
    elapsed = round(time.perf_counter() - t0, 2)
    has_vcrcs = any(c.get("name") == "_vcrcs" for c in cookies)
    has_cf = any(c.get("name") == "cf_clearance" for c in cookies)
    print(f"  Solve Time:     {elapsed}s")
    print(f"  Total Cookies:  {len(cookies)}")
    print(f"  _vcrcs Token:   {'FOUND (Clearance Granted)' if has_vcrcs else 'MISSING'}")
    print(f"  cf_clearance:   {'FOUND' if has_cf else 'None (Not Required)'}")
except Exception as exc:
    print(f"  Playwright solve failed: {exc}")
    cookies = []

# 3. Test MetaMatcha Gas API with curl_cffi
print("\n[3/4] Testing MetaMatcha Gas Endpoint (GET /api/gas?chainId=1)...")
gas_ok = False
try:
    from curl_cffi import requests
    proxies = {"http": matcha_proxy, "https": matcha_proxy} if matcha_proxy else {"http": "", "https": ""}
    s = requests.Session(impersonate="chrome119", trust_env=False, proxies=proxies)
    cm.inject_matcha_cookies(s, force_refresh=False, target_url="https://meta.matcha.xyz/solana")
    r = s.get(
        "https://meta.matcha.xyz/api/gas?chainId=1",
        headers={
            "origin": "https://meta.matcha.xyz",
            "referer": "https://meta.matcha.xyz/solana",
            "sec-fetch-site": "same-origin",
        },
        timeout=10,
    )
    mitigated = r.headers.get("x-vercel-mitigated", "none")
    req_id = r.headers.get("x-vercel-id", "none")
    print(f"  HTTP Status:         {r.status_code}")
    print(f"  x-vercel-mitigated:  {mitigated}")
    print(f"  Request ID:          {req_id}")
    if r.status_code == 200:
        gas_ok = True
        print(f"  Gas Response:        {r.text[:100]}... (SUCCESS)")
    else:
        print(f"  Response Body:       {r.text[:150]}")
except Exception as exc:
    print(f"  Gas probe failed: {exc}")

# 4. Test MetaMatcha Competition API (POST /api/competitions)
print("\n[4/4] Testing MetaMatcha Competitions Endpoint (POST /api/competitions)...")
comp_ok = False
try:
    competition_payload = {
        "chainId": 1,
        "isAllowanceHolderFlow": True,
        "gasPrice": "50000000",
        "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",   # PYUSD
        "sellAmount": "100000000",
        "sellTokenDecimals": 6,
        "buyTokenDecimals": 6,
        "slippageBps": 0,
        "slippagePpm": 0,
        "taker": "0x0000000000000000000000000000000000000001",
    }
    r2 = s.post(
        "https://meta.matcha.xyz/api/competitions",
        json=competition_payload,
        headers={
            "origin": "https://meta.matcha.xyz",
            "referer": "https://meta.matcha.xyz/ethereum",
            "sec-fetch-site": "same-origin",
            "content-type": "application/json",
        },
        timeout=15,
    )
    mitigated2 = r2.headers.get("x-vercel-mitigated", "none")
    print(f"  HTTP Status:         {r2.status_code}")
    print(f"  x-vercel-mitigated:  {mitigated2}")
    if r2.status_code == 200:
        comp_ok = True
        comp_data = r2.json()
        print(f"  Competition ID:      {comp_data.get('id') or comp_data.get('competitionId')} (SUCCESS)")
    else:
        print(f"  Response Body:       {r2.text[:150]}")
except Exception as exc:
    print(f"  Competition probe failed: {exc}")

print("\n" + "=" * 65)
if gas_ok and comp_ok:
    print(" VERDICT: READY FOR 24/7 ARBITRAGE OPERATION")
    print(" MetaMatcha clearance is verified. Both gas and competition")
    print(" endpoints returned HTTP 200.")
else:
    print(" VERDICT: ACTION REQUIRED")
    if not matcha_proxy:
        print(" Direct access from this IP is restricted by Vercel WAF.")
        print(" Recommended action: Configure a Static Residential Proxy in .env:")
        print("   MATCHA_PROXY=http://username:password@proxyhost:port")
    else:
        print(" Check proxy credentials, IP allowlist, or connection status.")
print("=" * 65 + "\n")
