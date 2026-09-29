"""Keep browser tabs for MetaMatcha quotes and report real upstream failures.

A ready browser does not guarantee API access. Lifecycle operations are serialized
across callers so a slow request or browser warmup cannot trigger a restart storm.
"""

from __future__ import annotations

import hashlib
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

from filelock import FileLock, Timeout as FileLockTimeout

logger = logging.getLogger("matcha.bridge")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PID_FILE = PROJECT_ROOT / ".matcha_bridge.pid"
LOCK_FILE = PROJECT_ROOT / ".matcha_bridge.lock"
DEFAULT_PORT = 18234
DEFAULT_HOST = "127.0.0.1"
BRIDGE_VERSION = 4
# Diagnostic instances on another port must not share lifecycle files.
if os.getenv("MATCHA_BRIDGE_PORT", str(DEFAULT_PORT)) != str(DEFAULT_PORT):
    _port = int(os.environ["MATCHA_BRIDGE_PORT"])
    PID_FILE = PROJECT_ROOT / f".matcha_bridge_{_port}.pid"
    LOCK_FILE = PROJECT_ROOT / f".matcha_bridge_{_port}.lock"


def get_bridge_port() -> int:
    return int(os.getenv("MATCHA_BRIDGE_PORT", str(DEFAULT_PORT)))


def get_bridge_base_url() -> str:
    host = os.getenv("MATCHA_BRIDGE_HOST", DEFAULT_HOST)
    port = get_bridge_port()
    return f"http://{host}:{port}"


def proxy_fingerprint(proxy: str | None = None) -> str:
    value = os.getenv("MATCHA_PROXY", "").strip() if proxy is None else proxy.strip()
    return hashlib.sha256(value.encode()).hexdigest()


def _compatible(status: dict[str, Any]) -> bool:
    # Compare launch configuration, not the active connection after direct fallback.
    return (status.get("version") == BRIDGE_VERSION
            and status.get("configured_proxy") == proxy_fingerprint())


def is_bridge_ready(base_url: str | None = None, timeout: float = 0.8) -> bool:
    status = get_bridge_status(base_url, timeout)
    return bool(status.get("ready") and _compatible(status))


def get_bridge_status(base_url: str | None = None, timeout: float = 0.8) -> dict[str, Any]:
    """Query bridge daemon health and error status."""
    url = f"{base_url or get_bridge_base_url()}/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MatchaBridgeClient"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ready": False, "error": f"HTTP {exc.code}"}
    except Exception:
        return {"ready": False, "unreachable": True}


def is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is currently active in the OS."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (OSError, ProcessLookupError):
            return False


def _stop_bridge_server_locked(base_url: str | None = None) -> bool:
    """Request graceful shutdown of the background Matcha bridge daemon."""
    url = f"{base_url or get_bridge_base_url()}/shutdown"
    stopped = False
    try:
        req = urllib.request.Request(url, method="POST", headers={"User-Agent": "MatchaBridgeClient"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                stopped = True
    except Exception:
        pass

    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if is_pid_alive(pid):
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
                else:
                    import signal
                    os.kill(pid, signal.SIGTERM)
            stopped = True
        except Exception:
            pass
        finally:
            try:
                PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass

    if stopped:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if get_bridge_status(base_url, timeout=0.2).get("unreachable"):
                break
            time.sleep(0.05)
    return stopped


_spawn_lock = threading.Lock()


def stop_bridge_server(base_url: str | None = None) -> bool:
    """Serialize shutdown with startup; never kill arbitrary port users."""
    with _spawn_lock:
        try:
            with FileLock(str(LOCK_FILE), timeout=10):
                return _stop_bridge_server_locked(base_url)
        except FileLockTimeout:
            logger.warning("[MatchaBridge] Startup is still in progress; shutdown deferred")
            return False


def _recorded_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        return pid if is_pid_alive(pid) else None
    except (OSError, ValueError):
        return None


def _spawn_bridge() -> subprocess.Popen:
    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "matcha_browser_bridge.log").open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve())],
            creationflags=creationflags, startupinfo=startupinfo,
            close_fds=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    return proc


def ensure_bridge_running(timeout: float = 50.0) -> bool:
    """A warming/busy daemon is not dead. Only confirmed mismatches are replaced."""
    base_url = get_bridge_base_url()
    deadline = time.monotonic() + timeout
    if is_bridge_ready(base_url):
        return True
    if not _spawn_lock.acquire(timeout=max(0, deadline - time.monotonic())):
        return False
    try:
        try:
            with FileLock(str(LOCK_FILE), timeout=max(0, deadline - time.monotonic())):
                # Both thread and cross-process locks cover the entire lifecycle.
                status = get_bridge_status(base_url)
                if status.get("ready") and _compatible(status):
                    return True
                if "version" in status and not _compatible(status):
                    logger.info("[MatchaBridge] Replacing daemon with outdated version or launch configuration")
                    if not _stop_bridge_server_locked(base_url):
                        return False
                    status = {}
                # Fatal startup errors need intervention, not an endless restart loop.
                if status.get("error"):
                    logger.warning("[MatchaBridge] Worker reported a startup error; see bridge log")
                    return False
                proc = None
                if not status.get("running") and not _recorded_pid():
                    try:
                        logger.info("[MatchaBridge] Launching browser worker at %s", base_url)
                        proc = _spawn_bridge()
                    except OSError as exc:
                        logger.warning("[MatchaBridge] Could not start worker (%s)", type(exc).__name__)
                        return False
                while time.monotonic() < deadline:
                    if proc is not None and proc.poll() is not None:
                        return False
                    status = get_bridge_status(base_url)
                    if status.get("ready") and _compatible(status):
                        return True
                    if status.get("error"):
                        return False
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                logger.warning("[MatchaBridge] Worker is not ready yet; leaving it running")
                return False
        except FileLockTimeout:
            return False
    finally:
        _spawn_lock.release()


class BridgeUnavailableError(RuntimeError):
    """Local browser/transport failure, with no claim about provider HTTP status."""


class BridgeProviderError(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.upstream_status = detail.get("status")
        self.endpoint = detail.get("endpoint", "unknown")
        self.access_blocked = self.upstream_status in (401, 403)
        message = f"MetaMatcha {self.endpoint} returned HTTP {self.upstream_status}"
        for key in ("mitigation", "request_id", "denial"):
            value = detail.get(key)
            if value:
                message += f"; {key}={str(value)[:160]}"
        retry_after = str(detail.get("retry_after", ""))
        if retry_after.isdigit():
            message += f"; retry-after={retry_after}s"
        super().__init__(message)


def fetch_bridge_quotes(
    chain: str,
    payload: dict[str, Any],
    aggregators: list[str],
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Fetch quotes via the Matcha browser bridge."""
    if not ensure_bridge_running(timeout=45.0):
        raise BridgeUnavailableError("Matcha bridge temporarily failed to become ready; see logs/matcha_browser_bridge.log")

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
                if isinstance(data.get("provider_error"), dict):
                    raise BridgeProviderError(data["provider_error"])
                if "error" in data:
                    raise BridgeUnavailableError("Matcha bridge temporarily failed: " + str(data["error"]))
                return data.get("quotes", {})
            raise RuntimeError(f"Matcha bridge HTTP {resp.status}")
    except BridgeProviderError:
        raise
    except BridgeUnavailableError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            err_data = json.loads(exc.read().decode("utf-8"))
            err_msg = err_data.get("error", str(exc))
        except Exception:
            err_msg = str(exc)
        raise BridgeUnavailableError(f"Matcha bridge temporarily failed (local-status={exc.code}): {err_msg}") from exc
    except Exception as exc:
        raise BridgeUnavailableError(f"Matcha bridge temporarily failed ({type(exc).__name__})") from exc


# ---------------------------------------------------------------------------
# Server / Daemon Implementation
# ---------------------------------------------------------------------------

QUOTE_SCRIPT = r"""async (args) => {
    const { chain, payload, aggregators } = args;
    const headers = { 'content-type': 'application/json', 'x-fetch-native': '1' };
    if (chain === 'ethereum' && payload.taker) headers['x-taker'] = payload.taker.toLowerCase();
    async function failure(response, endpoint) {
        const safe = name => {
            const value = response.headers.get(name) || '';
            return /^[a-zA-Z0-9._|:= -]{1,160}$/.test(value) ? value : '';
        };
        const body = (await response.text()).toLowerCase();
        const denial = ['vercel security checkpoint', 'kasada', 'forbidden', 'unauthorized', 'rate limit']
            .find(marker => body.includes(marker)) || '';
        return {status: response.status, endpoint, denial,
                mitigation: safe('x-vercel-mitigated'),
                request_id: safe('x-vercel-id') || safe('cf-ray'),
                retry_after: safe('retry-after')};
    }
    if (chain === 'ethereum') {
        try { await fetch('https://meta.matcha.xyz/api/gas?chainId=1', {signal: AbortSignal.timeout(8000)}); } catch(e) {}
    }
    const compRes = await fetch('https://meta.matcha.xyz/api/competitions', {
        method: 'POST', headers, body: JSON.stringify(payload), signal: AbortSignal.timeout(15000)
    });
    if (!compRes.ok) return {provider_error: await failure(compRes, '/api/competitions')};
    let comp;
    try { comp = await compRes.json(); }
    catch(e) { return {error: 'Competition response was not JSON'}; }
    const compId = comp && (comp.id || comp.competitionId);
    if (!compId) return {error: 'Competition response omitted competitionId'};
    const quotes = {};
    const failures = [];
    await Promise.all(aggregators.map(async agg => {
        try {
            const response = await fetch(`https://meta.matcha.xyz/api/quotes?aggregator=${encodeURIComponent(agg)}`, {
                method: 'POST', headers, body: JSON.stringify({competitionId: compId, aggregator: agg}),
                signal: AbortSignal.timeout(15000)
            });
            if (!response.ok) {
                const detail = await failure(response, '/api/quotes');
                failures.push(detail);
                quotes[agg] = {error: `HTTP ${response.status}`, provider_error: detail};
            } else {
                quotes[agg] = await response.json();
            }
        } catch(e) { quotes[agg] = {error: 'Quote transport or JSON parsing failed'}; }
    }));
    if (failures.length && !Object.values(quotes).some(q => q && !q.error)) {
        return {provider_error: failures.find(f => [401,403].includes(f.status)) || failures[0]};
    }
    return {competitionId: compId, quotes};
}"""


class _BridgeServerState:
    def __init__(self) -> None:
        from queue import Queue
        self.queue: Queue[Any] = Queue()
        self.ready_eth = threading.Event()
        self.ready_sol = threading.Event()
        self.is_running = True
        self.fatal_error: str | None = None
        self.proxy_url: str = os.getenv("MATCHA_PROXY", "").strip()
        self.configured_proxy = proxy_fingerprint(self.proxy_url)


def _run_playwright_worker(state: _BridgeServerState) -> None:
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError as exc:
        err_msg = f"playwright or playwright_stealth missing: {exc}"
        logger.error("[MatchaBridge] %s", err_msg)
        state.fatal_error = err_msg
        state.is_running = False
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

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(**launch_kwargs)
            except Exception as exc:
                err_text = f"Failed to launch Chromium: {exc}. Run 'playwright install chromium' to install browser binaries."
                logger.error("[MatchaBridge] %s", err_text)
                state.fatal_error = err_text
                state.is_running = False
                return

            try:
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

                    eth_ok = "checkpoint" not in t_eth and len(t_eth) > 0 and any(w in t_eth for w in ("matcha", "swap", "ethereum"))
                    sol_ok = "checkpoint" not in t_sol and len(t_sol) > 0 and any(w in t_sol for w in ("matcha", "swap", "solana"))

                    if eth_ok and sol_ok and poll_idx >= 2:
                        logger.info("[MatchaBridge] Both tabs cleared! (ETH: %s, SOL: %s)", t_eth, t_sol)
                        time.sleep(2.0)  # Allow Kasada runtime to settle
                        state.ready_eth.set()
                        state.ready_sol.set()
                        break

                # If proxy was used and failed to clear tabs, fallback directly without proxy
                if proxy_url and not (state.ready_eth.is_set() and state.ready_sol.is_set()):
                    logger.warning("[MatchaBridge] Proxy failed to clear browser tabs; retrying directly without proxy...")
                    state.proxy_url = ""
                    try:
                        context.close()
                        browser.close()
                    except Exception:
                        pass
                    launch_kwargs.pop("proxy", None)
                    launch_kwargs["args"] = list(launch_kwargs.get("args", [])) + ["--no-proxy-server"]
                    browser = pw.chromium.launch(**launch_kwargs)
                    context = browser.new_context(viewport={"width": 1280, "height": 800})
                    page_eth = context.new_page()
                    Stealth().apply_stealth_sync(page_eth)
                    page_sol = context.new_page()
                    Stealth().apply_stealth_sync(page_sol)
                    try:
                        page_eth.goto("https://meta.matcha.xyz/ethereum", wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass
                    try:
                        page_sol.goto("https://meta.matcha.xyz/solana", wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass
                    for poll_idx in range(30):
                        time.sleep(1)
                        t_eth = (page_eth.title() or "").lower()
                        t_sol = (page_sol.title() or "").lower()
                        eth_ok = "checkpoint" not in t_eth and len(t_eth) > 0 and any(w in t_eth for w in ("matcha", "swap", "ethereum"))
                        sol_ok = "checkpoint" not in t_sol and len(t_sol) > 0 and any(w in t_sol for w in ("matcha", "swap", "solana"))
                        if eth_ok and sol_ok and poll_idx >= 2:
                            logger.info("[MatchaBridge] Direct fallback cleared tabs! (ETH: %s, SOL: %s)", t_eth, t_sol)
                            time.sleep(2.0)
                            state.ready_eth.set()
                            state.ready_sol.set()
                            break

                last_health_check = time.monotonic()
                while state.is_running:
                    try:
                        task = state.queue.get(timeout=1.0)
                    except Exception:
                        # Periodic keep-alive
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
                            QUOTE_SCRIPT,
                            {"chain": chain, "payload": payload, "aggregators": aggregators},
                        )

                        if isinstance(res, dict) and res.get("provider_error"):
                            detail = res["provider_error"]
                            logger.warning("[MatchaBridge] %s | %s", chain, BridgeProviderError(detail))
                            # Only a real browser challenge warrants reloading, not a deny or 429.
                            if detail.get("mitigation") == "challenge":
                                try:
                                    page.reload(wait_until="domcontentloaded", timeout=25000)
                                except Exception:
                                    pass

                        elapsed = time.perf_counter() - t0
                        logger.info("[MatchaBridge] Quote [%s] fetched in %.1fms (aggregators: %s)", chain, elapsed * 1000, aggregators)
                        result_box["result"] = res
                    except Exception as exc:
                        logger.error("[MatchaBridge] Evaluate error on [%s]: %s", chain, exc)
                        result_box["error"] = str(exc)
                    finally:
                        event.set()
                        state.queue.task_done()
            finally:
                browser.close()
    except Exception as exc:
        state.fatal_error = f"Browser worker failed: {exc}"
        logger.exception("[MatchaBridge] %s", state.fatal_error)
    finally:
        state.is_running = False
        state.ready_eth.clear()
        state.ready_sol.clear()
        from queue import Empty

        while True:
            try:
                _, _, _, event, result_box = state.queue.get_nowait()
            except Empty:
                break
            result_box["error"] = state.fatal_error or "Browser worker stopped"
            event.set()
            state.queue.task_done()


def run_bridge_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the bridge HTTP service and Playwright worker."""
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    state = _BridgeServerState()

    class BridgeHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # Suppress access logs for high-frequency quoting

        def do_GET(self) -> None:
            if self.path == "/health":
                if state.fatal_error:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    try:
                        self.wfile.write(json.dumps({
                            "ready": False,
                            "error": state.fatal_error,
                            "version": BRIDGE_VERSION,
                            "configured_proxy": state.configured_proxy,
                            "connection": "proxy" if state.proxy_url else "direct",
                            "pid": os.getpid(),
                        }).encode("utf-8"))
                    except Exception:
                        pass
                    return

                ready = state.ready_eth.is_set() and state.ready_sol.is_set()
                status = 200 if ready else 503
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(json.dumps({
                        "ready": ready,
                        "running": state.is_running,
                        "version": BRIDGE_VERSION,
                        "configured_proxy": state.configured_proxy,
                        "connection": "proxy" if state.proxy_url else "direct",
                        "pid": os.getpid(),
                    }).encode("utf-8"))
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                    pass
            elif self.path == "/shutdown":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(b'{"status": "shutting down"}')
                except Exception:
                    pass
                state.is_running = False
                threading.Thread(target=server.shutdown, daemon=True).start()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path == "/shutdown":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(b'{"status": "shutting down"}')
                except Exception:
                    pass
                state.is_running = False
                threading.Thread(target=server.shutdown, daemon=True).start()
                return

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

    try:
        server = ThreadingHTTPServer((host, port), BridgeHandler)
    except OSError as exc:
        logger.warning(
            "[MatchaBridge] Cannot bind http://%s:%d: %s. An existing bridge instance may be active.",
            host,
            port,
            exc,
        )
        return

    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    worker_thread = threading.Thread(
        target=_run_playwright_worker,
        args=(state,),
        daemon=True,
        name="MatchaPlaywrightWorker",
    )
    worker_thread.start()

    logger.info("[MatchaBridge] Server running at http://%s:%d (PID %d)", host, port, os.getpid())
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        state.is_running = False
        server.server_close()
    finally:
        state.is_running = False
        server.server_close()
        try:
            if PID_FILE.exists():
                stored_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
                if stored_pid == os.getpid():
                    PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    port = get_bridge_port()
    host = os.getenv("MATCHA_BRIDGE_HOST", DEFAULT_HOST)
    run_bridge_server(host, port)
