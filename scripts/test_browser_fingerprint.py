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

    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=45000)
    for i in range(30):
        time.sleep(1)
        t = (page.title() or "").lower()
        if t and not any(m in t for m in ("checkpoint", "challenge", "just a moment")):
            break
    time.sleep(3)

    # Search loaded scripts inside the page
    res = page.evaluate("""async () => {
        const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
        const matches = [];
        for (const s of scripts) {
            try {
                const text = await (await fetch(s)).text();
                const terms = ['/api/competitions', '/api/quotes', '/api/order', 'competitions', 'quotes?aggregator'];
                for (const t of terms) {
                    if (text.includes(t)) {
                        const idx = text.indexOf(t);
                        matches.append ? null : matches.push({
                            src: s,
                            term: t,
                            snippet: text.substring(Math.max(0, idx - 100), Math.min(text.length, idx + 200))
                        });
                    }
                }
            } catch(e) {}
        }
        return {scriptCount: scripts.length, matches};
    }""")
    print("Script search result:")
    print("Script count:", res.get("scriptCount"))
    for m in res.get("matches", []):
        print(f"\n--- Found {m['term']} in {m['src'].split('/')[-1]} ---")
        print(m['snippet'])

    b.close()
