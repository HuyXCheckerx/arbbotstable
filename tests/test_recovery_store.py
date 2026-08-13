import tempfile
import unittest
import sys
from pathlib import Path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
for sub in ("core", "recovery", "engines", "web", "deployers"):
    subpath = str(SRC_DIR / sub)
    if subpath not in sys.path:
        sys.path.insert(0, subpath)

from recovery_store import RecoveryStore


class RecoveryStoreTests(unittest.TestCase):
    def test_plan_lifecycle_and_pending_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "recovery.json")
            plan = store.schedule("USDG", 123_000_000, 0.50, "Jupiter", "exit failed")
            self.assertEqual(plan["status"], "watching")
            self.assertEqual(store.schedule("PYUSD", 1, 0.50, "Jupiter")["id"], plan["id"])
            synced, changed = store.sync_detected_position("USDG", 456_000_000, 0.10, "Jupiter")
            self.assertTrue(changed)
            self.assertEqual(synced["amount_raw"], 456_000_000)

            store.set_pending_submission("sig", "hash", "USDG recovery")
            self.assertEqual(store.get_active()["status"], "pending")
            self.assertEqual(store.set_min_net_profit(plan["id"], 0.10)["min_net_profit_usd"], 0.10)
            self.assertFalse(store.clear_pending_submission("other"))
            self.assertTrue(store.clear_pending_submission("sig"))

            remaining = store.update_remaining_amount(plan["id"], 2_089_146)
            self.assertEqual(remaining["amount_raw"], 2_089_146)
            self.assertEqual(remaining["status"], "watching")

            store.mark_manual_review(plan["id"], "balance changed")
            self.assertEqual(store.get_active()["status"], "manual_review")
            self.assertTrue(store.complete(plan["id"]))
            self.assertIsNone(store.get_active())


if __name__ == "__main__":
    unittest.main()
