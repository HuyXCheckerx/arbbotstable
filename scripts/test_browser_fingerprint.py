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

    # Look for the botid script in all script tags or global window
    info = page.evaluate("""async () => {
        let botidCode = "";
        const scripts = Array.from(document.querySelectorAll('script')).map(s => s.src || s.innerText);
        for (const s of scripts) {
            let text = s;
            if (s.startsWith('http')) {
                try { text = await (await fetch(s)).text(); } catch(e){}
            }
            if (text.includes('checkLevel') || text.includes('botId') || text.includes('botid')) {
                botidCode = text;
                break;
            }
        }
        return {
            windowKeys: Object.keys(window).filter(k => k.toLowerCase().includes('bot') || k.toLowerCase().includes('kasada') || k.toLowerCase().includes('protect')),
            botidSnippet: botidCode.substring(0, 2000)
        };
    }""")
    print("Window keys:", info.get("windowKeys"))
    print("\nBotID Code Snippet:\n", info.get("botidSnippet")[:1500])
    b.close()
