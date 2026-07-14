import json
import tempfile
import unittest
from pathlib import Path

from state_store import BotStateStore, read_state


class BotStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.state_path = root / "bot_state.json"
        self.pnl_path = root / "pnl.txt"
        self.store = BotStateStore(self.state_path, self.pnl_path)
        self.store.start_session("wallet-address")

    def tearDown(self):
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

        state = read_state(self.state_path)
        self.assertEqual(state["performance"]["total_arbs"], 1)
        self.assertEqual(state["performance"]["total_attempts"], 1)
        self.assertAlmostEqual(state["performance"]["total_sol_consumed"], 0.00001)
        self.assertAlmostEqual(
            self.store.estimated_execution_cost_usd(default_cost=0.0001),
            0.001,
        )

    def test_failed_attempt_counts_cost_but_not_arb(self):
        before = {"usdc": 10.0, "usdg": 0.0, "pyusd": 0.0, "usdt": 0.0, "sol_lamports": 50_000, "sol_price": 80.0}
        after = {"usdc": 10.0, "usdg": 0.0, "pyusd": 0.0, "usdt": 0.0, "sol_lamports": 45_000, "sol_price": 80.0}

        record = self.store.record_attempt("USDC -> USDG", 10, 0.2, before, after, False, "rejected")

        self.assertLess(record["realized_pnl_usd"], 0)
        state = read_state(self.state_path)
        self.assertEqual(state["performance"]["total_arbs"], 0)
        self.assertEqual(state["performance"]["total_attempts"], 1)

    def test_stranded_usdt_is_counted_in_stablecoin_value(self):
        before = {
            "usdc": 100.0, "usdg": 0.0, "pyusd": 0.0, "usdt": 0.0,
            "sol_lamports": 1_000_000_000, "sol_price": 100.0,
        }
        after = {
            "usdc": 0.0, "usdg": 0.0, "pyusd": 0.0, "usdt": 100.0,
            "sol_lamports": 999_990_000, "sol_price": 100.0,
        }

        record = self.store.record_attempt(
            "USDC -> USDT", 100, 0.0, before, after, False, "awaiting recovery"
        )

        self.assertEqual(record["stablecoin_change_usd"], 0.0)
        self.assertAlmostEqual(record["realized_pnl_usd"], -0.001)

    def test_incomplete_snapshot_cannot_corrupt_pnl(self):
        before = {
            "usdc": 50_000.0, "usdg": 0.0, "pyusd": 0.0,
            "usdt": 0.0,
            "sol_lamports": 1_000_000_000, "sol_price": 80.0,
        }
        after = {
            "usdc": None, "usdg": 0.0, "pyusd": 0.0,
            "usdt": 0.0,
            "sol_lamports": 999_990_000, "sol_price": 80.0,
        }

        with self.assertRaisesRegex(ValueError, "incomplete after snapshot: usdc"):
            self.store.record_attempt("USDC -> USDG -> USDC", 50_000, 0.2, before, after, True)

        state = read_state(self.state_path)
        self.assertEqual(state["performance"]["total_attempts"], 0)
        self.assertEqual(state["performance"]["total_realized_pnl_usd"], 0.0)

    def test_snapshot_contains_full_balances_and_atomic_json(self):
        self.store.update_snapshot(
            {"USDC": 1_234_567, "USDG": 2_000_000, "PYUSD": 3_000_000},
            {"USDC": 10_000_000, "USDG": 20_000_000, "PYUSD": 30_000_000},
            500_000_000,
            120.0,
        )

        with open(self.state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(state["balances"]["wallet"]["USDC"]["raw"], 1_234_567)
        self.assertAlmostEqual(state["balances"]["wallet"]["USDC"]["amount"], 1.234567)
        self.assertAlmostEqual(state["performance"]["current_portfolio_usd"], 66.234567)
        self.assertFalse(self.state_path.with_suffix(".json.tmp").exists())

    def test_pending_submission_survives_restart_and_clears_by_signature(self):
        self.store.set_pending_submission("sig-a", "blockhash-a", "Jupiter exit")

        restarted = BotStateStore(self.state_path, self.pnl_path)
        pending = restarted.get_pending_submission()
        self.assertEqual(pending["signature"], "sig-a")
        self.assertEqual(pending["blockhash"], "blockhash-a")
        self.assertEqual(pending["label"], "Jupiter exit")
        self.assertTrue(pending["saved_at"])
        self.assertFalse(restarted.clear_pending_submission("different-sig"))
        self.assertIsNotNone(restarted.get_pending_submission())
        self.assertTrue(restarted.clear_pending_submission("sig-a"))
        self.assertIsNone(BotStateStore(self.state_path, self.pnl_path).get_pending_submission())

    def test_legacy_pnl_is_kept_separate_from_new_net_accounting(self):
        root = Path(self.temp_dir.name) / "legacy"
        root.mkdir()
        pnl_path = root / "pnl.txt"
        pnl_path.write_text(
            "Time Run: 01:23:36\nArbs Executed: 3\nTotal Balance Change: $2.4602\n",
            encoding="utf-8",
        )

        store = BotStateStore(root / "bot_state.json", pnl_path)
        store.start_session("wallet-address")
        state = store.snapshot()

        self.assertEqual(state["performance"]["total_arbs"], 3)
        self.assertEqual(state["performance"]["legacy_arbs"], 3)
        self.assertAlmostEqual(state["performance"]["legacy_balance_change_usd"], 2.4602)
        self.assertEqual(state["performance"]["total_realized_pnl_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
