import time
import json
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
p_url = "http://AurhPd:FxitOe@14.224.198.119:49797"
p = urlparse(p_url)
p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, proxy=p_cfg, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    c = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    page = c.new_page()
    Stealth().apply_stealth_sync(page)
    
    # Intercept all network requests to see what headers/cookies are sent
    reqs = []
    def on_request(request):
        if "meta.matcha.xyz" in request.url:
            reqs.append({
                "url": request.url,
                "method": request.method,
                "headers": request.headers
            })
    page.on("request", on_request)
    
    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=25000)
    for _ in range(10):
        time.sleep(1)
        if "checkpoint" not in (page.title() or "").lower():
            break
    time.sleep(2)
    print("Page title:", page.title())
    print("Cookies:", [{"name": ck["name"], "domain": ck["domain"]} for ck in c.cookies()])
    
    # Check captured requests to meta.matcha.xyz
    print(f"\nCaptured {len(reqs)} requests to meta.matcha.xyz:")
    for r in reqs[-5:]:
        print(f"  {r['method']} {r['url']}")
        # print interesting headers
        for h, v in r['headers'].items():
            if any(k in h.lower() for k in ("sec-", "x-", "cookie", "origin", "referer", "user-agent")):
                print(f"    {h}: {v}")

    # Now let's try fetch /api/gas?chainId=1 and log request/response
    print("\nEvaluating fetch(/api/gas?chainId=1)...")
    res_gas = page.evaluate("""async () => {
        const r = await fetch("https://meta.matcha.xyz/api/gas?chainId=1", {credentials: "include"});
        return {
            status: r.status,
            statusText: r.statusText,
            headers: Object.fromEntries(r.headers.entries()),
            text: await r.text()
        };
    }""")
    print("GAS STATUS:", res_gas["status"], res_gas["statusText"])
    print("GAS HEADERS:", json.dumps(res_gas["headers"], indent=2))
    print("GAS BODY:", res_gas["text"][:200])

    # Now let's try fetch /api/competitions
    print("\nEvaluating fetch(/api/competitions)...")
    payload = {
        "chainId": 1,
        "sellTokenAddress": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "buyTokenAddress": "0x6c3ea9036406852006290770bedfcaba0e23a0e8",
        "sellAmount": "100000000",
        "sellTokenDecimals": 6,
        "buyTokenDecimals": 6,
        "slippageBps": 50,
        "gasPrice": res_gas["text"] and json.loads(res_gas["text"]).get("price") or "2000000000",
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
