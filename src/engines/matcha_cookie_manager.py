"""Automated cookie extraction and rotation for MetaMatcha using Playwright stealth.

Extracts and caches Cloudflare (cf_clearance, __cf_bm, _cfuvid) and Kasada (KP_UIDz) tokens,
auto-rotating them periodically or on 403/429 access challenges.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any

from filelock import FileLock

logger = logging.getLogger("matcha.cookies")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_FILE = PROJECT_ROOT / ".matcha_cookies.json"
LOCK_FILE = PROJECT_ROOT / ".matcha_cookies.lock"
ROTATE_INTERVAL_SECONDS = 20 * 60  # 20 minutes
DEFAULT_URL = "https://meta.matcha.xyz/solana"


def is_vps() -> bool:
    """Determine if running in a VPS or remote cloud VM environment."""
    # 1. Direct environment variable flags
    for env_var in ("IS_VPS", "RUNNING_ON_VPS", "RUNNING_ON_VM", "VPS", "VM_HOST", "CLOUD_ENV"):
        val = os.getenv(env_var, "").strip().lower()
        if val in ("1", "true", "yes", "y"):
            return True
        if val in ("0", "false", "no", "n"):
            return False

    # 2. Check for VM marker files in project root
    for marker_name in ("runningonvm.txt", ".vps", ".runningonvm"):
        if (PROJECT_ROOT / marker_name).exists():
            if sys.platform != "darwin":
                return True

    # 3. Known VPS usernames
    user = (os.getenv("USER") or os.getenv("USERNAME") or "").strip().lower()
    if user in ("administrator", "root", "ubuntu", "ec2-user", "azureuser", "cloud-user", "vm-user", "admin"):
        return True

    # 4. OS-specific VPS detection
    if sys.platform == "linux":
        for p in ("/sys/class/dmi/id/product_name", "/sys/class/dmi/id/sys_vendor"):
            path = Path(p)
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore").lower()
                    if any(x in content for x in ("kvm", "qemu", "virtualbox", "vmware", "google", "amazon", "droplet", "openstack", "bochs", "microsoft corporation")):
                        return True
                except Exception:
                    pass
        return True  # Linux execution of this bot is virtually always on a cloud VPS/VM
    elif sys.platform == "win32":
        if user == "administrator":
            return True
        comp = os.getenv("COMPUTERNAME", "").lower()
        if any(x in comp for x in ("vps", "vm", "server", "rdp")):
            return True

    return False


def is_cookie_file_missing_or_empty(cache_file: Path | None = None) -> bool:
    """Check if the cookie cache file is not present, 0 bytes, or missing clearance cookies."""
    path = cache_file or CACHE_FILE
    if not path.exists():
        return True
    try:
        if path.stat().st_size == 0:
            return True
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies")
        if not isinstance(cookies, list) or len(cookies) == 0:
            return True
        has_clearance = any(c.get("name") in ("_vcrcs", "cf_clearance") for c in cookies)
        if not has_clearance:
            return True
        return False
    except Exception:
        return True


def ensure_vps_cookies(
    force: bool = False,
    target_url: str = DEFAULT_URL,
    logger_instance: Any = None,
) -> list[dict[str, Any]]:
    """If on a VPS and the cookie file is not present or empty, do the browser solve step and add cookies."""
    log = logger_instance or logger
    on_vps = is_vps()
    missing_or_empty = is_cookie_file_missing_or_empty(CACHE_FILE)

    if (on_vps and missing_or_empty) or force:
        reason = "force requested" if force else "VPS detected with missing/empty cookie file"
        log.info(
            "[CookieManager] %s. Executing browser solve step to acquire and add clearance cookies to %s...",
            reason,
            CACHE_FILE.name,
        )
        with FileLock(str(LOCK_FILE), timeout=60):
            if not force and not is_cookie_file_missing_or_empty(CACHE_FILE):
                cached = _read_cache(CACHE_FILE, ROTATE_INTERVAL_SECONDS)
                if cached:
                    return cached
            cookies = _solve_challenge(target_url)
            _write_cache(CACHE_FILE, cookies)
            log.info(
                "[CookieManager] Successfully added %d cookies to %s.",
                len(cookies),
                CACHE_FILE.name,
            )
            return cookies

    cached = _read_cache(CACHE_FILE, ROTATE_INTERVAL_SECONDS)
    return cached or []


def _read_cache(cache_file: Path, ttl: int) -> list[dict[str, Any]] | None:
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        age = time.time() - float(data.get("timestamp", 0))
        cookies = data.get("cookies")
        if isinstance(cookies, list) and len(cookies) > 0:
            # Require clearance cookie (_vcrcs or cf_clearance) in cache
            has_clearance = any(c.get("name") in ("_vcrcs", "cf_clearance") for c in cookies)
            if not has_clearance:
                logger.debug("Cached cookies missing clearance token (_vcrcs / cf_clearance); invalidating cache")
                return None
            if age < ttl:
                return cookies
            # Fallback to stale cookies up to 3x TTL rather than blocking quotes with 60s browser solve
            if age < ttl * 3:
                return cookies
    except Exception as exc:
        logger.debug("Failed to read cookie cache: %s", exc)
    return None


def _write_cache(cache_file: Path, cookies: list[dict[str, Any]]) -> None:
    if not cookies or len(cookies) == 0:
        logger.warning("[CookieManager] Solved 0 cookies; skipping cache overwrite to preserve existing session")
        return
    try:
        existing_cookies = []
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                    if isinstance(existing_data.get("cookies"), list):
                        existing_cookies = existing_data["cookies"]
            except Exception:
                pass

        cookie_dict = {(c.get("name"), c.get("domain")): c for c in existing_cookies if c.get("name")}
        for c in cookies:
            if c.get("name"):
                cookie_dict[(c.get("name"), c.get("domain"))] = c
        merged = list(cookie_dict.values())

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(cache_file.parent), prefix=".matcha_cookies_", suffix=".tmp"
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time(), "cookies": merged}, f, indent=2)
        os.replace(tmp_path, str(cache_file))
    except Exception as exc:
        logger.warning("Failed to write cookie cache: %s", exc)


DEFAULT_MATCHA_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

def trigger_proxy_rotation(rotate_url: str | None = None) -> bool:
    """Trigger residential proxy IP rotation via provider API if MATCHA_ROTATE_URL is set."""
    import urllib.request
    url = rotate_url or os.getenv("MATCHA_ROTATE_URL", "").strip()
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_MATCHA_USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            logger.info(
                "[ProxyManager] Proxy rotation triggered: status=%s, msg=%s, ip=%s",
                data.get("status"),
                data.get("message"),
                data.get("ip"),
            )
            time.sleep(3)
            return True
    except Exception as exc:
        logger.warning("[ProxyManager] Failed to trigger proxy rotation: %s", exc)
        return False


def _solve_challenge(target_url: str = DEFAULT_URL) -> list[dict[str, Any]]:
    """Spins up headless Chromium with Playwright stealth to solve Vercel/Cloudflare."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError as exc:
        raise RuntimeError(
            "playwright and playwright-stealth are required for automated MetaMatcha cookie rotation. "
            "Run `pip install playwright playwright-stealth && python -m playwright install chromium`"
        ) from exc

    logger.info("[CookieManager] Launching headless browser to solve Vercel/Cloudflare challenge...")
    t0 = time.perf_counter()
    proxy_url = os.getenv("MATCHA_PROXY", "").strip()
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-domain-reliability",
        "--disable-extensions",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-renderer-backgrounding",
        "--disable-sync",
    ]
    if not proxy_url:
        launch_args.append("--no-proxy-server")
    launch_kwargs: dict[str, Any] = {"headless": True, "args": launch_args}
    if proxy_url:
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        scheme = p.scheme or "http"
        proxy_cfg: dict[str, str] = {"server": f"{scheme}://{p.hostname}:{p.port}"}
        if p.username:
            proxy_cfg["username"] = p.username
        if p.password:
            proxy_cfg["password"] = p.password
        launch_kwargs["proxy"] = proxy_cfg
        logger.info("[CookieManager] Using proxy for challenge solver: %s:%s", p.hostname, p.port)

    def _execute_browser_solve(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        with sync_playwright() as p:
            browser = p.chromium.launch(**kwargs)
            context = browser.new_context(
                user_agent=DEFAULT_MATCHA_USER_AGENT,
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            Stealth().apply_stealth_sync(page)

            # Navigate directly to target MetaMatcha endpoint
            try:
                page.goto(target_url, timeout=35000, wait_until="domcontentloaded")
            except Exception as exc:
                logger.debug("Failed initial navigation to %s: %s", target_url, exc)

            # Actively poll for Vercel challenge clearance (_vcrcs or cf_clearance)
            cleared = False
            for poll_sec in range(30):
                time.sleep(1)
                cookies = context.cookies()
                cookie_map = {c.get("name"): c.get("value") for c in cookies}
                has_clearance = "_vcrcs" in cookie_map or "cf_clearance" in cookie_map
                title = (page.title() or "").lower()

                is_checkpoint = any(marker in title for marker in ("checkpoint", "just a moment", "challenge"))
                if has_clearance and not is_checkpoint:
                    logger.info(
                        "[CookieManager] Vercel clearance cookie acquired at %ds",
                        poll_sec + 1,
                    )
                    cleared = True
                    break
                elif not is_checkpoint and len(title) > 0 and poll_sec >= 3:
                    cleared = True
                    break

            if cleared:
                time.sleep(2.0)
                try:
                    page.evaluate("""async () => {
                        try { await fetch("https://meta.matcha.xyz/api/gas?chainId=1"); } catch(e){}
                    }""")
                    time.sleep(1.0)
                except Exception:
                    pass

            c = context.cookies()
            browser.close()
            return c

    try:
        cookies = _execute_browser_solve(launch_kwargs)
    except Exception as exc:
        cookies = []
        logger.warning("[CookieManager] Headless solve failed: %s", exc)

    has_vcrcs = any(c.get("name") == "_vcrcs" for c in cookies)
    has_cf = any(c.get("name") == "cf_clearance" for c in cookies)

    # Fall back to direct connection if proxy failed to acquire clearance tokens
    if proxy_url and not (has_vcrcs or has_cf):
        logger.warning(
            "[CookieManager] Proxy failed to collect clearance cookies; retrying directly without proxy..."
        )
        direct_kwargs = dict(launch_kwargs)
        direct_kwargs.pop("proxy", None)
        if "--no-proxy-server" not in direct_kwargs.get("args", []):
            direct_kwargs["args"] = list(direct_kwargs.get("args", [])) + ["--no-proxy-server"]
        try:
            direct_cookies = _execute_browser_solve(direct_kwargs)
            if any(c.get("name") in ("_vcrcs", "cf_clearance") for c in direct_cookies):
                cookies = direct_cookies
                has_vcrcs = any(c.get("name") == "_vcrcs" for c in cookies)
                has_cf = any(c.get("name") == "cf_clearance" for c in cookies)
                logger.info("[CookieManager] Direct fallback solve succeeded! Clearance cookie acquired.")
                os.environ["MATCHA_PROXY"] = ""
                try:
                    from .matcha_browser_bridge import stop_bridge_server
                    stop_bridge_server()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("[CookieManager] Direct fallback solve failed: %s", exc)

    elapsed = round(time.perf_counter() - t0, 2)
    logger.info(
        "[CookieManager] Collected %d cookies in %ss (_vcrcs: %s, cf_clearance: %s)",
        len(cookies),
        elapsed,
        "Present" if has_vcrcs else "Missing",
        "Present" if has_cf else "Missing",
    )
    return cookies


_background_solver: BackgroundCookieSolver | None = None
_solver_lock = threading.RLock()


class BackgroundCookieSolver(threading.Thread):
    """Dedicated background solver daemon that handles Playwright clearance solving without blocking trading threads."""

    def __init__(self, interval: int = ROTATE_INTERVAL_SECONDS, target_url: str = DEFAULT_URL) -> None:
        super().__init__(daemon=True, name="MatchaBackgroundCookieSolver")
        self.interval = interval
        self.target_url = target_url
        self._solve_requested = threading.Event()
        self._stop_event = threading.Event()
        self._last_solve_time = 0.0

    def trigger_solve(self) -> None:
        """Asynchronously signal the background solver to acquire fresh clearance cookies."""
        self._solve_requested.set()

    def stop(self) -> None:
        """Signal the solver daemon to stop cleanly."""
        self._stop_event.set()
        self._solve_requested.set()

    def run(self) -> None:
        logger.info("[CookieSolver] Background solver daemon started (refresh interval: %ds)", self.interval)
        # Check initial cache status
        try:
            cached = _read_cache(CACHE_FILE, self.interval)
            if cached is None:
                self._solve_requested.set()
        except Exception as exc:
            logger.debug("[CookieSolver] Initial cache check error: %s", exc)

        while not self._stop_event.is_set():
            # Wait for solve request or scheduled interval timeout
            triggered = self._solve_requested.wait(timeout=max(30.0, float(self.interval - 300)))
            if self._stop_event.is_set():
                break

            self._solve_requested.clear()
            now = time.monotonic()
            # Debounce rapid solve triggers (at least 15s between headless browser solves)
            if now - self._last_solve_time < 15.0 and not triggered:
                continue

            try:
                logger.info("[CookieSolver] Background Playwright clearance solve starting...")
                trigger_proxy_rotation()
                with FileLock(str(LOCK_FILE), timeout=60):
                    cookies = _solve_challenge(self.target_url)
                    _write_cache(CACHE_FILE, cookies)
                self._last_solve_time = time.monotonic()
                logger.info("[CookieSolver] Background clearance solve complete. Cache updated.")
            except Exception as exc:
                logger.warning("[CookieSolver] Background clearance solve error: %s", exc)
                time.sleep(10.0)


def trigger_background_solve() -> None:
    """Non-blocking signal for background solver to fetch fresh cookies."""
    global _background_solver
    with _solver_lock:
        if _background_solver is None or not _background_solver.is_alive():
            start_background_cookie_solver()
        if _background_solver is not None:
            _background_solver.trigger_solve()


def start_background_cookie_solver(
    interval: int = ROTATE_INTERVAL_SECONDS,
    target_url: str = DEFAULT_URL,
) -> BackgroundCookieSolver:
    """Starts the global background cookie solver daemon if not already running."""
    global _background_solver
    with _solver_lock:
        if _background_solver is not None and _background_solver.is_alive():
            return _background_solver
        _background_solver = BackgroundCookieSolver(interval=interval, target_url=target_url)
        _background_solver.start()
        return _background_solver


def stop_background_cookie_solver() -> None:
    """Stops the global background cookie solver daemon."""
    global _background_solver
    with _solver_lock:
        if _background_solver is not None:
            _background_solver.stop()
            _background_solver = None


# Alias for backward compatibility
start_background_rotator = start_background_cookie_solver


def get_valid_cookies(
    force_refresh: bool = False,
    target_url: str = DEFAULT_URL,
    ttl: int = ROTATE_INTERVAL_SECONDS,
    non_blocking: bool = True,
) -> list[dict[str, Any]]:
    """Returns valid MetaMatcha cookies from cache, triggering background solver when stale without blocking trading."""
    cached = _read_cache(CACHE_FILE, ttl)
    if cached is not None and not force_refresh:
        return cached

    # Quote workers must remain non-blocking on every host. Startup callers that
    # need to wait for cookies can explicitly request non_blocking=False.
    # In non-blocking mode, signal background solver and return cached cookies immediately
    if non_blocking:
        trigger_background_solve()
        if cached is not None:
            return cached
        # Give background solver or existing file a brief check
        cached_fallback = _read_cache(CACHE_FILE, ttl * 5)
        if cached_fallback:
            return cached_fallback
        return []

    # Explicit blocking solve requested
    with FileLock(str(LOCK_FILE), timeout=60):
        cached = _read_cache(CACHE_FILE, ttl)
        if cached is not None and not force_refresh:
            return cached
        cookies = _solve_challenge(target_url)
        _write_cache(CACHE_FILE, cookies)
        return cookies


def inject_matcha_cookies(
    session: Any,
    force_refresh: bool = False,
    target_url: str = DEFAULT_URL,
    chain_env_key: str | None = None,
    non_blocking: bool = True,
) -> list[dict[str, Any]]:
    """Injects fresh or cached cookies into a requests / curl_cffi Session without blocking trading loops."""
    cookies = get_valid_cookies(
        force_refresh=force_refresh,
        target_url=target_url,
        non_blocking=non_blocking,
    )
    cookie_pairs: list[str] = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".matcha.xyz"
        cookie_pairs.append(f"{name}={value}")
        try:
            session.cookies.set(name, value, domain=domain)
        except Exception:
            session.cookies.set(name, value)

    # Attach explicit Cookie header so requests to any subdomain (meta.matcha.xyz) carry tokens
    if hasattr(session, "headers"):
        if cookie_pairs:
            session.headers["cookie"] = "; ".join(cookie_pairs)
        session.headers["user-agent"] = DEFAULT_MATCHA_USER_AGENT
        session.headers["sec-ch-ua-platform"] = '"macOS"'
        session.headers["sec-fetch-site"] = "same-origin"

    # Merge explicit environment override if provided
    env_keys = [chain_env_key, "MATCHA_COOKIES"] if chain_env_key else ["MATCHA_COOKIES"]
    for key in env_keys:
        if not key:
            continue
        env_val = os.environ.get(key, "").strip()
        if env_val:
            for item in env_val.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    session.cookies.set(k.strip(), v.strip())
            break

    return cookies


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MetaMatcha Cookie Manager & VPS Clearance Solver")
    parser.add_argument("--force", action="store_true", help="Force browser challenge solve and write cookies")
    parser.add_argument("--check-vps", action="store_true", help="Ensure cookies if on a VPS and missing")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Target URL for challenge solve (default: {DEFAULT_URL})")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    vps_status = is_vps()
    missing_status = is_cookie_file_missing_or_empty()
    print(f"VPS Detected: {vps_status}")
    print(f"Cookie Cache Missing or Empty: {missing_status}")

    if args.force or (vps_status and missing_status) or args.check_vps:
        res = ensure_vps_cookies(force=args.force or (not vps_status and args.check_vps), target_url=args.url)
        print(f"Result: Acquired and saved {len(res)} cookies to {CACHE_FILE.name}.")
    else:
        cached = _read_cache(CACHE_FILE, ROTATE_INTERVAL_SECONDS)
        print(f"Result: {len(cached or [])} existing valid cookies in {CACHE_FILE.name}.")

