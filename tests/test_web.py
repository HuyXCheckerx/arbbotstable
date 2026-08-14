import http.client
import json
import threading
import unittest
import xml.etree.ElementTree as ET
from subprocess import CompletedProcess
from unittest.mock import patch
import sys
from pathlib import Path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
for sub in ("core", "recovery", "engines", "web", "deployers"):
    subpath = str(SRC_DIR / sub)
    if subpath not in sys.path:
        sys.path.insert(0, subpath)

import web
from src.web import app as supervisor
from state_store import default_state


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.server = web.DashboardServer(("127.0.0.1", 0), web.DashboardHandler)
        self.host, self.port = self.server.server_address
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def request(self, path, timeout=2, method="GET", body=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            encoded = json.dumps(body) if body is not None else None
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_homepage_responds_while_state_request_is_blocked(self):
        state_read_started = threading.Event()
        release_state_read = threading.Event()
        state_result = []

        def delayed_state(*args, **kwargs):
            state_read_started.set()
            release_state_read.wait(timeout=2)
            return default_state()

        def request_state():
            state_result.append(self.request("/api/state", timeout=3)[0])

        with patch("web.read_dashboard_state", side_effect=delayed_state):
            state_thread = threading.Thread(target=request_state)
            state_thread.start()
            self.assertTrue(state_read_started.wait(timeout=1))

            status, headers, body = self.request("/", timeout=1)
            self.assertEqual(status, 200)
            self.assertEqual(int(headers["Content-Length"]), len(body))

            release_state_read.set()
            state_thread.join(timeout=3)

        self.assertEqual(state_result, [200])

    def test_supervisor_uses_canonical_script_locations(self):
        self.assertEqual(supervisor.PROJECT_ROOT, Path(__file__).resolve().parents[1])
        self.assertTrue(all(path.is_file() for path in supervisor.PROCESS_SCRIPTS.values()))

    def test_state_failure_returns_service_unavailable(self):
        with patch("web.read_dashboard_state", side_effect=RuntimeError("database busy")):
            status, _, body = self.request("/api/state")

        self.assertEqual(status, 503)
        self.assertIn(b"temporarily unavailable", body)

    def test_browser_polling_is_sequential_and_time_bounded(self):
        status, headers, javascript = self.request("/static/dashboard.js")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertIn(b"AbortController", javascript)
        self.assertNotIn(b"setInterval(refresh", javascript)
        self.assertIn('src="/static/dashboard.js" defer', web.HTML_TEMPLATE)
        self.assertNotIn("<script>", web.HTML_TEMPLATE)

    def test_styles_are_served_as_a_separate_asset(self):
        status, headers, stylesheet = self.request("/static/dashboard.css")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertIn(b"@media (max-width: 820px)", stylesheet)
        self.assertIn('href="/static/dashboard.css"', web.HTML_TEMPLATE)
        self.assertNotIn("<style>", web.HTML_TEMPLATE)

    def test_homepage_sets_a_restrictive_content_security_policy(self):
        status, headers, _ = self.request("/")

        self.assertEqual(status, 200)
        policy = headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_live_run_requires_explicit_confirmation(self):
        status, _, body = self.request(
            "/api/run",
            method="POST",
            body={
                "chain": "ethereum",
                "pair": "PYUSD/USDC",
                "mode": "live",
                "amount": "1000",
                "slippageBps": "0",
            },
        )

        self.assertEqual(status, 403)
        self.assertIn(b"EXECUTE LIVE ARB", body)

    def test_rejects_a_pair_that_the_selected_chain_cannot_execute(self):
        status, _, body = self.request(
            "/api/run",
            method="POST",
            body={
                "chain": "polygon",
                "pair": "PYUSD/USDC",
                "mode": "quote",
                "amount": "1000",
                "slippageBps": "0",
            },
        )

        self.assertEqual(status, 400)
        self.assertIn(b"not supported", body)

    def test_failed_engine_process_is_not_reported_as_success(self):
        with patch(
            "web.subprocess.run",
            return_value=CompletedProcess([], 2, stdout="", stderr="ERROR: no route\n"),
        ) as run:
            success, error, _ = web.run_arb_command(
                "polygon",
                "USDT/USDC",
                "quote",
                "1234.5",
                "7",
            )

        self.assertFalse(success)
        self.assertEqual(error, "no route")
        command = run.call_args.args[0]
        self.assertIn("--amount", command)
        self.assertEqual(command[command.index("--amount") + 1], "1234.5")
        self.assertEqual(command[command.index("--slippage-bps") + 1], "7")

    def test_quant_logo_is_served_as_the_favicon(self):
        status, headers, body = self.request("/favicon.svg")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(ET.fromstring(body).tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn('href="/favicon.svg"', web.HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
