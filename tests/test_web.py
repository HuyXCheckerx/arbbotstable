import http.client
import threading
import unittest
import xml.etree.ElementTree as ET
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

    def request(self, path, timeout=2):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request("GET", path)
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

    def test_state_failure_returns_service_unavailable(self):
        with patch("web.read_dashboard_state", side_effect=RuntimeError("database busy")):
            status, _, body = self.request("/api/state")

        self.assertEqual(status, 503)
        self.assertIn(b"temporarily unavailable", body)

    def test_browser_polling_is_sequential_and_time_bounded(self):
        self.assertIn("AbortController", web.HTML_TEMPLATE)
        self.assertNotIn("setInterval(refresh", web.HTML_TEMPLATE)

    def test_quant_logo_is_served_as_the_favicon(self):
        status, headers, body = self.request("/favicon.svg")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(ET.fromstring(body).tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn('href="/favicon.svg"', web.HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
