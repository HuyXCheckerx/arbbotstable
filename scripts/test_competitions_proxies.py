import json
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
test_proxies = [
    ("Proxy 3", "http://PeZCwN:faVnzR@118.70.179.70:23556"),
    ("Proxy 4", "http://PheIKp:ZgyCKN@118.70.187.141:19722"),
    ("Proxy 5", "http://JOqXJK:DgyfPr@118.70.190.208:45093"),
    ("Proxy 8", "http://fteuAB:KqDdtm@118.70.171.77:47888"),
]

payload = {
    "chainId": 1,
    "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
    "sellAmount": "100000000",
    "sellTokenDecimals": 6,
    "buyTokenDecimals": 6,
    "slippageBps": 50,
    "taker": "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
}

for name, p_url in test_proxies:
    print(f"Testing {name} ({p_url})...")
    p = urlparse(p_url)
    p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True, proxy=p_cfg, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            c = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
            page = c.new_page()
            Stealth().apply_stealth_sync(page)
            page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)
            res = page.evaluate("""async (pl) => {
                const r = await fetch('https://meta.matcha.xyz/api/competitions', {
                    method: 'POST',
                    headers: {'content-type': 'application/json', 'x-fetch-native': '1'},
                    credentials: 'include',
                    body: JSON.stringify(pl)
                });
                return {status: r.status, mit: r.headers.get('x-vercel-mitigated')};
            }""", payload)
            print(f"  Result: status={res['status']}, mit={res.get('mit')}")
            b.close()
    except Exception as e:
        print(f"  Error: {e}")
