import json
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )
    page = context.new_page()

    provider_token_res = []
    quotes_res = []

    def on_res(r):
        if "get-provider-token" in r.url:
            try:
                provider_token_res.append(r.json())
            except Exception:
                pass
        if "competitions" in r.url or "quote" in r.url or "swap" in r.url:
            print(f"-> {r.status} {r.request.method} {r.url}")

    page.on("response", on_res)

    print("Opening https://matcha.xyz/tokens/ethereum/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48...")
    page.goto("https://matcha.xyz/tokens/ethereum/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", wait_until="networkidle")
    time.sleep(3)

    print("Provider token response:", provider_token_res)

    cookies = context.cookies()
    print("Matcha.xyz cookies:", [(c['name'], c['domain']) for c in cookies])

    browser.close()
