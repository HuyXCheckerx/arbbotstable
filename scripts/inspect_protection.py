import re
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    Stealth().apply_stealth_sync(page)

    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    url1 = "https://meta.matcha.xyz/_next/static/immutable/chunks/3lodfiiq4tor_.js"
    code1 = page.evaluate(f"fetch('{url1}').then(r => r.text())")
    print(f"=== {url1} length: {len(code1)} ===")
    for m in re.finditer(r'.{0,300}protect:[^}]+}.{0,300}', code1):
        print("PROTECT CHUNK:", m.group(0))

    url2 = "https://meta.matcha.xyz/_next/static/immutable/chunks/2h0_3uuys7-lh.js"
    code2 = page.evaluate(f"fetch('{url2}').then(r => r.text())")
    print(f"\n=== {url2} length: {len(code2)} ===")
    for m in re.finditer(r'.{0,300}/api/competitions.{0,300}', code2):
        print("COMP CHUNK:", m.group(0))

    browser.close()
