import io
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rich.console import Console

from scripts.preview_sniper_terminal import sample_snapshot
from src.engines.sniper_terminal import (
    EventHandler, clean_text, profit_cell, render_dashboard, terminal_dashboard,
)


class TerminalDashboardTests(unittest.TestCase):
    def render(self, snapshot, width=150, height=32, elapsed=0):
        output = io.StringIO()
        console = Console(file=output, width=width, height=height, color_system=None)
        console.print(render_dashboard(snapshot, width=width, height=height, elapsed=elapsed,
                                       events=[("12:00:00", logging.INFO, "Starting"),
                                               ("12:00:01", logging.WARNING, "RPC waiting")]))
        return output.getvalue()

    def unique_routes(self):
        snapshot = sample_snapshot()
        snapshot["recent_results"] = []
        for i, row in enumerate(snapshot["routes"].values()):
            row["pair"] = f"Q{i:02d}/USDC"
        return snapshot

    def test_wide_frame_contains_every_route_without_overflow(self):
        snapshot = self.unique_routes()
        rendered = self.render(snapshot)
        for row in snapshot["routes"].values():
            self.assertEqual(rendered.count(row["pair"]), 1)
        self.assertIn("ETHEREUM", rendered)
        self.assertIn("SOLANA", rendered)
        self.assertIn("All routes visible", rendered)
        self.assertLessEqual(len(rendered.splitlines()), 32)
        self.assertTrue(all(len(line) <= 150 for line in rendered.splitlines()))

    def test_small_window_pages_cover_every_route_once(self):
        snapshot = self.unique_routes()
        frames = [self.render(snapshot, width=80, height=24, elapsed=elapsed) for elapsed in (0, 8, 16)]
        for index, frame in enumerate(frames):
            self.assertIn(f"Page {index + 1}/3", frame)
            self.assertLessEqual(len(frame.splitlines()), 24)
            self.assertTrue(all(len(line) <= 80 for line in frame.splitlines()))
        for row in snapshot["routes"].values():
            self.assertEqual("".join(frames).count(row["pair"]), 1)

    def test_resizing_changes_layout_without_losing_routes(self):
        snapshot = self.unique_routes()
        compact = self.render(snapshot, width=60, height=40)
        self.assertIn("Gross", compact)
        self.assertNotIn(" Net ", compact)
        for row in snapshot["routes"].values():
            self.assertEqual(compact.count(row["pair"]), 1)
        self.assertTrue(all(len(line) <= 60 for line in compact.splitlines()))

    def test_profit_format_does_not_turn_missing_or_tiny_values_into_zero(self):
        self.assertEqual(profit_cell(None).plain, "--")
        self.assertEqual(profit_cell("NaN").plain, "--")
        self.assertEqual(profit_cell("0").plain, "0.0000")
        self.assertEqual(profit_cell("0.000001").plain, "+<0.0001")
        self.assertEqual(profit_cell("-0.000001").plain, "-<0.0001")
        self.assertEqual(profit_cell("5", stale=True).style, "dim")

    def test_cooldown_does_not_hide_confirmed_or_unresolved_transaction(self):
        snapshot = sample_snapshot()
        row = next(iter(snapshot["routes"].values()))
        row.update(state="CONFIRMED", cooldown_until="2099-01-01T00:00:00+00:00")
        self.assertIn("CONFIRMED", self.render(snapshot))
        row.update(state="STOPPED", category="submitted")
        self.assertIn("PENDING", self.render(snapshot))

    def test_auto_redirected_output_leaves_logging_untouched(self):
        logger, feed = Mock(), Mock()
        with patch("sys.stdout", io.StringIO()):
            with terminal_dashboard(feed, logger, "auto"):
                pass
        logger.addHandler.assert_not_called()
        feed.snapshot.assert_not_called()

    @patch.dict(os.environ, {"TERM": "xterm"})
    def test_handlers_and_file_logging_survive_display_exit_with_exception(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=True, width=150, height=32)
        feed = Mock(snapshot=Mock(return_value=sample_snapshot()))
        logger = logging.getLogger("test.sniper.terminal")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        with tempfile.TemporaryDirectory() as temp, patch("sys.stdout", output):
            log_path = Path(temp) / "sniper.log"
            file_handler = RotatingFileHandler(log_path)
            stream_handler = logging.StreamHandler(output)
            logger.handlers = [stream_handler, file_handler]
            try:
                with patch("rich.console.Console", return_value=console):
                    with self.assertRaisesRegex(RuntimeError, "test shutdown"):
                        with terminal_dashboard(feed, logger, "table"):
                            self.assertNotIn(stream_handler, logger.handlers)
                            logger.warning("Persistent event")
                            raise RuntimeError("test shutdown")
                self.assertCountEqual(logger.handlers, [stream_handler, file_handler])
                self.assertIn("Persistent event", log_path.read_text())
                self.assertIn("ARBBOT", output.getvalue())
            finally:
                file_handler.close()
                logger.handlers.clear()

    def test_provider_text_cannot_inject_terminal_codes_or_proxy_secrets(self):
        value = clean_text("\x1b[2Jbad https://user:secret@proxy.test:80/path?key=token\nnext")
        self.assertNotIn("\x1b", value)
        self.assertNotIn("secret", value)
        self.assertNotIn("token", value)
        handler = EventHandler()
        handler.emit(logging.LogRecord("test", logging.WARNING, "", 0, "PAUSE | gas too high", (), None))
        self.assertIn("gas too high", handler.latest()[0][2])


if __name__ == "__main__":
    unittest.main()
