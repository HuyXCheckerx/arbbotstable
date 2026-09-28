import ast
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.engines import matcha_browser_bridge as bridge


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
        return patch.dict(sys.modules, {"playwright.sync_api": api, "playwright_stealth": stealth})

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
