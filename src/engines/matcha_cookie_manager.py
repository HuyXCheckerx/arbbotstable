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
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(cache_file.parent), prefix=".matcha_cookies_", suffix=".tmp"
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time(), "cookies": cookies}, f)
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

                if has_clearance:
                    logger.info(
                        "[CookieManager] Vercel clearance cookie acquired at %ds",
                        poll_sec + 1,
                    )
                    cleared = True
                    break
                elif "checkpoint" not in title and "just a moment" not in title and len(title) > 0 and poll_sec >= 3:
                    cleared = True
                    break

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


def get_valid_cookies(
    force_refresh: bool = False,
    target_url: str = DEFAULT_URL,
    ttl: int = ROTATE_INTERVAL_SECONDS,
) -> list[dict[str, Any]]:
    """Returns valid MetaMatcha cookies from cache or solves via headless browser."""
    if not force_refresh:
        cached = _read_cache(CACHE_FILE, ttl)
        if cached is not None:
            return cached

    # Inter-process lock to prevent multiple worker processes from launching browsers concurrently
    with FileLock(str(LOCK_FILE), timeout=60):
        if not force_refresh:
            cached = _read_cache(CACHE_FILE, ttl)
            if cached is not None:
                return cached

        cookies = _solve_challenge(target_url)
        _write_cache(CACHE_FILE, cookies)
        return cookies


def inject_matcha_cookies(
    session: Any,
    force_refresh: bool = False,
    target_url: str = DEFAULT_URL,
    chain_env_key: str | None = None,
) -> list[dict[str, Any]]:
    """Injects fresh or cached cookies into a requests / curl_cffi Session."""
    cookies = get_valid_cookies(force_refresh=force_refresh, target_url=target_url)
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


_rotator_thread: threading.Thread | None = None
_rotator_lock = threading.Lock()


def start_background_rotator(interval: int = ROTATE_INTERVAL_SECONDS) -> threading.Thread:
    """Starts a global background daemon thread to rotate cookies every `interval` seconds."""
    global _rotator_thread
    with _rotator_lock:
        if _rotator_thread is not None and _rotator_thread.is_alive():
            return _rotator_thread

        def _loop():
            # Initial ensure
            try:
                get_valid_cookies()
            except Exception as exc:
                logger.warning("[CookieManager] Initial cookie ensure failed: %s", exc)

            while True:
                time.sleep(max(60, interval - 60))
                try:
                    logger.info("[CookieManager] Background rotation triggered...")
                    trigger_proxy_rotation()
                    with FileLock(str(LOCK_FILE), timeout=60):
                        cookies = _solve_challenge(DEFAULT_URL)
                        _write_cache(CACHE_FILE, cookies)
                except Exception as exc:
                    logger.warning("[CookieManager] Background rotation failed: %s", exc)

        _rotator_thread = threading.Thread(
            target=_loop, daemon=True, name="MatchaCookieRotator"
        )
        _rotator_thread.start()
        logger.info("[CookieManager] Background rotation worker active (interval: %ds)", interval)
        return _rotator_thread
