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
    c = b.new_context(user_agent=UA)
    page = c.new_page()
    Stealth().apply_stealth_sync(page)

    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=45000)
    for _ in range(30):
        time.sleep(1)
        t = (page.title() or "").lower()
        if t and "checkpoint" not in t:
            break
    time.sleep(3)

    # Let's inspect 3md428e29o3sy.js to see what ec() does!
    res = page.evaluate("""async () => {
        const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
        for (const s of scripts) {
            if (s.includes('3md428e29o3sy')) {
                const text = await (await fetch(s)).text();
                const idx = text.indexOf('/api/competitions');
                return {
                    context: text.substring(Math.max(0, idx - 600), Math.min(text.length, idx + 800))
                };
            }
        }
        return {error: 'not found'};
    }""")
    print("3md428e29o3sy Context:\n", res.get("context"))

    b.close()
