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
        if age < ttl and isinstance(cookies, list) and len(cookies) > 0:
            return cookies
    except Exception as exc:
        logger.debug("Failed to read cookie cache: %s", exc)
    return None


def _write_cache(cache_file: Path, cookies: list[dict[str, Any]]) -> None:
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

if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


def _solve_challenge(target_url: str = DEFAULT_URL) -> list[dict[str, Any]]:
    """Spins up headless Chromium with Playwright stealth to solve Cloudflare/Kasada."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError as exc:
        raise RuntimeError(
            "playwright and playwright-stealth are required for automated MetaMatcha cookie rotation. "
            "Run `pip install playwright playwright-stealth && python -m playwright install chromium`"
        ) from exc

    logger.info("[CookieManager] Launching headless browser to solve Cloudflare/Kasada challenge...")
    t0 = time.perf_counter()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=DEFAULT_MATCHA_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        # 1. Visit root matcha.xyz to solve Cloudflare challenge for .matcha.xyz domain
        try:
            page.goto("https://matcha.xyz", timeout=30000, wait_until="domcontentloaded")
            time.sleep(3)
        except Exception as exc:
            logger.debug("Failed visiting matcha.xyz: %s", exc)

        # 2. Visit target meta endpoint to initialize subdomain tokens
        try:
            page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(2)
            # Warm up API endpoints directly in browser to initialize tokens and WAF session
            page.evaluate("() => fetch('/api/gas?chainId=1').catch(() => {})")
        except Exception as exc:
            logger.debug("Failed visiting %s: %s", target_url, exc)

        cookies = context.cookies()
        browser.close()

    elapsed = round(time.perf_counter() - t0, 2)
    cf_token = next((c["value"][:15] + "..." for c in cookies if c["name"] == "cf_clearance"), "None")
    logger.info(
        "[CookieManager] Successfully solved challenge: %d cookies in %ss (cf_clearance: %s)",
        len(cookies),
        elapsed,
        cf_token,
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
