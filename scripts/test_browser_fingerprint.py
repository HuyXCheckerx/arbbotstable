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

    api_events = []
    def on_request(r):
        if "/api/" in r.url:
            api_events.append({
                "type": "REQUEST",
                "method": r.method,
                "url": r.url,
                "headers": dict(r.headers),
                "post_data": r.post_data
            })
    def on_response(r):
        if "/api/" in r.url:
            try:
                body = r.text()
            except Exception as e:
                body = f"<error reading body: {e}>"
            api_events.append({
                "type": "RESPONSE",
                "status": r.status,
                "url": r.url,
                "headers": dict(r.headers),
                "body": body[:500]
            })
    page.on("request", on_request)
    page.on("response", on_response)

    print("Navigating to https://meta.matcha.xyz/ethereum...")
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="networkidle", timeout=45000)
    print("Title:", page.title())
    time.sleep(3)

    print("\n--- Initial API Events ---")
    for ev in api_events:
        print(f"[{ev['type']}] {ev.get('method', '')} {ev.get('status', '')} {ev['url']}")
        if ev['type'] == 'REQUEST' and ev.get('post_data'):
            print(f"  Payload: {ev['post_data'][:200]}")
        if ev['type'] == 'RESPONSE':
            print(f"  Body: {ev['body'][:200]}")

    # Now inspect input fields on the page
    inputs = page.locator("input").all()
    print(f"\nFound {len(inputs)} input fields on page")
    for i, inp in enumerate(inputs):
        try:
            print(f"Input {i}: placeholder={inp.get_attribute('placeholder')}, aria-label={inp.get_attribute('aria-label')}, name={inp.get_attribute('name')}")
        except Exception:
            pass

    # Try typing '100' into the first number input or amount input
    api_events.clear()
    for inp in inputs:
        ph = (inp.get_attribute("placeholder") or "").lower()
        if "0" in ph or "amount" in ph or inp.get_attribute("inputmode") == "decimal":
            print(f"Typing into input (placeholder={ph})...")
            inp.click()
            inp.fill("100")
            break

    print("Waiting 5 seconds for UI quote requests...")
    time.sleep(5)

    print(f"\n--- API Events after typing amount ({len(api_events)} events) ---")
    for ev in api_events:
        print(f"[{ev['type']}] {ev.get('method', '')} {ev.get('status', '')} {ev['url']}")
        if ev['type'] == 'REQUEST':
            print(f"  Headers: {json.dumps({k: v for k, v in ev['headers'].items() if any(x in k.lower() for x in ('x-', 'sec-', 'content-type', 'cookie', 'origin', 'referer'))}, indent=2)}")
            if ev.get('post_data'):
                print(f"  Payload: {ev['post_data']}")
        if ev['type'] == 'RESPONSE':
            print(f"  Status: {ev['status']}, Mitigated: {ev['headers'].get('x-vercel-mitigated')}")
            print(f"  Body: {ev['body'][:300]}")

    b.close()
