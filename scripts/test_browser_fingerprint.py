import json
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
p_url = "http://AurhPd:FxitOe@14.224.198.119:49797"
p = urlparse(p_url)
p_cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}

with sync_playwright() as pw:
    chrome_exe = pw.chromium.executable_path
    b = pw.chromium.launch(executable_path=chrome_exe, headless=False, args=["--headless=new", "--no-sandbox"], proxy=p_cfg)
    c = b.new_context(user_agent=UA)
    page = c.new_page()
    page.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=45000)
    for _ in range(30):
        time.sleep(1)
        t = (page.title() or "").lower()
        if t and "checkpoint" not in t:
            break

    details = page.evaluate("""async () => {
        const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
        const out = [];
        for (const s of scripts) {
            try {
                const text = await (await fetch(s)).text();
                if (text.includes('/api/competitions')) {
                    const idx = text.indexOf('/api/competitions');
                    out.push({
                        url: s,
                        before: text.substring(Math.max(0, idx - 400), idx),
                        after: text.substring(idx, Math.min(text.length, idx + 600))
                    });
                }
            } catch(e) {}
        }
        return out;
    }""")
    for d in details:
        print("=== URL:", d["url"])
        print("BEFORE:\n", d["before"])
        print("AFTER:\n", d["after"])
        print("-" * 50)
    b.close()
