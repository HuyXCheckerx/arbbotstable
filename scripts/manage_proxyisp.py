#!/usr/bin/env python3
"""Manage ProxyISP proxies via the client API.

Capabilities:
- Query wallet balance
- List active proxies and test MetaMatcha connectivity
- Buy 1-day Vietnam residential proxy automatically
- Update MATCHA_PROXY in .env
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from curl_cffi import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

BASE_URL = "https://api.proxyisp.net"
XOR_KEY = bytes.fromhex("f0d7985145df712f00f1f45711d1ef373d46895d5d0b2ae19cf16a202569e221")
VN_RESIDENTIAL_PRODUCT_ID = "698f174928abf7a4ed22d2c7"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def decrypt_payload(enc_str: str) -> str:
    try:
        raw = base64.b64decode(enc_str)
        dec = bytes([b ^ XOR_KEY[i % len(XOR_KEY)] for i, b in enumerate(raw)])
        return dec.decode("utf-8", errors="replace")
    except Exception:
        return enc_str


def api_request(path: str, api_key: str, method: str = "GET", data: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    headers = {
        "User-Agent": "curl/7.68.0",
        "Accept": "application/json",
        "X-API-Key": api_key,
    }
    encoded_data = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        encoded_data = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, headers=headers, data=encoded_data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if resp.headers.get("X-Encrypted") == "1":
                body = decrypt_payload(body)
            return json.loads(body)
    except urllib.error.HTTPError as e:
        raw_body = e.read().decode("utf-8", errors="ignore")
        body = decrypt_payload(raw_body)
        try:
            err_json = json.loads(body)
        except Exception:
            err_json = {"error": body, "status": e.code}
        raise RuntimeError(f"API Error ({e.code}): {err_json}") from e


def get_balance(api_key: str) -> float:
    res = api_request("/client/balance", api_key)
    return float(res.get("balance", 0.0))


def get_proxies(api_key: str) -> list[dict]:
    res = api_request("/client/proxies", api_key)
    return res.get("proxies", [])


def test_proxy(proxy_url: str) -> dict:
    result = {"proxy": proxy_url, "ip_ok": False, "ip": None, "gas_ok": False, "comp_ok": False, "detail": ""}
    try:
        s = requests.Session(impersonate="chrome124", trust_env=False, proxies={"http": proxy_url, "https": proxy_url})
        r_ip = s.get("https://api.ipify.org?format=json", timeout=10)
        if r_ip.status_code == 200:
            result["ip_ok"] = True
            result["ip"] = r_ip.json().get("ip")
    except Exception as e:
        result["detail"] = f"IP check failed: {e}"
        return result

    # Check MetaMatcha gas with cookies if cached
    cookie_file = PROJECT_ROOT / ".matcha_cookies.json"
    if cookie_file.exists():
        try:
            with open(cookie_file) as f:
                for c in json.load(f).get("cookies", []):
                    s.cookies.set(c["name"], c["value"], domain=c.get("domain", "meta.matcha.xyz"))
        except Exception:
            pass

    s.headers.update({
        "origin": "https://meta.matcha.xyz",
        "referer": "https://meta.matcha.xyz/solana",
        "sec-fetch-site": "same-origin",
        "accept": "*/*",
    })

    try:
        r_gas = s.get("https://meta.matcha.xyz/api/gas?chainId=1", timeout=10)
        mit = r_gas.headers.get("x-vercel-mitigated", "none")
        if r_gas.status_code == 200 and mit != "deny":
            result["gas_ok"] = True
        else:
            result["detail"] = f"Gas {r_gas.status_code} (mitigated={mit})"
    except Exception as e:
        result["detail"] = f"Gas request error: {e}"

    if result["gas_ok"]:
        try:
            payload = {
                "chainId": 1,
                "isAllowanceHolderFlow": True,
                "gasPrice": "50000000",
                "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
                "sellAmount": "100000000",
                "sellTokenDecimals": 6,
                "buyTokenDecimals": 6,
                "slippageBps": 0,
                "slippagePpm": 0,
                "taker": "0x0000000000000000000000000000000000000001",
            }
            r_comp = s.post("https://meta.matcha.xyz/api/competitions", json=payload, timeout=10)
            mit_comp = r_comp.headers.get("x-vercel-mitigated", "none")
            if r_comp.status_code == 200 and mit_comp != "deny":
                result["comp_ok"] = True
            else:
                result["detail"] = f"Comp {r_comp.status_code} (mitigated={mit_comp})"
        except Exception as e:
            result["detail"] = f"Comp request error: {e}"

    return result


def buy_residential_proxy(api_key: str, days: int = 1) -> dict:
    order_payload = {
        "items": [
            {
                "product_id": VN_RESIDENTIAL_PRODUCT_ID,
                "quantity": 1,
                "duration": days,
                "selected_attributes": {
                    "country": "VN",
                    "protocol": "HTTP",
                },
            }
        ]
    }
    return api_request("/client/orders", api_key, method="POST", data=order_payload)


def update_env_proxy(proxy_url: str) -> None:
    if not ENV_FILE.exists():
        return
    content = ENV_FILE.read_text(encoding="utf-8")
    lines = content.splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith("MATCHA_PROXY="):
            new_lines.append(f"MATCHA_PROXY={proxy_url}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"MATCHA_PROXY={proxy_url}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"[OK] Updated MATCHA_PROXY in {ENV_FILE}")


def main() -> None:
    load_dotenv(ENV_FILE)
    parser = argparse.ArgumentParser(description="ProxyISP API & proxy manager")
    parser.add_argument("--list", action="store_true", help="List all proxies and balance")
    parser.add_argument("--test-all", action="store_true", help="Test all proxies against MetaMatcha")
    parser.add_argument("--buy", type=int, metavar="DAYS", help="Buy 1 VN residential proxy for N days")
    parser.add_argument("--auto-select", action="store_true", help="Find a working proxy and set MATCHA_PROXY in .env")
    args = parser.parse_args()

    api_key = os.getenv("PROXYISP_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] PROXYISP_API_KEY not found in environment or .env")
        sys.exit(1)

    balance = get_balance(api_key)
    print(f"ProxyISP Balance: {balance:,.1f} VND")

    proxies = get_proxies(api_key)
    print(f"Total Proxies: {len(proxies)}")

    proxy_urls = []
    for i, p in enumerate(proxies, 1):
        if p.get("host") and p.get("port"):
            u = p.get("username", "")
            pw = p.get("password", "")
            h = p.get("host")
            port = p.get("port")
            url = f"http://{u}:{pw}@{h}:{port}" if u and pw else f"http://{h}:{port}"
            proxy_urls.append(url)
            print(f"  [{i}] {p.get('name')} | Exp: {p.get('expiresAt')} | {url}")

    if args.test_all or args.auto_select:
        print("\nTesting proxies against MetaMatcha...")
        working = []
        for url in proxy_urls:
            res = test_proxy(url)
            status_str = "WORKING" if (res["gas_ok"] and res["comp_ok"]) else "BLOCKED/FAILED"
            print(f"  {url} -> {status_str} (IP: {res['ip']}, Detail: {res['detail']})")
            if res["gas_ok"] and res["comp_ok"]:
                working.append(url)

        if args.auto_select:
            if working:
                print(f"\n[+] Selected working proxy: {working[0]}")
                update_env_proxy(working[0])
            else:
                print("\n[-] No existing proxies work on MetaMatcha!")
                if args.buy is not None or True:
                    print("[+] Purchasing 1 new VN residential proxy for 1 day...")
                    order = buy_residential_proxy(api_key, days=1)
                    print(f"Order created: {order.get('order_number')}, status={order.get('status')}")

    elif args.buy:
        print(f"\nPurchasing 1 VN residential proxy for {args.buy} day(s)...")
        order = buy_residential_proxy(api_key, days=args.buy)
        print(f"Order result: {json.dumps(order, indent=2)}")


if __name__ == "__main__":
    main()
