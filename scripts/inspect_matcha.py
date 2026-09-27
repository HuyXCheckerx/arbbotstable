import time
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )
    page = context.new_page()

    network = []
    def on_req(r):
        if any(k in r.url for k in ("meta.matcha", "matcha.xyz/api", "quote", "competition", "trade", "price")):
            network.append({
                "method": r.method,
                "url": r.url,
                "headers": dict(r.headers),
            })
    page.on("request", on_req)
    page.on("response", lambda r: print(f"RES {r.status} {r.url[:70]}"))

    print("Navigating to https://matcha.xyz/tokens/ethereum/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48...")
    page.goto("https://matcha.xyz", wait_until="domcontentloaded")
    time.sleep(10)

    print("\nCaptured requests:")
    for n in network:
        print(n["method"], n["url"])
        for h, v in n["headers"].items():
            if any(x in h.lower() for x in ("auth", "matcha", "0x", "key", "token", "origin", "referer")):
                print(f"   {h}: {v}")

    browser.close()
