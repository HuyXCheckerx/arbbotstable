"""Matcha Browser Bridge Daemon & Client.

Solves Kasada KPSDK client-side telemetry and Vercel firewall challenges by keeping warm
browser tabs active on MetaMatcha Ethereum and Solana endpoints. Quotes are executed directly
within the page context via native browser fetch(), guaranteeing genuine single-use KPSDK
proof-of-work tokens and avoiding HTTP 403 blocks.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.request
import urllib.error

logger = logging.getLogger("matcha.bridge")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PID_FILE = PROJECT_ROOT / ".matcha_bridge.pid"
DEFAULT_PORT = 18234
DEFAULT_HOST = "127.0.0.1"


def get_bridge_port() -> int:
    return int(os.getenv("MATCHA_BRIDGE_PORT", str(DEFAULT_PORT)))


def get_bridge_base_url() -> str:
    host = os.getenv("MATCHA_BRIDGE_HOST", DEFAULT_HOST)
    port = get_bridge_port()
    return f"http://{host}:{port}"


def is_bridge_ready(base_url: str | None = None, timeout: float = 0.8) -> bool:
    """Check if the Matcha browser bridge is running and tabs are warmed/cleared."""
    url = f"{base_url or get_bridge_base_url()}/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MatchaBridgeClient"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ready"))
    except Exception:
        pass
    return False


def ensure_bridge_running(timeout: float = 35.0) -> bool:
    """Ensures the background Matcha browser bridge server is running."""
    base_url = get_bridge_base_url()
    if is_bridge_ready(base_url, timeout=0.8):
        return True

    bridge_script = Path(__file__).resolve()
    logger.info("[MatchaBridge] Bridge daemon not detected at %s. Launching background worker...", base_url)

    flags = 0
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    try:
        subprocess.Popen(
            [sys.executable, str(bridge_script)],
            creationflags=flags,
            close_fds=(sys.platform != "win32"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("[MatchaBridge] Failed to spawn bridge process: %s", exc)
        return False

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if is_bridge_ready(base_url, timeout=1.0):
            logger.info("[MatchaBridge] Bridge daemon ready at %s (took %.1fs)", base_url, time.monotonic() - t0)
            return True
        time.sleep(0.5)

    logger.warning("[MatchaBridge] Timed out waiting for bridge daemon to be ready (%ss)", timeout)
    return False


def fetch_bridge_quotes(
    chain: str,
    payload: dict[str, Any],
    aggregators: list[str],
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Fetch quotes via the Matcha browser bridge."""
    if not ensure_bridge_running(timeout=30.0):
        raise RuntimeError("Matcha browser bridge daemon is not running or failed to clear challenges")

    chain_name = "ethereum" if chain in ("ethereum", "1", 1) else "solana"
    url = f"{get_bridge_base_url()}/{chain_name}/quote"
    req_body = json.dumps({"payload": payload, "aggregators": aggregators}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=req_body,
        headers={"Content-Type": "application/json", "User-Agent": "MatchaBridgeClient"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if "error" in data:
                    raise RuntimeError(f"Matcha bridge error: {data['error']}")
                return data.get("quotes", {})
            raise RuntimeError(f"Matcha bridge HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        try:
            err_data = json.loads(exc.read().decode("utf-8"))
            err_msg = err_data.get("error", str(exc))
        except Exception:
            err_msg = str(exc)
        raise RuntimeError(f"Matcha bridge request failed (HTTP {exc.code}): {err_msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"Matcha bridge connection failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Server / Daemon Implementation
# ---------------------------------------------------------------------------

class _BridgeServerState:
    def __init__(self) -> None:
        from queue import Queue
        self.queue: Queue[Any] = Queue()
        self.ready_eth = threading.Event()
        self.ready_sol = threading.Event()
        self.is_running = True


def _run_playwright_worker(state: _BridgeServerState) -> None:
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError as exc:
        logger.error("[MatchaBridge] playwright or playwright_stealth missing: %s", exc)
        return

    logger.info("[MatchaBridge] Initializing Playwright worker...")
    proxy_url = os.getenv("MATCHA_PROXY", "").strip()
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
    launch_kwargs: dict[str, Any] = {"headless": True, "args": launch_args}
    if proxy_url:
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        scheme = p.scheme or "http"
        proxy_cfg = {"server": f"{scheme}://{p.hostname}:{p.port}"}
        if p.username:
            proxy_cfg["username"] = p.username
        if p.password:
            proxy_cfg["password"] = p.password
        launch_kwargs["proxy"] = proxy_cfg
        logger.info("[MatchaBridge] Using proxy for browser: %s:%s", p.hostname, p.port)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport={"width": 1280, "height": 800})

        page_eth = context.new_page()
        Stealth().apply_stealth_sync(page_eth)

        page_sol = context.new_page()
        Stealth().apply_stealth_sync(page_sol)

        logger.info("[MatchaBridge] Warming Ethereum tab...")
        try:
            page_eth.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.debug("[MatchaBridge] ETH initial navigation notice: %s", e)

        logger.info("[MatchaBridge] Warming Solana tab...")
        try:
            page_sol.goto("https://meta.matcha.xyz/solana", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.debug("[MatchaBridge] SOL initial navigation notice: %s", e)

        # Clearance loop
        for poll_idx in range(40):
            time.sleep(1)
            t_eth = ""
            t_sol = ""
            try:
                t_eth = (page_eth.title() or "").lower()
                t_sol = (page_sol.title() or "").lower()
            except Exception:
                pass

            eth_ok = "checkpoint" not in t_eth and len(t_eth) > 0
            sol_ok = "checkpoint" not in t_sol and len(t_sol) > 0

            if eth_ok and sol_ok and poll_idx >= 2:
                logger.info("[MatchaBridge] Both tabs cleared! (ETH: %s, SOL: %s)", t_eth, t_sol)
                time.sleep(2.5)  # Allow Kasada runtime to settle
                state.ready_eth.set()
                state.ready_sol.set()
                break

        last_health_check = time.monotonic()
        while state.is_running:
            try:
                task = state.queue.get(timeout=1.0)
            except Exception:
                # Periodic health check / keep-alive
                if time.monotonic() - last_health_check > 120.0:
                    last_health_check = time.monotonic()
                    try:
                        t_e = (page_eth.title() or "").lower()
                        if "checkpoint" in t_e:
                            logger.info("[MatchaBridge] ETH checkpoint detected during check, reloading...")
                            page_eth.reload(wait_until="domcontentloaded")
                    except Exception:
                        pass
                continue

            chain, payload, aggregators, event, result_box = task
            page = page_eth if chain == "ethereum" else page_sol

            try:
                t0 = time.perf_counter()
                res = page.evaluate(
                    """async (args) => {
                        const { chain, payload, aggregators } = args;
                        const taker = payload.taker ? (chain === 'ethereum' ? payload.taker.toLowerCase() : payload.taker) : '';
                        const headers = { 'content-type': 'application/json', 'x-fetch-native': '1' };
                        if (chain === 'ethereum' && taker) {
                            headers['x-taker'] = taker;
                        }

                        if (chain === 'ethereum') {
                            try { await fetch('https://meta.matcha.xyz/api/gas?chainId=1'); } catch(e) {}
                        }
                        
                        let compRes = await fetch('https://meta.matcha.xyz/api/competitions', {
                            method: 'POST',
                            headers,
                            body: JSON.stringify(payload)
                        });
                        let comp = await compRes.json();
                        let compId = comp.id || comp.competitionId;
                        if (!compId) {
                            return { error: comp };
                        }
                        
                        const quotes = {};
                        await Promise.all(aggregators.map(async (agg) => {
                            try {
                                const qRes = await fetch(`https://meta.matcha.xyz/api/quotes?aggregator=${agg}`, {
                                    method: 'POST',
                                    headers,
                                    body: JSON.stringify({ competitionId: compId, aggregator: agg })
                                });
                                quotes[agg] = await qRes.json();
                            } catch (e) {
                                quotes[agg] = { error: String(e) };
                            }
                        }));
                        return { competitionId: compId, quotes };
                    }""",
                    {"chain": chain, "payload": payload, "aggregators": aggregators},
                )
                elapsed = time.perf_counter() - t0
                logger.info("[MatchaBridge] Quote [%s] fetched in %.1fms (aggregators: %s)", chain, elapsed * 1000, aggregators)
                result_box["result"] = res
            except Exception as exc:
                logger.error("[MatchaBridge] Evaluate error on [%s]: %s", chain, exc)
                result_box["error"] = str(exc)
            finally:
                event.set()
                state.queue.task_done()

        browser.close()


def run_bridge_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the bridge HTTP service and Playwright worker."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    state = _BridgeServerState()
    worker_thread = threading.Thread(target=_run_playwright_worker, args=(state,), daemon=True, name="MatchaPlaywrightWorker")
    worker_thread.start()

    class BridgeHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # Suppress access logs for high-frequency quoting

        def do_GET(self) -> None:
            if self.path == "/health":
                ready = state.ready_eth.is_set() and state.ready_sol.is_set()
                status = 200 if ready else 503
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ready": ready}).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode("utf-8")
            try:
                req_data = json.loads(body)
            except Exception as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"Invalid JSON: {exc}"}).encode("utf-8"))
                return

            chain = "ethereum" if "/ethereum" in self.path else "solana"
            payload = req_data.get("payload", {})
            aggregators = req_data.get("aggregators", ["0x"] if chain == "ethereum" else ["Jupiter"])

            event = threading.Event()
            result_box: dict[str, Any] = {}
            state.queue.put((chain, payload, aggregators, event, result_box))

            if not event.wait(timeout=25.0):
                self.send_response(504)
                self.end_headers()
                self.wfile.write(b'{"error": "Matcha bridge evaluation timeout"}')
                return

            if "error" in result_box:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": result_box["error"]}).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result_box["result"]).encode("utf-8"))

    server = HTTPServer((host, port), BridgeHandler)
    logger.info("[MatchaBridge] Server running at http://%s:%d (PID %d)", host, port, os.getpid())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        state.is_running = False
        server.server_close()
    finally:
        try:
            if PID_FILE.exists():
                PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    port = get_bridge_port()
    host = os.getenv("MATCHA_BRIDGE_HOST", DEFAULT_HOST)
    run_bridge_server(host, port)
