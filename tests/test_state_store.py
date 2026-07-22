import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from state_store import (
    BotStateStore,
    default_state,
    read_daily_profit,
    read_state,
)


class BotStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "bot_state.db"
        self.store = BotStateStore(self.db_path)
        self.store.start_session("wallet-address")

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_records_net_pnl_and_exact_lamport_decrease(self):
        before = {
            "usdc": 100.0,
            "usdg": 100.0,
            "pyusd": 100.0,
            "usdt": 0.0,
            "sol_lamports": 1_000_000_000,
            "sol_price": 100.0,
        }
        after = {
            "usdc": 300.5,
            "usdg": 0.0,
            "pyusd": 0.0,
            "usdt": 0.0,
            "sol_lamports": 999_990_000,
            "sol_price": 100.0,
        }

        record = self.store.record_attempt(
            "USDC -> USDG -> USDC", 100, 0.6, before, after, True
        )

        self.assertEqual(record["sol_consumed_lamports"], 10_000)
        self.assertAlmostEqual(record["sol_consumed"], 0.00001)
        self.assertAlmostEqual(record["sol_cost_usd"], 0.001)
        self.assertAlmostEqual(record["stablecoin_change_usd"], 0.5)
        self.assertAlmostEqual(record["realized_pnl_usd"], 0.499)

        state = read_state(self.db_path)
        self.assertEqual(state["performance"]["total_arbs"], 1)
        self.assertEqual(state["performance"]["total_attempts"], 1)
        self.assertAlmostEqual(state["performance"]["total_sol_consumed"], 0.00001)
        self.assertAlmostEqual(
            self.store.estimated_execution_cost_usd(default_cost=0.0001),
            0.001,
        )

    def test_failed_attempt_counts_cost_but_not_arb(self):
        before = {
            "usdc": 10.0,
            "usdg": 0.0,
            "pyusd": 0.0,
            "usdt": 0.0,
            "sol_lamports": 50_000,
            "sol_price": 80.0,
        }
        after = dict(before, sol_lamports=45_000)

        record = self.store.record_attempt(
            "USDC -> USDG", 10, 0.2, before, after, False, "rejected"
        )

        self.assertLess(record["realized_pnl_usd"], 0)
        state = read_state(self.db_path)
        self.assertEqual(state["performance"]["total_arbs"], 0)
        self.assertEqual(state["performance"]["total_attempts"], 1)

    def test_stranded_usdt_is_counted_in_stablecoin_value(self):
        before = {
            "usdc": 100.0,
            "usdg": 0.0,
            "pyusd": 0.0,
            "usdt": 0.0,
            "sol_lamports": 1_000_000_000,
            "sol_price": 100.0,
        }
        after = {
            "usdc": 0.0,
            "usdg": 0.0,
            "pyusd": 0.0,
            "usdt": 100.0,
            "sol_lamports": 999_990_000,
            "sol_price": 100.0,
        }

        record = self.store.record_attempt(
            "USDC -> USDT", 100, 0.0, before, after, False, "awaiting recovery"
        )

        self.assertAlmostEqual(record["stablecoin_change_usd"], -0.1)
        self.assertAlmostEqual(record["realized_pnl_usd"], -0.101)

    def test_incomplete_snapshot_cannot_corrupt_pnl(self):
        before = {
            "usdc": 50_000.0,
            "usdg": 0.0,
            "pyusd": 0.0,
            "usdt": 0.0,
            "sol_lamports": 1_000_000_000,
            "sol_price": 80.0,
        }
        after = dict(before, usdc=None, sol_lamports=999_990_000)

        with self.assertRaisesRegex(ValueError, "incomplete after snapshot: usdc"):
            self.store.record_attempt(
                "USDC -> USDG -> USDC", 50_000, 0.2, before, after, True
            )

        state = read_state(self.db_path)
        self.assertEqual(state["performance"]["total_attempts"], 0)
        self.assertEqual(state["performance"]["total_realized_pnl_usd"], 0.0)

    def test_snapshot_contains_full_balances_in_database(self):
        self.store.update_snapshot(
            {"USDC": 1_234_567, "USDG": 2_000_000, "PYUSD": 3_000_000},
            {"USDC": 10_000_000, "USDG": 20_000_000, "PYUSD": 30_000_000},
            500_000_000,
            120.0,
        )

        state = read_state(self.db_path)
        self.assertEqual(state["balances"]["wallet"]["USDC"]["raw"], 1_234_567)
        self.assertAlmostEqual(state["balances"]["wallet"]["USDC"]["amount"], 1.234567)
        self.assertAlmostEqual(state["performance"]["current_portfolio_usd"], 66.234567)
        self.assertTrue(self.db_path.exists())
        self.assertFalse(self.db_path.with_suffix(".json").exists())
        self.assertFalse(self.db_path.with_name("pnl.txt").exists())

    def test_pending_submission_survives_restart_and_clears_by_signature(self):
        self.store.set_pending_submission("sig-a", "blockhash-a", "Jupiter exit")

        restarted = BotStateStore(self.db_path)
        pending = restarted.get_pending_submission()
        self.assertEqual(pending["signature"], "sig-a")
        self.assertEqual(pending["blockhash"], "blockhash-a")
        self.assertEqual(pending["label"], "Jupiter exit")
        self.assertTrue(pending["saved_at"])
        self.assertFalse(restarted.clear_pending_submission("different-sig"))
        self.assertIsNotNone(restarted.get_pending_submission())
        self.assertTrue(restarted.clear_pending_submission("sig-a"))
        restarted.close()

        second_restart = BotStateStore(self.db_path)
        self.assertIsNone(second_restart.get_pending_submission())
        second_restart.close()

    def test_legacy_pnl_is_kept_separate_from_new_net_accounting(self):
        root = Path(self.temp_dir.name) / "legacy-pnl"
        root.mkdir()
        pnl_path = root / "pnl.txt"
        pnl_path.write_text(
            "Time Run: 01:23:36\nArbs Executed: 3\nTotal Balance Change: $2.4602\n",
            encoding="utf-8",
        )

        store = BotStateStore(
            root / "bot_state.db",
            legacy_state_path=root / "bot_state.json",
            legacy_pnl_path=pnl_path,
        )
        store.start_session("wallet-address")
        state = store.snapshot()

        self.assertEqual(state["performance"]["total_arbs"], 3)
        self.assertEqual(state["performance"]["legacy_arbs"], 3)
        self.assertAlmostEqual(state["performance"]["legacy_balance_change_usd"], 2.4602)
        self.assertEqual(state["performance"]["total_realized_pnl_usd"], 0.0)
        store.close()

    def test_existing_json_state_and_attempts_are_imported_once(self):
        root = Path(self.temp_dir.name) / "legacy-json"
        root.mkdir()
        state_path = root / "bot_state.json"
        legacy = default_state()
        legacy["performance"]["total_realized_pnl_usd"] = 1.25
        legacy["recent_arbs"] = [
            {
                "id": "legacy-attempt",
                "timestamp": "2026-07-18T12:00:00+00:00",
                "route": "legacy route",
                "amount": 100.0,
                "status": "success",
                "expected_gross_profit_usd": 1.0,
                "stablecoin_change_usd": 1.0,
                "sol_consumed_lamports": 0,
                "sol_consumed": 0.0,
                "sol_price_usd": 0.0,
                "sol_cost_usd": 0.0,
                "realized_pnl_usd": 1.0,
                "note": "",
            }
        ]
        state_path.write_text(json.dumps(legacy), encoding="utf-8")

        db_path = root / "bot_state.db"
        store = BotStateStore(db_path, legacy_state_path=state_path)
        state = store.snapshot()
        store.close()

        self.assertAlmostEqual(state["performance"]["total_realized_pnl_usd"], 1.25)
        self.assertEqual(state["recent_arbs"][0]["id"], "legacy-attempt")
        days = read_daily_profit(
            db_path,
            days=10,
            now=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
        self.assertEqual(days[0]["date"], "2026-07-18")
        self.assertAlmostEqual(days[0]["profit_usdc"], 1.0)

    def test_daily_profit_is_aggregated_by_utc_day(self):
        before = {
            "usdc": 100.0,
            "usdg": 0.0,
            "pyusd": 0.0,
            "usdt": 0.0,
            "sol_lamports": 1_000_000_000,
            "sol_price": 100.0,
        }
        after_profit = dict(before, usdc=100.5)
        after_loss = dict(before, usdc=99.8)

        with patch("state_store.utc_now", return_value="2026-07-20T12:00:00+00:00"):
            self.store.record_attempt("profit-a", 100, 0.5, before, after_profit, True)
        with patch("state_store.utc_now", return_value="2026-07-20T18:00:00+00:00"):
            self.store.record_attempt("loss-a", 100, 0.0, before, after_loss, False)
        with patch("state_store.utc_now", return_value="2026-07-21T12:00:00+00:00"):
            self.store.record_attempt("profit-b", 100, 0.5, before, after_profit, True)

        days = read_daily_profit(
            self.db_path,
            days=10,
            now=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )

        self.assertEqual([day["date"] for day in days], ["2026-07-20", "2026-07-21"])
        self.assertAlmostEqual(days[0]["profit_usdc"], 0.3)
        self.assertEqual(days[0]["attempts"], 2)
        self.assertEqual(days[0]["successful_arbs"], 1)
        self.assertAlmostEqual(days[1]["profit_usdc"], 0.5)


if __name__ == "__main__":
    unittest.main()
