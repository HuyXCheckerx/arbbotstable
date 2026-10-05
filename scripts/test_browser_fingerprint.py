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
    b = pw.chromium.launch(executable_path=chrome_exe, headless=False, args=["--headless=new", "--no-sandbox"], proxy=p_cfg)
    c = b.new_context(
        user_agent=UA,
        extra_http_headers={
            "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }
    )
    page = c.new_page()
    Stealth().apply_stealth_sync(page)

    captured_comp_requests = []
    def on_req(r):
        if "competitions" in r.url:
            captured_comp_requests.append({
                "url": r.url,
                "method": r.method,
                "headers": dict(r.headers),
                "post_data": r.post_data
            })
    page.on("request", on_req)

    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=45000)
    for _ in range(30):
        time.sleep(1)
        t = (page.title() or "").lower()
        if t and "checkpoint" not in t:
            break
    print("Page title:", page.title())
    time.sleep(4)

    # 1. Check window.fetch
    fetch_info = page.evaluate("""() => {
        return {
            fetchString: window.fetch.toString(),
            isNative: /\[native code\]/.test(window.fetch.toString())
        };
    }""")
    print("Fetch info:", fetch_info)

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

    # 2. Test relative URL: fetch("/api/competitions")
    print("\n--- Testing relative fetch('/api/competitions') ---")
    res_rel = page.evaluate("""async (pl) => {
        try {
            const r = await fetch("/api/competitions", {
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
                headers: Object.fromEntries(r.headers.entries()),
                body: await r.text()
            };
        } catch(e) {
            return {error: e.message || String(e)};
        }
    }""", payload)
    print("Relative fetch status:", res_rel.get("status"))
    print("Relative fetch headers:", json.dumps(res_rel.get("headers"), indent=2))
    print("Relative fetch body:", res_rel.get("body", res_rel.get("error"))[:300])

    # 3. Check what was captured on the wire
    print("\nCaptured wire requests for competitions:")
    for cr in captured_comp_requests:
        print(f"URL: {cr['url']}")
        print(f"Headers: {json.dumps(cr['headers'], indent=2)}")

    b.close()
