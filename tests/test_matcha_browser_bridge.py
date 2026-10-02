import ast
import io
import json
import multiprocessing
import tempfile
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.engines import matcha_browser_bridge as bridge


def concurrent_process_start(directory, launched, ready, count, results):
    # Real file lock across independent Python processes; no browsers or trading.
    def status(*args, **kwargs):
        return {"version": bridge.BRIDGE_VERSION, "configured_proxy": bridge.proxy_fingerprint(),
                "running": launched.is_set(), "ready": ready.is_set()} if launched.is_set() else {"unreachable": True}

    def spawn():
        with count.get_lock():
            count.value += 1
        launched.set()
        threading.Timer(0.15, ready.set).start()
        return SimpleNamespace(poll=lambda: None)

    with patch.object(bridge, "LOCK_FILE", Path(directory) / "bridge.lock"), \
            patch.object(bridge, "get_bridge_status", side_effect=status), \
            patch.object(bridge, "_recorded_pid", return_value=None), \
            patch.object(bridge, "_spawn_bridge", side_effect=spawn):
        results.put(bridge.ensure_bridge_running(timeout=5))


class BrowserBridgeTests(unittest.TestCase):
    def runtime(self, browser, active):
        @contextmanager
        def sync_playwright():
            active.append(True)
            try:
                yield SimpleNamespace(chromium=SimpleNamespace(launch=Mock(return_value=browser)))
            finally:
                active.pop()

        api = ModuleType("playwright.sync_api")
        api.sync_playwright = sync_playwright
        stealth = ModuleType("playwright_stealth")
        stealth.Stealth = Mock()
        @contextmanager
        def isolated_runtime():
            # Browser unit tests must not start the real background cookie solver.
            from src.engines import matcha_cookie_manager as cookies
            with patch.dict(sys.modules, {"playwright.sync_api": api, "playwright_stealth": stealth}), \
                    patch.object(cookies, "get_valid_cookies", return_value=[]):
                yield
        return isolated_runtime()

    def ready_status(self, **overrides):
        return {"version": bridge.BRIDGE_VERSION, "configured_proxy": bridge.proxy_fingerprint(),
                "running": True, "ready": True, **overrides}

    def test_six_threads_start_only_one_daemon(self):
        launched = threading.Event()
        ready = threading.Event()

        def status(*args, **kwargs):
            return self.ready_status(ready=ready.is_set()) if launched.is_set() else {"unreachable": True}

        def spawn():
            launched.set()
            threading.Timer(0.1, ready.set).start()
            return SimpleNamespace(poll=lambda: None)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bridge, "LOCK_FILE", Path(directory) / "lock"), \
                patch.object(bridge, "get_bridge_status", side_effect=status), \
                patch.object(bridge, "_recorded_pid", return_value=None), \
                patch.object(bridge, "_spawn_bridge", side_effect=spawn) as start, \
                patch.object(bridge, "_stop_bridge_server_locked") as stop:
            with ThreadPoolExecutor(max_workers=6) as pool:
                self.assertTrue(all(pool.map(lambda _: bridge.ensure_bridge_running(2), range(6))))
            start.assert_called_once()
            stop.assert_not_called()

    def test_processes_share_startup_lock(self):
        ctx = multiprocessing.get_context("spawn")
        launched, ready, count, results = ctx.Event(), ctx.Event(), ctx.Value("i", 0), ctx.Queue()
        with tempfile.TemporaryDirectory() as directory:
            processes = [ctx.Process(target=concurrent_process_start,
                         args=(directory, launched, ready, count, results)) for _ in range(3)]
            try:
                for process in processes:
                    process.start()
                self.assertEqual([results.get(timeout=10) for _ in processes], [True] * 3)
                self.assertEqual(count.value, 1)
            finally:
                for process in processes:
                    process.join(timeout=5)
                    if process.is_alive():
                        process.terminate()
                        process.join()
                results.close()

    def test_warming_busy_or_unreachable_live_daemon_is_never_killed(self):
        for status in (self.ready_status(ready=False), {"unreachable": True}):
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(bridge, "LOCK_FILE", Path(directory) / "lock"), \
                    patch.object(bridge, "get_bridge_status", return_value=status), \
                    patch.object(bridge, "_recorded_pid", return_value=123), \
                    patch.object(bridge, "_spawn_bridge") as start, \
                    patch.object(bridge, "_stop_bridge_server_locked") as stop:
                self.assertFalse(bridge.ensure_bridge_running(timeout=0.03))
                start.assert_not_called()
                stop.assert_not_called()

    def test_confirmed_version_mismatch_replaced_under_file_lock(self):
        replaced = threading.Event()

        def status(*args, **kwargs):
            return self.ready_status() if replaced.is_set() else self.ready_status(version=0)

        def stop(*args):
            from filelock import FileLock, Timeout
            with self.assertRaises(Timeout):
                with FileLock(str(bridge.LOCK_FILE), timeout=0):
                    pass
            replaced.set()
            return True

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bridge, "LOCK_FILE", Path(directory) / "lock"), \
                patch.object(bridge, "get_bridge_status", side_effect=status), \
                patch.object(bridge, "_recorded_pid", return_value=None), \
                patch.object(bridge, "_spawn_bridge", return_value=SimpleNamespace(poll=lambda: None)) as start, \
                patch.object(bridge, "_stop_bridge_server_locked", side_effect=stop):
            self.assertTrue(bridge.ensure_bridge_running(1))
            start.assert_called_once()

    def test_proxy_fallback_does_not_invalidate_launch_configuration(self):
        with patch.dict("os.environ", {"MATCHA_PROXY": "http://user:secret@proxy.test:80"}):
            state = bridge._BridgeServerState()
            state.proxy_url = ""
            self.assertTrue(bridge._compatible({"version": bridge.BRIDGE_VERSION,
                                               "configured_proxy": state.configured_proxy}))
            self.assertNotIn("secret", state.configured_proxy)

    def test_cookie_direct_fallback_preserves_proxy_and_warming_daemon(self):
        from src.engines import matcha_cookie_manager as cookies
        proxy = "http://proxy.test:80"
        clearance = [{"name": "_vcrcs", "value": "test"}]
        browser = Mock()
        context = browser.new_context.return_value
        context.cookies.side_effect = [[]] * 31 + [clearance, clearance]
        context.new_page.return_value.title.side_effect = ["Security checkpoint"] * 30 + ["MetaMatcha"]
        with self.runtime(browser, []), patch.object(cookies.time, "sleep"), \
                patch.dict("os.environ", {"MATCHA_PROXY": proxy, "PROXYISP_API_KEY": ""}), \
                patch.object(bridge, "stop_bridge_server") as stop:
            self.assertEqual(cookies._solve_challenge(), clearance)
            self.assertEqual(bridge.proxy_fingerprint(), bridge.proxy_fingerprint(proxy))
            stop.assert_not_called()
        self.assertEqual(browser.close.call_count, 2)

    def test_proxy_fallback_warmup_is_not_recycled_after_150_seconds(self):
        status = self.ready_status(ready=False, uptime=200.0)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bridge, "LOCK_FILE", Path(directory) / "lock"), \
                patch.object(bridge, "get_bridge_status", return_value=status), \
                patch.object(bridge, "_recorded_pid", return_value=123), \
                patch.object(bridge, "_spawn_bridge") as start, \
                patch.object(bridge, "_stop_bridge_server_locked") as stop, \
                patch.object(bridge.time, "monotonic", side_effect=[0, 0, 0, 0, 6]):
            self.assertFalse(bridge.ensure_bridge_running(timeout=5))
            start.assert_not_called()
            stop.assert_not_called()

    def test_startup_failure_is_not_an_http_403(self):
        with patch.object(bridge, "ensure_bridge_running", return_value=False):
            with self.assertRaises(bridge.BridgeUnavailableError) as caught:
                bridge.fetch_bridge_quotes("ethereum", {}, ["0x"])
            self.assertNotIn("403", str(caught.exception))
            self.assertNotIn("challenge", str(caught.exception))

    def test_real_upstream_status_survives_client(self):
        for status in (403, 429, 500):
            response = Mock()
            response.status = 200
            response.read.return_value = json.dumps({"provider_error": {
                "status": status, "endpoint": "/api/competitions", "mitigation": "deny" if status == 403 else "",
            }}).encode()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            with patch.object(bridge, "ensure_bridge_running", return_value=True), \
                    patch.object(bridge.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(bridge.BridgeProviderError) as caught:
                    bridge.fetch_bridge_quotes("ethereum", {}, ["0x"])
                self.assertEqual(caught.exception.upstream_status, status)
                self.assertEqual(caught.exception.access_blocked, status == 403)

    def test_eth_client_does_not_invent_403_from_local_failure_or_429(self):
        from src.engines import eth_flash_arb as eth
        client = eth.MatchaClient(object.__new__(eth.HttpJsonClient))
        errors = [bridge.BridgeUnavailableError("daemon failed to clear challenges"),
                  bridge.BridgeProviderError({"status": 429, "endpoint": "/api/competitions"})]
        for error in errors:
            with patch.object(client, "gas_price", return_value=1), \
                    patch.object(bridge, "fetch_bridge_quotes", side_effect=error), \
                    patch.dict("os.environ", {"MATCHA_USE_BRIDGE": "true"}):
                with self.assertRaises(type(error)) as caught:
                    client.quotes("0x" + "1" * 40, 1000000, 10, ["0x"])
                self.assertNotIn("HTTP 403", str(caught.exception))

    def test_health_responds_while_quote_is_waiting_on_browser(self):
        from http.server import ThreadingHTTPServer
        created, evaluating, release = threading.Event(), threading.Event(), threading.Event()
        servers = []

        def server_factory(*args, **kwargs):
            server = ThreadingHTTPServer(*args, **kwargs)
            servers.append(server)
            created.set()
            return server

        def worker(state):
            state.ready_eth.set()
            state.ready_sol.set()
            chain, payload, aggregators, event, result = state.queue.get(timeout=5)
            evaluating.set()
            release.wait(timeout=5)
            result["result"] = {"quotes": {"0x": {"ok": True}}}
            event.set()

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bridge, "PID_FILE", Path(directory) / "pid"), \
                patch.object(bridge, "_run_playwright_worker", side_effect=worker), \
                patch("http.server.ThreadingHTTPServer", side_effect=server_factory):
            thread = threading.Thread(target=bridge.run_bridge_server, args=("127.0.0.1", 0), daemon=True)
            thread.start()
            self.assertTrue(created.wait(3))
            server = servers[0]
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    req = urllib.request.Request(url + "/ethereum/quote", data=b'{}', method="POST")
                    def quote():
                        with urllib.request.urlopen(req, timeout=5) as response:
                            return response.status
                    future = pool.submit(quote)
                    try:
                        self.assertTrue(evaluating.wait(3))
                        self.assertTrue(bridge.get_bridge_status(url, timeout=0.5)["ready"])
                    finally:
                        release.set()
                    self.assertEqual(future.result(timeout=3), 200)
            finally:
                release.set()
                server.shutdown()
                thread.join(timeout=3)

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser-fetch contract test")
    def test_browser_fetch_preserves_competition_and_aggregator_status(self):
        script = "const quote = " + bridge.QUOTE_SCRIPT + ";" + r"""
        const results = [];
        for (const [competitionStatus, quoteStatus] of [[403,200], [200,429], [200,200]]) {
            globalThis.fetch = async url => {
                if (url.includes('/api/gas')) return new Response('{}');
                if (url.includes('/api/competitions')) return new Response(
                    competitionStatus === 200 ? '{"id":"test"}' : '{"error":"Forbidden"}',
                    {status:competitionStatus, headers:{'x-vercel-id':'test-id'}});
                return new Response(quoteStatus === 200 ? '{"buyAmount":"1"}' : 'rate limit',
                    {status:quoteStatus, headers:{'retry-after':'30'}});
            };
            results.push(await quote({chain:'ethereum', payload:{}, aggregators:['0x']}));
        }
        console.log(JSON.stringify(results));
        """
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                capture_output=True, text=True, check=True)
        denied, limited, success = json.loads(result.stdout)
        self.assertEqual(denied["provider_error"]["status"], 403)
        self.assertEqual(denied["provider_error"]["denial"], "forbidden")
        self.assertEqual(limited["provider_error"]["endpoint"], "/api/quotes")
        self.assertEqual(limited["provider_error"]["status"], 429)
        self.assertIn("retry-after=30s", str(bridge.BridgeProviderError(limited["provider_error"])))
        self.assertEqual(success["quotes"]["0x"]["buyAmount"], "1")

    def test_all_production_python_modules_parse(self):
        root = Path(__file__).resolve().parents[1] / "src"
        for path in root.rglob("*.py"):
            with self.subTest(path=str(path.relative_to(root))):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_browser_stays_alive_through_warmup_and_quote_then_closes(self):
        state = bridge._BridgeServerState()
        event, result = threading.Event(), {}
        state.queue.put(("ethereum", {"taker": "test"}, ["0x"], event, result))
        active = []
        page = Mock()
        browser = Mock()
        browser.new_context.return_value.new_page.return_value = page

        def require_active(*args, **kwargs):
            self.assertTrue(active, "browser used after leaving Playwright context")
            return "MetaMatcha"

        def evaluate(*args):
            require_active()
            self.assertTrue(state.ready_eth.is_set())
            state.is_running = False
            return {"quotes": {"0x": {"ok": True}}}

        page.goto.side_effect = require_active
        page.title.side_effect = require_active
        page.evaluate.side_effect = evaluate
        browser.close.side_effect = require_active
        with self.runtime(browser, active), patch.object(bridge.time, "sleep"), patch.dict("os.environ", {"MATCHA_PROXY": ""}):
            bridge._run_playwright_worker(state)

        self.assertTrue(event.is_set())
        self.assertEqual(result["result"], {"quotes": {"0x": {"ok": True}}})
        self.assertEqual(state.queue.unfinished_tasks, 0)
        browser.close.assert_called_once()
        self.assertFalse(active)
        self.assertFalse(state.ready_eth.is_set())
        self.assertFalse(state.ready_sol.is_set())

    def test_uncleared_tabs_fail_explicitly_instead_of_idling_forever(self):
        state = bridge._BridgeServerState()
        browser = Mock()
        page = browser.new_context.return_value.new_page.return_value
        page.title.return_value = "Security checkpoint"
        with self.runtime(browser, []), patch.object(bridge.time, "sleep"), \
                patch.dict("os.environ", {"MATCHA_PROXY": ""}), self.assertLogs(bridge.logger, level="ERROR"):
            bridge._run_playwright_worker(state)
        self.assertFalse(state.is_running)
        self.assertIn("warm-up exhausted", state.fatal_error)
        page.evaluate.assert_not_called()
        browser.close.assert_called_once()

    def test_warmup_failure_records_checkpoint_and_screenshots(self):
        page = Mock()
        page.title.return_value = "Vercel Security Checkpoint"
        page.locator.return_value.inner_text.return_value = "Failed to verify your browser\nCode 21"
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bridge, "PROJECT_ROOT", Path(directory)), \
                self.assertLogs(bridge.logger, level="WARNING") as logs:
            detail = bridge._record_warmup_failure(page, page, "direct")
            self.assertIn("Code 21", detail)
            self.assertIn("Vercel Security Checkpoint", logs.output[0])
            self.assertEqual(page.screenshot.call_count, 2)
            self.assertEqual(Path(page.screenshot.call_args.kwargs["path"]).name,
                             "matcha-warmup-direct-solana.png")

    def test_expired_queued_quote_is_skipped_before_next_live_quote(self):
        state = bridge._BridgeServerState()
        expired_event, expired = threading.Event(), {"deadline": 0.0}
        live_event, live = threading.Event(), {}
        state.queue.put(("ethereum", {"old": True}, ["0x"], expired_event, expired))
        state.queue.put(("solana", {"live": True}, ["Jupiter"], live_event, live))
        browser = Mock()
        page = browser.new_context.return_value.new_page.return_value
        page.title.return_value = "MetaMatcha"
        def evaluate(*args):
            state.is_running = False
            return {"quotes": {}}
        page.evaluate.side_effect = evaluate
        with self.runtime(browser, []), patch.object(bridge.time, "sleep"), \
                patch.dict("os.environ", {"MATCHA_PROXY": ""}):
            bridge._run_playwright_worker(state)
        self.assertTrue(expired_event.is_set())
        self.assertIn("expired in queue", expired["error"])
        self.assertTrue(live_event.is_set())
        page.evaluate.assert_called_once()
        self.assertEqual(page.evaluate.call_args.args[1]["payload"], {"live": True})
        self.assertEqual(state.queue.unfinished_tasks, 0)

    def test_startup_failure_reports_error_and_releases_waiters(self):
        state = bridge._BridgeServerState()
        event, result = threading.Event(), {}
        state.queue.put(("solana", {}, [], event, result))
        browser = Mock()
        browser.new_context.side_effect = RuntimeError("context failed")
        with self.runtime(browser, []), patch.dict("os.environ", {"MATCHA_PROXY": ""}), self.assertLogs(bridge.logger, level="ERROR"):
            bridge._run_playwright_worker(state)
        self.assertFalse(state.is_running)
        self.assertIn("context failed", state.fatal_error)
        self.assertTrue(event.is_set())
        self.assertIn("context failed", result["error"])
        self.assertEqual(state.queue.unfinished_tasks, 0)
        browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
