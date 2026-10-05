import json
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

UA_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
p_url = "http://AurhPd:FxitOe@14.224.198.119:49797"
p = urlparse(p_url)
p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}

with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path=pw.chromium.executable_path, headless=False, args=["--headless=new", "--no-sandbox"], proxy=p_cfg)
    c = b.new_context(user_agent=UA_WIN)
    page = c.new_page()
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=30000)
    for _ in range(25):
        time.sleep(1)
        t = (page.title() or "").lower()
        if t and "checkpoint" not in t:
            break

    res = page.evaluate("""async () => {
        const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
        for (const s of scripts) {
            const text = await (await fetch(s)).text();
            const idx = text.indexOf('if(!V)return;');
            if (idx !== -1) {
                return {
                    url: s,
                    snippet: text.substring(Math.max(0, idx - 400), Math.min(text.length, idx + 400))
                };
            }
        }
        return {error: 'not found'};
    }""")
    print("Found in:", res.get("url"))
    print("Snippet:\n", res.get("snippet"))
    b.close()
