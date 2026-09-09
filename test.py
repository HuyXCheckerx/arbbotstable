import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from curl_cffi import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

CACHE_FILE = ".matcha_cookies.json"
ROTATE_INTERVAL_SECONDS = 20 * 60  # Rotate every 20 minutes
BASE_URL = "https://meta.matcha.xyz"
SOLANA_PAGE = f"{BASE_URL}/solana"
CHAIN_ID = 1399811149

HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": BASE_URL,
    "referer": SOLANA_PAGE,
}


class MatchaSessionManager:
    """Manages curl_cffi session with headless Playwright stealth cookie auto-rotation."""

    def __init__(self, cache_file: str = CACHE_FILE, ttl: int = ROTATE_INTERVAL_SECONDS):
        self.cache_file = cache_file
        self.ttl = ttl
        self.lock = threading.Lock()
        self.last_refreshed: float = 0
        self.session = requests.Session(impersonate="chrome124")
        self._init_session()

    def _init_session(self):
        """Loads cached cookies if valid; otherwise solves via headless Playwright."""
        if self._load_cache():
            return
        self.refresh_cookies()

    def _load_cache(self) -> bool:
        if not os.path.exists(self.cache_file):
            return False
        try:
            with open(self.cache_file, "r") as f:
                data = json.load(f)
            age = time.time() - data.get("timestamp", 0)
            if age < self.ttl and data.get("cookies"):
                self._inject_cookies(data["cookies"])
                self.last_refreshed = data["timestamp"]
                print(f"[CookieManager] Loaded {len(data['cookies'])} cached cookies (age: {int(age)}s)")
                return True
        except Exception as e:
            print(f"[CookieManager] Cache read failed: {e}")
        return False

    def _inject_cookies(self, cookies: list[dict]):
        for c in cookies:
            self.session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain") or ".matcha.xyz",
            )

    def refresh_cookies(self):
        """Spins up lightweight headless Chromium, applies stealth, and captures tokens."""
        with self.lock:
            print("[CookieManager] Launching headless browser to solve Cloudflare/Kasada...")
            t0 = time.perf_counter()
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()
                Stealth().apply_stealth_sync(page)

                page.goto(SOLANA_PAGE, timeout=30000, wait_until="domcontentloaded")
                time.sleep(5)  # Allow Cloudflare challenge execution & Kasada script initialization

                cookies = context.cookies()
                browser.close()

            elapsed = round(time.perf_counter() - t0, 2)
            self._inject_cookies(cookies)
            self.last_refreshed = time.time()

            # Save to disk cache
            with open(self.cache_file, "w") as f:
                json.dump({"timestamp": self.last_refreshed, "cookies": cookies}, f)

            cf_token = next((c["value"][:15] + "..." for c in cookies if c["name"] == "cf_clearance"), "None")
            print(f"[CookieManager] Refreshed {len(cookies)} cookies in {elapsed}s (cf_clearance: {cf_token})")

    def ensure_valid_session(self):
        """Checks age and rotates if needed."""
        if time.time() - self.last_refreshed > self.ttl:
            self.refresh_cookies()

    def start_background_rotator(self):
        """Starts a background daemon thread that rotates cookies every `ttl` seconds."""
        def _loop():
            while True:
                time.sleep(self.ttl - 60)  # Refresh 1 minute before TTL
                try:
                    self.refresh_cookies()
                except Exception as exc:
                    print(f"[CookieManager] Auto-rotation error: {exc}")

        thread = threading.Thread(target=_loop, daemon=True, name="CookieRotatorThread")
        thread.start()
        print(f"[CookieManager] Background rotation worker active (interval: {self.ttl}s)")


# =====================================================================
# Main Quote Pipeline (Polling every 5 seconds)
# =====================================================================

POLL_INTERVAL_SECONDS = 5
SELL_AMOUNT_USDG = 100_000
SELL_AMOUNT_RAW = "100000000000"  # 100k with 6 decimals
AGGREGATORS = ["0x", "DFlow", "Jupiter", "OKX"]

manager = MatchaSessionManager()
manager.start_background_rotator()  # Auto-refreshes Cloudflare/Kasada in background every 20 min


def poll_once(iteration: int):
    manager.ensure_valid_session()
    session = manager.session
    now_str = time.strftime("%H:%M:%S")

    comp_payload = {
        "chainId": CHAIN_ID,
        "sellTokenAddress": "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",  # USDG
        "buyTokenAddress": "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",   # PYUSD
        "sellAmount": SELL_AMOUNT_RAW,
        "sellTokenDecimals": 6,
        "buyTokenDecimals": 6,
        "slippageBps": 50,
    }

    # Step 1: Create competition
    comp_res = session.post(f"{BASE_URL}/api/competitions", headers=HEADERS, json=comp_payload)

    # Auto-handle challenge/expiration if encountered
    if comp_res.status_code in (403, 429):
        print(f"[{now_str}] Received HTTP {comp_res.status_code}, forcing cookie refresh...")
        manager.refresh_cookies()
        comp_res = session.post(f"{BASE_URL}/api/competitions", headers=HEADERS, json=comp_payload)

    if comp_res.status_code != 200:
        print(f"[{now_str}] [Error] Competition failed: {comp_res.status_code} {comp_res.text}")
        return

    comp_id = comp_res.json().get("id")

    # Step 2: Query all 4 Solana aggregators in parallel
    def fetch_quote(agg: str):
        t0 = time.perf_counter()
        res = session.post(
            f"{BASE_URL}/api/quotes?aggregator={agg}",
            headers=HEADERS,
            json={"competitionId": comp_id, "aggregator": agg},
        )
        dt = int((time.perf_counter() - t0) * 1000)
        return agg, res.status_code, res.json() if res.status_code == 200 else {}, dt

    with ThreadPoolExecutor(max_workers=len(AGGREGATORS)) as pool:
        results = list(pool.map(fetch_quote, AGGREGATORS))

    quotes = []
    for agg, status, body, latency in results:
        direct = body.get("direct", {})
        quote = direct.get("quote", {})
        sim = direct.get("simulation", {})
        buy_amount_raw = quote.get("buyAmount")
        buy_amount = int(buy_amount_raw) if buy_amount_raw else 0
        quotes.append({
            "aggregator": agg,
            "status": status,
            "latency_ms": latency,
            "buy_amount_pyusd": buy_amount / 1e6,
            "sources": quote.get("sources", []),
            "sim_result": sim.get("result"),
        })

    quotes.sort(key=lambda q: (q["sim_result"] == "success", q["buy_amount_pyusd"]), reverse=True)
    best = quotes[0] if quotes else None
    diff = (best["buy_amount_pyusd"] - SELL_AMOUNT_USDG) if best and best["sim_result"] == "success" else 0.0
    spread_str = f"+${diff:.4f}" if diff >= 0 else f"-${abs(diff):.4f}"

    print(f"\n[{now_str}] Poll #{iteration} | Best: {best['aggregator']} ({best['buy_amount_pyusd']:,.4f} PYUSD | Spread: {spread_str})", flush=True)
    print(f"{'Aggregator':<10} | {'Status':<6} | {'Sim':<8} | {'Out (PYUSD)':<14} | {'Latency':<9} | {'Sources'}", flush=True)
    print("-" * 75, flush=True)
    for q in quotes:
        sources = ", ".join(q["sources"]) if q["sources"] else "-"
        print(f"{q['aggregator']:<10} | {q['status']:<6} | {str(q['sim_result']):<8} | {q['buy_amount_pyusd']:<14.6f} | {q['latency_ms']:<5} ms | {sources}", flush=True)


if __name__ == "__main__":
    print(f"Starting MetaMatcha quote monitor (refreshing every {POLL_INTERVAL_SECONDS}s)... Press Ctrl+C to stop.\n", flush=True)
    iteration = 1
    try:
        while True:
            t_start = time.perf_counter()
            poll_once(iteration)
            iteration += 1
            elapsed = time.perf_counter() - t_start
            sleep_time = max(0.0, POLL_INTERVAL_SECONDS - elapsed)
            time.sleep(sleep_time)
    except KeyboardInterrupt:
        print("\n[!] Stopped quote monitor.", flush=True)



