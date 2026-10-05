import json
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
p_url = "http://AurhPd:FxitOe@14.224.198.119:49797"
p = urlparse(p_url)
p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}

with sync_playwright() as pw:
    chrome_exe = pw.chromium.executable_path
    print("Using executable:", chrome_exe)
    
    # Launch real chrome with --headless=new
    b = pw.chromium.launch(
        executable_path=chrome_exe,
        headless=False,
        args=[
            "--headless=new",
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
        proxy=p_cfg
    )
    c = b.new_context(
        user_agent=UA,
        viewport={"width": 1280, "height": 800},
        extra_http_headers={
            "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }
    )
    page = c.new_page()
    Stealth().apply_stealth_sync(page)

    reqs = []
    def on_req(r):
        if "meta.matcha.xyz" in r.url:
            reqs.append((r.method, r.url, r.headers.get("sec-ch-ua"), r.headers.get("user-agent")))
    page.on("request", on_req)

    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=30000)
    for _ in range(10):
        time.sleep(1)
        if "checkpoint" not in (page.title() or "").lower():
            break
    time.sleep(2)
    print("Title:", page.title())
    print("Captured requests sample:", reqs[:3])

    # Test /api/gas
    res_gas = page.evaluate("""async () => {
        const r = await fetch("https://meta.matcha.xyz/api/gas?chainId=1", {credentials: "include"});
        return {status: r.status, text: await r.text()};
    }""")
    print("GAS:", res_gas)

    # Test /api/competitions
    payload = {
        "chainId": 1,
        "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
        "sellAmount": "100000000",
        "sellTokenDecimals": 6,
        "buyTokenDecimals": 6,
        "slippageBps": 50,
        "gasPrice": "2000000000",
        "taker": "0x50da32e628b45abb1335924086ca0013b9d4ec1c"
    }
    res_comp = page.evaluate("""async (pl) => {
        const r = await fetch("https://meta.matcha.xyz/api/competitions", {
            method: "POST",
            headers: {
                "content-type": "application/json",
                "x-taker": pl.taker
            },
            credentials: "include",
            body: JSON.stringify(pl)
        });
        return {
            status: r.status,
            statusText: r.statusText,
            headers: Object.fromEntries(r.headers.entries()),
            text: await r.text()
        };
    }""", payload)
    print("COMP STATUS:", res_comp["status"], res_comp["statusText"])
    print("COMP HEADERS:", json.dumps(res_comp["headers"], indent=2))
    print("COMP BODY:", res_comp["text"][:300])

    b.close()
