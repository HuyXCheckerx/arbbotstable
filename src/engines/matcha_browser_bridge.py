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

from filelock import FileLock

logger = logging.getLogger("matcha.bridge")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PID_FILE = PROJECT_ROOT / ".matcha_bridge.pid"
LOCK_FILE = PROJECT_ROOT / ".matcha_bridge.lock"
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


def stop_bridge_server(base_url: str | None = None) -> bool:
    """Request graceful shutdown of the background Matcha bridge daemon."""
    url = f"{base_url or get_bridge_base_url()}/shutdown"
    stopped = False
    try:
        req = urllib.request.Request(url, method="POST", headers={"User-Agent": "MatchaBridgeClient"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            if resp.status == 200:
                stopped = True
    except Exception:
        pass

    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if is_pid_alive(pid):
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
    return stopped


_spawn_lock = threading.Lock()
_last_spawn_attempt: float = 0.0
_SPAWN_COOLDOWN_SECONDS: float = 30.0


def ensure_bridge_running(timeout: float = 50.0) -> bool:
    """Ensures the background Matcha browser bridge server is running using process locks."""
    base_url = get_bridge_base_url()
    if is_bridge_ready(base_url, timeout=0.8):
        return True

    # 1. Check if a bridge process was already spawned and is currently alive in the OS
    existing_pid: int | None = None
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            existing_pid = None

    if existing_pid and is_pid_alive(existing_pid):
        logger.debug("[MatchaBridge] Bridge daemon PID %d is already active; waiting for readiness...", existing_pid)
        t0 = time.monotonic()
        wait_limit = min(timeout, 15.0)
        while time.monotonic() - t0 < wait_limit:
            st = get_bridge_status(base_url, timeout=1.0)
            if st.get("ready"):
                return True
            if st.get("error"):
                logger.error("[MatchaBridge] Running bridge daemon (PID %d) reported fatal error: %s", existing_pid, st["error"])
                return False
            time.sleep(0.5)
        logger.debug("[MatchaBridge] Bridge daemon PID %d is still warming up. Skipping duplicate spawn.", existing_pid)
        return False
    elif existing_pid:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # 2. Thread-level guard: avoid duplicate spawns from concurrent worker threads
    if not _spawn_lock.acquire(blocking=False):
        t0 = time.monotonic()
        while time.monotonic() - t0 < min(timeout, 10.0):
            if is_bridge_ready(base_url, timeout=1.0):
                return True
            time.sleep(0.5)
        return is_bridge_ready(base_url, timeout=1.0)

    try:
        if is_bridge_ready(base_url, timeout=0.8):
            return True

        # 3. Debounce: enforce cooldown between spawn attempts to prevent runaway spawning
        global _last_spawn_attempt
        now = time.monotonic()
        if (now - _last_spawn_attempt) < _SPAWN_COOLDOWN_SECONDS:
            logger.debug(
                "[MatchaBridge] Spawn cooldown active (%.1fs < %.1fs). Skipping spawn.",
                now - _last_spawn_attempt,
                _SPAWN_COOLDOWN_SECONDS,
            )
            return False
        _last_spawn_attempt = now

        try:
            lock = FileLock(str(LOCK_FILE), timeout=10)
        except Exception:
            lock = None

        def _spawn_and_wait() -> bool:
            if is_bridge_ready(base_url, timeout=0.8):
                return True

            if PID_FILE.exists():
                try:
                    p = int(PID_FILE.read_text(encoding="utf-8").strip())
                    if is_pid_alive(p):
                        logger.debug("[MatchaBridge] Daemon PID %d active inside lock. Skipping spawn.", p)
                        return is_bridge_ready(base_url, timeout=1.0)
                except Exception:
                    pass

            bridge_script = Path(__file__).resolve()
            logger.info("[MatchaBridge] Bridge daemon not detected at %s. Launching background worker...", base_url)

            creationflags = 0
            startupinfo = None
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE

            log_dir = PROJECT_ROOT / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "matcha_browser_bridge.log"
            log_file = open(log_path, "a", encoding="utf-8")

            try:
                proc = subprocess.Popen(
                    [sys.executable, str(bridge_script)],
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                    close_fds=(sys.platform != "win32"),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                )
                try:
                    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
                except Exception:
                    pass
            except Exception as exc:
                logger.warning("[MatchaBridge] Failed to spawn bridge process: %s", exc)
                return False

            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                if proc.poll() is not None:
                    logger.warning(
                        "[MatchaBridge] Bridge process PID %d exited prematurely with code %s (check %s)",
                        proc.pid,
                        proc.returncode,
                        log_path,
                    )
                    try:
                        PID_FILE.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return False

                st = get_bridge_status(base_url, timeout=1.0)
                if st.get("ready"):
                    logger.info("[MatchaBridge] Bridge daemon ready at %s (took %.1fs)", base_url, time.monotonic() - t0)
                    return True
                if st.get("error"):
                    logger.error("[MatchaBridge] Bridge daemon reported fatal error: %s", st["error"])
                    return False

                time.sleep(0.5)

            logger.warning("[MatchaBridge] Timed out waiting for bridge daemon to be ready (%ss)", timeout)
            return False

        if lock:
            with lock:
                return _spawn_and_wait()
        return _spawn_and_wait()
    finally:
        _spawn_lock.release()


def fetch_bridge_quotes(
    chain: str,
    payload: dict[str, Any],
    aggregators: list[str],
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Fetch quotes via the Matcha browser bridge."""
    if not ensure_bridge_running(timeout=45.0):
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
        self.fatal_error: str | None = None


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

                    eth_ok = "checkpoint" not in t_eth and len(t_eth) > 0
                    sol_ok = "checkpoint" not in t_sol and len(t_sol) > 0

                    if eth_ok and sol_ok and poll_idx >= 2:
                        logger.info("[MatchaBridge] Both tabs cleared! (ETH: %s, SOL: %s)", t_eth, t_sol)
                        time.sleep(2.0)  # Allow Kasada runtime to settle
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

                        # Auto-heal if challenged
                        if isinstance(res, dict) and "error" in res:
                            err_str = str(res.get("error", "")).lower()
                            if any(x in err_str for x in ("checkpoint", "403", "429", "challenge")):
                                logger.warning("[MatchaBridge] %s tab hit checkpoint (%s). Auto-reloading...", chain, err_str[:80])
                                try:
                                    page.reload(wait_until="domcontentloaded", timeout=25000)
                                    time.sleep(2.0)
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
    from http.server import HTTPServer, BaseHTTPRequestHandler

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
                        self.wfile.write(json.dumps({"ready": False, "error": state.fatal_error}).encode("utf-8"))
                    except Exception:
                        pass
                    return

                ready = state.ready_eth.is_set() and state.ready_sol.is_set()
                status = 200 if ready else 503
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(json.dumps({"ready": ready, "running": state.is_running}).encode("utf-8"))
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
        server = HTTPServer((host, port), BridgeHandler)
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
