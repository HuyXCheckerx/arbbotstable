import json
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

PROXIES = [
    ("ORD-0001 (118.70.171.107)", "http://cQDBka:wWqPkm@118.70.171.107:45491"),
    ("ORD-0041 (118.70.179.70)", "http://PeZCwN:faVnzR@118.70.179.70:23556"),
    ("ORD-0014 (14.224.225.135)", "http://COlBMQ:bCYTai@14.224.225.135:45376"),
]

UA_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

with sync_playwright() as pw:
    for name, p_url in PROXIES:
        print(f"\n================ Testing {name} ================")
        p = urlparse(p_url)
        p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}
        try:
            b = pw.chromium.launch(
                executable_path=pw.chromium.executable_path,
                headless=False,
                args=["--headless=new", "--no-sandbox", "--disable-blink-features=AutomationControlled"],
                proxy=p_cfg
            )
            c = b.new_context(
                user_agent=UA_WIN,
                extra_http_headers={
                    "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                }
            )
            page = c.new_page()
            Stealth().apply_stealth_sync(page)

            page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=30000)
            for _ in range(25):
                time.sleep(1)
                t = (page.title() or "").lower()
                if t and "checkpoint" not in t:
                    break
            print(f"[{name}] Title: {page.title()}")

            payload = {
                "chainId": 1,
                "isAllowanceHolderFlow": True,
                "gasPrice": "2000000000",
                "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "sellTokenDecimals": 6,
                "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
                "buyTokenDecimals": 6,
                "sellAmount": "100000000",
                "slippageBps": 50,
                "slippagePpm": 5000,
                "taker": "0x50da32e628b45abb1335924086ca0013b9d4ec1c"
            }

            res = page.evaluate("""async (pl) => {
                try {
                    const r = await fetch("/api/competitions", {
                        method: "POST",
                        headers: { "Content-Type": "application/json", "x-fetch-native": "1", "x-taker": pl.taker },
                        credentials: "include",
                        body: JSON.stringify(pl)
                    });
                    return { status: r.status, text: await r.text() };
                } catch(e) { return { error: String(e) }; }
            }""", payload)
            print(f"[{name}] Status: {res.get('status')}, Body: {res.get('text', res.get('error'))[:200]}")
            b.close()
        except Exception as e:
            print(f"[{name}] Failed with exception: {e}")
