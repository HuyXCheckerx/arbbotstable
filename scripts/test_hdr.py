import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
p_url = "http://COlBMQ:bCYTai@14.224.225.135:45376" # Proxy 18
p = urlparse(p_url)
p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}

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

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, proxy=p_cfg, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
    c = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
    
    # Test on matcha.xyz
    page = c.new_page()
    Stealth().apply_stealth_sync(page)
    print("Navigating to https://matcha.xyz/ ...")
    try:
        page.goto("https://matcha.xyz/", wait_until="domcontentloaded", timeout=25000)
    except Exception as e:
        print("Goto matcha.xyz error:", e)
    time.sleep(4)
    print("matcha.xyz title:", page.title())
    
    # Try fetch meta.matcha.xyz/api/competitions from matcha.xyz origin
    res1 = page.evaluate("""async (pl) => {
        try {
            const r = await fetch("https://meta.matcha.xyz/api/competitions", {
                method: "POST",
                headers: {"content-type": "application/json"},
                credentials: "include",
                body: JSON.stringify(pl)
            });
            return {status: r.status, mit: r.headers.get("x-vercel-mitigated"), text: (await r.text()).substring(0, 150)};
        } catch(e) {
            return {error: String(e)};
        }
    }""", payload)
    print("Fetch from matcha.xyz origin:", res1)

    # Try fetch /api/gas
    res_gas = page.evaluate("""async () => {
        try {
            const r = await fetch("https://meta.matcha.xyz/api/gas?chainId=1", {
                credentials: "include"
            });
            return {status: r.status, mit: r.headers.get("x-vercel-mitigated"), text: (await r.text()).substring(0, 150)};
        } catch(e) {
            return {error: String(e)};
        }
    }""")
    print("Fetch gas from matcha.xyz origin:", res_gas)
    
    b.close()
