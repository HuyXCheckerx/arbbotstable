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

    def test_dashboard_exposes_quick_access_and_log_controls(self):
        self.assertIn('id="copy-url-button"', web.HTML_TEMPLATE)
        self.assertIn('data-log-filter="error"', web.HTML_TEMPLATE)
        self.assertIn('id="copy-logs-button"', web.HTML_TEMPLATE)
        self.assertIn('id="session-mode"', web.HTML_TEMPLATE)
        self.assertIn('id="route-table-body"', web.HTML_TEMPLATE)

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

    def test_polygon_pyusd_pair_is_supported(self):
        self.assertIn("PYUSD/USDC", web.SUPPORTED_PAIRS["polygon"])
        self.assertNotIn("USDT/USDC", web.SUPPORTED_PAIRS["polygon"])

    def test_all_six_loan_counter_pairs_are_supported_on_both_sniper_chains(self):
        for chain in ("ethereum", "solana"):
            self.assertEqual(len(web.SUPPORTED_PAIRS[chain]), 6)
            for stable_from in ("USDC", "USDG", "PYUSD"):
                for stable_to in ("USDC", "USDG", "PYUSD"):
                    if stable_from != stable_to:
                        self.assertIn(
                            f"{stable_from}/{stable_to}",
                            web.SUPPORTED_PAIRS[chain],
                        )
            self.assertNotIn("USDT/USDC", web.SUPPORTED_PAIRS[chain])

    def test_ethereum_cross_token_pair_reaches_the_generic_engine(self):
        with patch(
            "web.subprocess.run",
            return_value=CompletedProcess([], 2, stdout="", stderr="ERROR: no route\n"),
        ) as run:
            web.run_arb_command("ethereum", "USDG/PYUSD", "quote", "1000", "1")

        command = run.call_args.args[0]
        self.assertTrue(command[1].endswith("eth_flash_arb_pyusd_usdc.py"))
        self.assertEqual(command[command.index("--loan-token") + 1], "USDG")
        self.assertEqual(
            command[command.index("--intermediate-token") + 1], "PYUSD"
        )
        self.assertEqual(command[command.index("--swap-order") + 1], "stable-first")

    def test_ethereum_dashboard_can_request_dex_first(self):
        with patch(
            "web.subprocess.run",
            return_value=CompletedProcess([], 2, stdout="", stderr="ERROR: no route\n"),
        ) as run:
            web.run_arb_command(
                "ethereum",
                "PYUSD/USDC",
                "quote",
                "1000",
                "1",
                "dex-first",
            )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--loan-token") + 1], "PYUSD")
        self.assertEqual(command[command.index("--intermediate-token") + 1], "USDC")
        self.assertEqual(command[command.index("--swap-order") + 1], "dex-first")

    def test_solana_cross_token_pair_reaches_both_engine_settings(self):
        with patch(
            "web.subprocess.run",
            return_value=CompletedProcess([], 2, stdout="", stderr="ERROR: no route\n"),
        ) as run:
            web.run_arb_command("solana", "PYUSD/USDG", "quote", "1000", "1")

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["SOL_FLASH_ARB_AMOUNT_PYUSD"], "1000")
        self.assertEqual(environment["SOL_FLASH_ARB_LOAN_TOKEN"], "PYUSD")
        self.assertEqual(environment["SOL_FLASH_ARB_INTERMEDIATE_TOKEN"], "USDG")
        self.assertEqual(environment["SOL_FLASH_ARB_SWAP_ORDER"], "stable-first")
        self.assertEqual(environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"], "false")
        self.assertEqual(environment["SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS"], "24")
        self.assertNotIn("SOL_FLASH_ARB_SLIPPAGE_BPS", environment)

    def test_solana_request_does_not_require_a_slippage_setting(self):
        validated = web._validated_request(
            {
                "chain": "solana",
                "pair": "PYUSD/USDG",
                "mode": "quote",
                "amount": "1000",
            }
        )

        self.assertEqual(validated[4], "0")

    def test_dashboard_route_summary_is_stable_first(self):
        summary = web.parse_output_summary(
            "",
            chain="ethereum",
            pair="PYUSD/USDC",
        )

        dex_first = web.parse_output_summary(
            "",
            chain="ethereum",
            pair="PYUSD/USDC",
            swap_order="dex-first",
        )
        self.assertEqual(
            dex_first["flow"],
            "PYUSD → USDC (MetaMatcha) → PYUSD (Stable.com)",
        )
        self.assertEqual(
            summary["flow"],
            "PYUSD → USDC (Stable.com) → PYUSD (MetaMatcha)",
        )

    def test_solana_dashboard_can_request_dex_first(self):
        with patch(
            "web.subprocess.run",
            return_value=CompletedProcess([], 2, stdout="", stderr="ERROR: no route\n"),
        ) as run:
            web.run_arb_command(
                "solana",
                "USDC/PYUSD",
                "quote",
                "1000",
                "1",
                "dex-first",
            )

        environment = run.call_args.kwargs["env"]
        command = run.call_args.args[0]
        self.assertEqual(environment["SOL_FLASH_ARB_LOAN_TOKEN"], "USDC")
        self.assertEqual(environment["SOL_FLASH_ARB_INTERMEDIATE_TOKEN"], "PYUSD")
        self.assertEqual(environment["SOL_FLASH_ARB_SWAP_ORDER"], "dex-first")
        self.assertEqual(command[command.index("--swap-order") + 1], "dex-first")

    def test_dashboard_orders_structured_stable_and_metamatcha_legs(self):
        summary = web.parse_output_summary(
            "",
            {
                "swapOrder": "stable-first",
                "loanToken": {"symbol": "PYUSD"},
                "intermediateToken": {"symbol": "USDC"},
                "stable": {
                    "sellToken": {"symbol": "PYUSD", "amount": "100"},
                    "buyToken": {"symbol": "USDC", "amount": "100.1"},
                },
                "matcha": {
                    "sellToken": {"symbol": "USDC", "amount": "100.1"},
                    "buyToken": {"symbol": "PYUSD", "amount": "100.2"},
                },
            },
            chain="ethereum",
            pair="PYUSD/USDC",
        )

        self.assertEqual(summary["leg1"], "100 PYUSD → 100.1 USDC (Stable.com)")
        self.assertEqual(summary["leg2"], "100.1 USDC → 100.2 PYUSD (MetaMatcha)")

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
        self.assertEqual(command[command.index("--pair") + 1], "USDT/USDC")

    def test_quant_logo_is_served_as_the_favicon(self):
        status, headers, body = self.request("/favicon.svg")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(ET.fromstring(body).tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn('href="/favicon.svg"', web.HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
