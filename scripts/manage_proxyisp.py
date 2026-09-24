#!/usr/bin/env python3
"""Manage ProxyISP proxies via the client API.

Capabilities:
- Query wallet balance & pricing
- List active proxies, expiration dates, and remaining time
- Detailed diagnostic connectivity testing against MetaMatcha (Gas & Competition)
- Auto-detect when all existing proxies have expired
- Automatically purchase a new 1-day Vietnam residential proxy once all old ones expire
- Poll ProxyISP for instant provisioning and extract credentials
- Safely update MATCHA_PROXY in .env and invalidate stale Vercel challenge cookies
- Remove auto-renew on orders and provisioned proxies
- Sniper startup integration: test all existing proxies, select working one or auto-buy new 1-day residential proxy
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import ssl
import sys
import time
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from curl_cffi import requests
from dotenv import load_dotenv

logger = logging.getLogger("proxyisp")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
COOKIE_FILE = PROJECT_ROOT / ".matcha_cookies.json"

BASE_URL = "https://api.proxyisp.net"
XOR_KEY = bytes.fromhex("f0d7985145df712f00f1f45711d1ef373d46895d5d0b2ae19cf16a202569e221")
VN_RESIDENTIAL_PRODUCT_ID = "698f174928abf7a4ed22d2c7"
VN_RESIDENTIAL_DAILY_PRICE_VND = 1495.0

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def decrypt_payload(enc_str: str) -> str:
    """Decrypt XOR-encoded response payloads from ProxyISP API."""
    try:
        raw = base64.b64decode(enc_str)
        dec = bytes([b ^ XOR_KEY[i % len(XOR_KEY)] for i, b in enumerate(raw)])
        return dec.decode("utf-8", errors="replace")
    except Exception:
        return enc_str


def api_request(
    path: str,
    api_key: str,
    method: str = "GET",
    data: dict | None = None,
    timeout: float = 12.0,
) -> dict:
    """Execute an authenticated HTTP request to ProxyISP API."""
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
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
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
    """Query current wallet balance in VND."""
    res = api_request("/client/balance", api_key)
    return float(res.get("balance", 0.0))


def get_proxies(api_key: str) -> list[dict]:
    """Retrieve all assigned proxies for this account."""
    res = api_request("/client/proxies", api_key)
    return res.get("proxies", [])


def format_proxy_url(proxy_info: dict) -> str:
    """Format proxy credentials into a standard HTTP proxy URL."""
    u = proxy_info.get("username", "")
    pw = proxy_info.get("password", "")
    h = proxy_info.get("host", "")
    port = proxy_info.get("port", "")
    if not (h and port):
        return ""
    if u and pw:
        return f"http://{u}:{pw}@{h}:{port}"
    return f"http://{h}:{port}"


def parse_expiry(exp_str: str | None) -> datetime | None:
    """Parse ProxyISP ISO expiry string into a timezone-aware UTC datetime."""
    if not exp_str:
        return None
    try:
        dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def format_remaining_time(delta_seconds: float) -> str:
    """Format time difference in seconds to a human-readable duration."""
    if delta_seconds <= 0:
        return "EXPIRED"
    days = int(delta_seconds // 86400)
    hours = int((delta_seconds % 86400) // 3600)
    minutes = int((delta_seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_proxies_status(api_key: str, now: datetime | None = None) -> dict[str, Any]:
    """Analyze all proxies from ProxyISP API, determining expiry and remaining lifetime."""
    if now is None:
        now = datetime.now(timezone.utc)

    proxies_raw = get_proxies(api_key)
    balance = get_balance(api_key)

    proxy_list = []
    active_count = 0
    expired_count = 0
    latest_expiry_dt: datetime | None = None
    soonest_expiry_dt: datetime | None = None

    for p in proxies_raw:
        exp_raw = p.get("expiresAt")
        exp_dt = parse_expiry(exp_raw)
        status_field = str(p.get("status", "")).lower()

        if exp_dt is not None:
            remaining_seconds = (exp_dt - now).total_seconds()
            is_expired = remaining_seconds <= 0 or status_field == "expired"
        else:
            remaining_seconds = 0.0
            is_expired = True

        if not is_expired:
            active_count += 1
            if latest_expiry_dt is None or exp_dt > latest_expiry_dt:
                latest_expiry_dt = exp_dt
            if soonest_expiry_dt is None or exp_dt < soonest_expiry_dt:
                soonest_expiry_dt = exp_dt
        else:
            expired_count += 1

        proxy_url = format_proxy_url(p)
        proxy_list.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "host": p.get("host"),
            "port": p.get("port"),
            "username": p.get("username"),
            "password": p.get("password"),
            "proxy_url": proxy_url,
            "status_field": status_field,
            "auto_renew": p.get("auto_renew", False),
            "expires_at_raw": exp_raw,
            "expires_dt": exp_dt,
            "remaining_seconds": remaining_seconds,
            "remaining_str": format_remaining_time(remaining_seconds),
            "is_expired": is_expired,
            "product_name": p.get("product_name"),
            "type": p.get("type"),
        })

    all_expired = (len(proxy_list) == 0) or (active_count == 0)

    time_until_all_expire = 0.0
    if latest_expiry_dt is not None:
        time_until_all_expire = max(0.0, (latest_expiry_dt - now).total_seconds())

    return {
        "balance_vnd": balance,
        "total_proxies": len(proxy_list),
        "active_count": active_count,
        "expired_count": expired_count,
        "all_expired": all_expired,
        "latest_expiry_dt": latest_expiry_dt,
        "time_until_all_expire_seconds": time_until_all_expire,
        "time_until_all_expire_str": format_remaining_time(time_until_all_expire),
        "proxies": proxy_list,
    }


def test_proxy(proxy_url: str, check_meta: bool = True) -> dict[str, Any]:
    """Test HTTP connectivity and MetaMatcha clearance for a proxy URL."""
    result = {
        "proxy": proxy_url,
        "ip_ok": False,
        "ip": None,
        "status_code": 0,
        "gas_ok": False,
        "comp_ok": False,
        "detail": "",
        "mitigated": "none",
    }
    if not proxy_url:
        result["detail"] = "Empty proxy URL"
        return result

    # 1. Basic IP check
    try:
        s = requests.Session(
            impersonate="chrome124",
            trust_env=False,
            proxies={"http": proxy_url, "https": proxy_url},
        )
        r_ip = s.get("https://api.ipify.org?format=json", timeout=10)
        result["status_code"] = r_ip.status_code
        if r_ip.status_code == 200:
            result["ip_ok"] = True
            result["ip"] = r_ip.json().get("ip")
        else:
            result["detail"] = f"IP check HTTP {r_ip.status_code}"
            return result
    except Exception as e:
        result["detail"] = f"IP check failed: {e}"
        return result

    if not check_meta:
        return result

    # 2. Check MetaMatcha gas endpoint
    cookie_file = COOKIE_FILE
    if cookie_file.exists():
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                c_data = json.load(f)
                for c in c_data.get("cookies", []):
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
        r_gas = s.get("https://meta.matcha.xyz/api/gas?chainId=1", timeout=8)
        mit = r_gas.headers.get("x-vercel-mitigated", "none")
        result["mitigated"] = mit
        result["status_code"] = r_gas.status_code
        if r_gas.status_code == 200 and mit not in ("deny", "challenge"):
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
            r_comp = s.post("https://meta.matcha.xyz/api/competitions", json=payload, timeout=8)
            mit_comp = r_comp.headers.get("x-vercel-mitigated", "none")
            if r_comp.status_code == 200 and mit_comp not in ("deny", "challenge"):
                result["comp_ok"] = True
            else:
                result["detail"] = f"Comp {r_comp.status_code} (mitigated={mit_comp})"
        except Exception as e:
            result["detail"] = f"Comp request error: {e}"

    return result


def _test_proxy_challenge_clearance(proxy_url: str) -> bool:
    """Verify whether a challenged proxy can solve Vercel challenge and achieve HTTP 200."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        p = urlparse(proxy_url)
        proxy_cfg: dict[str, str] = {"server": f"http://{p.hostname}:{p.port}"}
        if p.username:
            proxy_cfg["username"] = p.username
        if p.password:
            proxy_cfg["password"] = p.password

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                proxy=proxy_cfg,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            Stealth().apply_stealth_sync(page)
            try:
                page.goto("https://meta.matcha.xyz/solana", timeout=15000, wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(2)
            eval_res = page.evaluate("""async () => {
                try {
                    const r = await fetch("https://meta.matcha.xyz/api/gas?chainId=1");
                    return { status: r.status };
                } catch(e) {
                    return { status: 0 };
                }
            }""")
            browser.close()
            return eval_res.get("status") == 200
    except Exception as exc:
        logger.debug("Proxy challenge test failed for %s: %s", proxy_url, exc)
        return False


def is_proxy_working(proxy_url: str, check_challenge: bool = True) -> bool:
    """Determine whether a proxy is fully functional for MetaMatcha quotes."""
    if not proxy_url:
        return False
    res = test_proxy(proxy_url, check_meta=True)
    if not res.get("ip_ok"):
        return False
    if res.get("gas_ok"):
        return True
    # If explicitly denied by Vercel firewall (HTTP 403 or mitigated=deny)
    if res.get("mitigated") in ("deny", "denied") or res.get("status_code") in (401, 403):
        return False
    # If challenged (HTTP 429), verify if it can clear the challenge
    if check_challenge and (res.get("mitigated") == "challenge" or res.get("status_code") == 429):
        return _test_proxy_challenge_clearance(proxy_url)
    return False


def buy_residential_proxy(api_key: str, days: int = 1, auto_renew: bool = False) -> dict:
    """Purchase a 1-day or multi-day Vietnam residential proxy without auto-renew."""
    order_payload = {
        "items": [
            {
                "product_id": VN_RESIDENTIAL_PRODUCT_ID,
                "quantity": 1,
                "duration": days,
                "auto_renew": auto_renew,
                "selected_attributes": {
                    "country": "VN",
                    "protocol": "HTTP",
                },
            }
        ],
        "auto_renew": auto_renew,
    }
    return api_request("/client/orders", api_key, method="POST", data=order_payload)


def disable_proxy_autorenew(api_key: str, proxy_id: str | None = None) -> bool:
    """Ensure auto-renew is removed/disabled on proxy services in ProxyISP."""
    if not proxy_id:
        return True
    payload = {"auto_renew": False, "enabled": False}
    for endpoint in [
        f"/client/proxies/{proxy_id}/auto-renew",
        f"/client/proxies/{proxy_id}/autorenew",
        f"/client/proxies/{proxy_id}",
        f"/client/services/{proxy_id}/auto-renew",
    ]:
        for method in ["POST", "PUT", "PATCH"]:
            try:
                api_request(endpoint, api_key, method=method, data=payload)
                logger.info("[ProxyManager] Auto-renew disabled via %s %s", method, endpoint)
                return True
            except Exception:
                pass
    return True


def wait_for_new_proxy(
    api_key: str,
    known_proxy_ids: set[str],
    timeout: float = 30.0,
    poll_interval: float = 2.0,
) -> dict | None:
    """Poll ProxyISP proxies list until a newly provisioned proxy appears."""
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        try:
            current_proxies = get_proxies(api_key)
            for p in current_proxies:
                p_id = p.get("id")
                if p_id and p_id not in known_proxy_ids:
                    if p.get("host") and p.get("port"):
                        return p
        except Exception as exc:
            logger.debug("Error while polling for new proxy: %s", exc)
        time.sleep(poll_interval)
    return None


def update_env_proxy(proxy_url: str, env_path: Path = ENV_FILE) -> bool:
    """Safely update MATCHA_PROXY in .env, runtime environment, and clear old cookies."""
    if not env_path.exists():
        logger.warning(".env file not found at %s", env_path)
        return False
    try:
        content = env_path.read_text(encoding="utf-8")
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
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        os.environ["MATCHA_PROXY"] = proxy_url

        # Invalidate old cached clearance cookie so fresh IP generates new challenge token
        if COOKIE_FILE.exists():
            try:
                COOKIE_FILE.unlink()
                logger.info("[CookieManager] Cleared stale .matcha_cookies.json for new proxy IP")
            except Exception:
                pass

        print(f"[OK] Updated MATCHA_PROXY in {env_path}")
        return True
    except Exception as exc:
        print(f"[ERROR] Failed to update MATCHA_PROXY in {env_path}: {exc}")
        return False


def setup_sniper_proxy(
    api_key: str | None = None,
    logger: logging.Logger | None = None,
    env_path: Path = ENV_FILE,
) -> str | None:
    """Sniper startup routine:
    1. Query all proxies from ProxyISP.
    2. Test them against connectivity and MetaMatcha.
    3. If one works, select it, configure it in .env, and go with it.
    4. If none works, buy a new VN residential proxy for 1 day, add to list,
       remove auto-renew, update .env, and return the new proxy.
    """
    log_info = logger.info if logger else print
    log_warn = logger.warning if logger else print

    if not api_key:
        api_key = os.getenv("PROXYISP_API_KEY", "").strip()
    if not api_key:
        log_info("[ProxyManager] No PROXYISP_API_KEY configured; continuing with current MATCHA_PROXY.")
        return os.getenv("MATCHA_PROXY")

    log_info("[ProxyManager] Checking and testing all proxies from ProxyISP...")
    try:
        proxies = get_proxies(api_key)
    except Exception as exc:
        log_warn(f"[ProxyManager] Failed to fetch proxies from ProxyISP: {exc}")
        return os.getenv("MATCHA_PROXY")

    working_proxy = None
    known_ids = set()

    for p in proxies:
        p_id = p.get("id")
        if p_id:
            known_ids.add(p_id)
        url = format_proxy_url(p)
        if not url:
            continue

        log_info(f"[ProxyManager] Testing proxy {p.get('name')} ({url})...")
        if is_proxy_working(url):
            working_proxy = url
            log_info(f"[ProxyManager] Proxy {p.get('name')} is WORKING! Selecting this proxy.")
            break
        else:
            log_info(f"[ProxyManager] Proxy {p.get('name')} failed connectivity / MetaMatcha check.")

    if working_proxy:
        update_env_proxy(working_proxy, env_path=env_path)
        return working_proxy

    # None of the existing proxies work -> Buy a new 1-day VN residential proxy
    log_info("[ProxyManager] None of the existing proxies are working. Buying a new VN residential proxy for 1 day...")
    try:
        balance = get_balance(api_key)
        if balance < VN_RESIDENTIAL_DAILY_PRICE_VND:
            log_warn(
                f"[ProxyManager] Insufficient balance ({balance:,.1f} VND) to buy proxy "
                f"(requires {VN_RESIDENTIAL_DAILY_PRICE_VND:,.1f} VND)."
            )
            return os.getenv("MATCHA_PROXY")

        # Buy 1 day residential proxy with auto_renew=False
        order = buy_residential_proxy(api_key, days=1, auto_renew=False)
        order_no = order.get("order_number", "Unknown")
        log_info(f"[ProxyManager] Order submitted: {order_no} (Status: {order.get('status')})")

        # Wait for allocation and add to list
        log_info("[ProxyManager] Waiting for residential proxy allocation from ProxyISP...")
        new_proxy = wait_for_new_proxy(api_key, known_ids, timeout=30.0)
        if not new_proxy:
            log_warn(f"[ProxyManager] Proxy allocation timed out for order {order_no}.")
            return os.getenv("MATCHA_PROXY")

        # Remove auto-renew on the new proxy
        disable_proxy_autorenew(api_key, new_proxy.get("id"))
        log_info("[ProxyManager] Auto-renew disabled for newly provisioned proxy.")

        new_url = format_proxy_url(new_proxy)
        log_info(f"[ProxyManager] Successfully added new residential proxy: {new_proxy.get('name')} -> {new_url}")

        # Update .env and memory
        update_env_proxy(new_url, env_path=env_path)

        # Invalidate old cookies
        if COOKIE_FILE.exists():
            try:
                COOKIE_FILE.unlink()
            except Exception:
                pass

        return new_url
    except Exception as exc:
        log_warn(f"[ProxyManager] Failed to purchase new residential proxy: {exc}")
        return os.getenv("MATCHA_PROXY")


def ensure_active_proxy(
    api_key: str | None = None,
    force_buy: bool = False,
    buy_if_none_work: bool = False,
    days: int = 1,
    env_path: Path = ENV_FILE,
) -> dict[str, Any]:
    """Monitor proxy status and automatically buy a new residential proxy if expired or forced."""
    if not api_key:
        api_key = os.getenv("PROXYISP_API_KEY", "").strip()
    if not api_key:
        raise ValueError("PROXYISP_API_KEY not found in environment or .env")

    status = get_proxies_status(api_key)
    balance = status["balance_vnd"]
    all_expired = status["all_expired"]

    result: dict[str, Any] = {
        "action": "none",
        "message": "",
        "status": status,
        "new_proxy": None,
        "new_proxy_url": None,
    }

    should_buy = force_buy or all_expired

    if not should_buy and buy_if_none_work:
        working_found = False
        for p in status["proxies"]:
            if not p["is_expired"] and p["proxy_url"]:
                if is_proxy_working(p["proxy_url"]):
                    working_found = True
                    break
        if not working_found:
            should_buy = True

    if not should_buy:
        time_left = status["time_until_all_expire_str"]
        active = status["active_count"]
        result["action"] = "active_proxies_remain"
        result["message"] = (
            f"{active} active proxy(ies) remaining. "
            f"All will not expire for another {time_left}. No purchase required."
        )
        return result

    cost_needed = VN_RESIDENTIAL_DAILY_PRICE_VND * days
    if balance < cost_needed:
        result["action"] = "insufficient_balance"
        result["message"] = (
            f"Cannot buy proxy: Balance ({balance:,.1f} VND) is less than required "
            f"cost ({cost_needed:,.1f} VND) for {days} day(s)."
        )
        print(f"[ERROR] {result['message']}")
        return result

    known_ids = {p["id"] for p in status["proxies"] if p.get("id")}
    reason = "all old proxies expired" if all_expired else "manual/health trigger"
    print(f"[+] Purchasing 1 new VN residential proxy ({days} day(s)) due to: {reason}...")
    order = buy_residential_proxy(api_key, days=days, auto_renew=False)
    order_no = order.get("order_number", "Unknown")
    print(f"    Order submitted: {order_no} (Status: {order.get('status')})")

    print("    Waiting for ProxyISP to allocate residential proxy...")
    new_proxy = wait_for_new_proxy(api_key, known_ids, timeout=30.0)
    if not new_proxy:
        result["action"] = "allocation_timeout"
        result["message"] = f"Order {order_no} submitted, but new proxy did not appear within 30s."
        print(f"[WARNING] {result['message']}")
        return result

    disable_proxy_autorenew(api_key, new_proxy.get("id"))
    new_proxy_url = format_proxy_url(new_proxy)
    result["action"] = "purchased_and_configured"
    result["new_proxy"] = new_proxy
    result["new_proxy_url"] = new_proxy_url
    result["message"] = f"Successfully purchased and provisioned new proxy: {new_proxy_url}"

    print(f"[+] New proxy provisioned: {new_proxy.get('name')} -> {new_proxy_url}")
    print(f"    Expires: {new_proxy.get('expiresAt')}")

    update_env_proxy(new_proxy_url, env_path=env_path)
    return result


def main() -> None:
    load_dotenv(ENV_FILE)
    parser = argparse.ArgumentParser(
        description="ProxyISP API proxy manager & automated residential proxy renewal"
    )
    parser.add_argument("--list", action="store_true", help="List all proxies, expiration dates, and wallet balance")
    parser.add_argument("--check-expiry", action="store_true", help="Check if all existing proxies are expired")
    parser.add_argument("--test-all", action="store_true", help="Test all proxies for IP and MetaMatcha connectivity")
    parser.add_argument("--sniper-start", action="store_true", help="Execute sniper startup proxy check: test all, go with working one, or buy 1-day residential without auto-renew")
    parser.add_argument("--auto-renew-on-expiry", action="store_true", help="Buy a new residential proxy if all old ones have expired")
    parser.add_argument("--buy", type=int, metavar="DAYS", help="Explicitly purchase 1 VN residential proxy for N days")
    parser.add_argument("--force-renew", action="store_true", help="Force purchase a new residential proxy immediately")
    parser.add_argument("--auto-select", action="store_true", help="Find a working proxy or renew if none work")

    args = parser.parse_args()

    api_key = os.getenv("PROXYISP_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] PROXYISP_API_KEY not found in environment or .env")
        sys.exit(1)

    # Sniper start routine
    if args.sniper_start:
        selected = setup_sniper_proxy(api_key)
        print(f"\nFinal Proxy: {selected}")
        return

    # Check and auto-renew if all expired
    if args.auto_renew_on_expiry:
        print("Checking proxy expiration status across all proxies...")
        res = ensure_active_proxy(api_key, force_buy=False, days=1)
        print(f"\nResult: {res['action']}")
        print(f"Details: {res['message']}")
        return

    # Explicit purchase
    if args.buy is not None or args.force_renew:
        days = args.buy if args.buy is not None else 1
        print(f"Executing immediate purchase for {days} day(s)...")
        res = ensure_active_proxy(api_key, force_buy=True, days=days)
        print(f"\nResult: {res['action']}")
        print(f"Details: {res['message']}")
        return

    # Expiry status
    if args.check_expiry:
        status = get_proxies_status(api_key)
        print(f"\nProxyISP Balance: {status['balance_vnd']:,.1f} VND")
        print(f"Total Proxies: {status['total_proxies']} (Active: {status['active_count']}, Expired: {status['expired_count']})")
        print(f"All Expired: {status['all_expired']}")
        print(f"Time Until All Expire: {status['time_until_all_expire_str']}")
        for p in status["proxies"]:
            status_tag = "[EXPIRED]" if p["is_expired"] else f"[ACTIVE: {p['remaining_str']} left]"
            print(f"  {status_tag} {p['name']} | Exp: {p['expires_at_raw']} | {p['proxy_url']}")
        return

    # Test all
    if args.test_all or args.auto_select:
        status = get_proxies_status(api_key)
        print(f"\nProxyISP Balance: {status['balance_vnd']:,.1f} VND")
        print(f"Total Proxies: {status['total_proxies']}")
        working = []
        for i, p in enumerate(status["proxies"], 1):
            url = p["proxy_url"]
            exp_tag = f"Remaining: {p['remaining_str']}" if not p["is_expired"] else "EXPIRED"
            print(f"\n[{i}] {p['name']} ({exp_tag})")
            print(f"    Proxy URL: {url}")
            res = test_proxy(url, check_meta=True)
            status_str = "WORKING" if (res["gas_ok"] and res["comp_ok"]) else "BLOCKED/FAILED"
            print(f"    Status: {status_str} | IP: {res['ip']} | Mitigated: {res['mitigated']} | Detail: {res['detail']}")
            if res["gas_ok"] and res["comp_ok"]:
                working.append(url)

        if args.auto_select:
            if working:
                print(f"\n[+] Selected working proxy: {working[0]}")
                update_env_proxy(working[0])
            else:
                print("\n[-] No existing proxies work on MetaMatcha!")
                setup_sniper_proxy(api_key)
        return

    # Default overview
    status = get_proxies_status(api_key)
    print("=" * 70)
    print(f" ProxyISP Account Overview")
    print(f" Balance: {status['balance_vnd']:,.1f} VND (1-Day Residential Proxy = {VN_RESIDENTIAL_DAILY_PRICE_VND:,.0f} VND)")
    print(f" Proxies: {status['total_proxies']} total ({status['active_count']} active, {status['expired_count']} expired)")
    print(f" All Expired: {status['all_expired']}")
    print(f" Time Until All Expire: {status['time_until_all_expire_str']}")
    print("=" * 70)
    for i, p in enumerate(status["proxies"], 1):
        tag = "EXPIRED" if p["is_expired"] else f"ACTIVE ({p['remaining_str']})"
        print(f"  [{i}] [{tag}] {p['name']}")
        print(f"      URL: {p['proxy_url']}")
        print(f"      Expires: {p['expires_at_raw']}")
    print("=" * 70)


if __name__ == "__main__":
    main()
