import threading
import unittest

from balance_tracker import BalanceTracker, confirm_balances_ws_first


class ManualClock:
    def __init__(self, start=0.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class BalanceTrackerTests(unittest.TestCase):
    def test_seed_stores_metadata_without_revision_or_notification(self):
        tracker = BalanceTracker(["user_usdg"])

        self.assertTrue(
            tracker.seed("user_usdg", 12_000_000, slot=101, timestamp=12.5)
        )

        snapshot = tracker.snapshot(["user_usdg"])
        self.assertEqual(snapshot["user_usdg"], 12_000_000)
        self.assertEqual(snapshot.revisions["user_usdg"], 0)
        self.assertEqual(snapshot.slots["user_usdg"], 101)
        self.assertEqual(snapshot.timestamps["user_usdg"], 12.5)
        self.assertFalse(tracker.update_event.is_set())

    def test_revisions_are_monotonic_and_independent_per_key(self):
        tracker = BalanceTracker(["a", "b"])

        self.assertEqual(tracker.update("a", 1, slot=11), 1)
        self.assertEqual(tracker.update("a", 1, slot=12), 2)
        self.assertEqual(tracker.update("b", 9, slot=13), 1)

        snapshot = tracker.snapshot()
        self.assertEqual(dict(snapshot.revisions), {"a": 2, "b": 1})
        self.assertEqual(snapshot.slots["a"], 12)
        self.assertTrue(tracker.update_event.is_set())

    def test_older_solana_slot_is_ignored_without_notification(self):
        tracker = BalanceTracker(["user_usdg"])
        tracker.seed("user_usdg", 10, slot=100)
        tracker.update_event.clear()

        self.assertEqual(tracker.update("user_usdg", 5, slot=99), 0)

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["user_usdg"], 10)
        self.assertEqual(snapshot.revisions["user_usdg"], 0)
        self.assertEqual(snapshot.slots["user_usdg"], 100)
        self.assertFalse(tracker.update_event.is_set())

    def test_slotless_update_preserves_slot_high_water_mark(self):
        tracker = BalanceTracker(["user_usdg"])
        tracker.update("user_usdg", 10, slot=100)
        tracker.update("user_usdg", 9, slot=None)

        self.assertEqual(tracker.snapshot().slots["user_usdg"], 100)
        self.assertEqual(tracker.update("user_usdg", 5, slot=99), 2)
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["user_usdg"], 9)
        self.assertEqual(snapshot.slots["user_usdg"], 100)

    def test_update_before_wait_is_observed_from_revision_cursor(self):
        tracker = BalanceTracker(["user_usdg"])
        tracker.seed("user_usdg", 0)
        before = tracker.snapshot()

        tracker.update("user_usdg", 5_000_000, slot=202)
        result = tracker.wait_for(
            lambda snapshot: snapshot["user_usdg"] >= 5_000_000,
            before.revisions,
            timeout=0,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.revisions["user_usdg"], 1)

    def test_delayed_update_notifies_waiter(self):
        tracker = BalanceTracker(["user_usdc"])
        tracker.seed("user_usdc", 10)
        predicate_checked = threading.Event()
        outcome = []

        def predicate(snapshot):
            predicate_checked.set()
            return snapshot["user_usdc"] == 25

        def wait():
            outcome.append(tracker.wait_for(predicate, {}, timeout=1.0))

        waiter = threading.Thread(target=wait)
        waiter.start()
        self.assertTrue(predicate_checked.wait(0.5))
        tracker.update("user_usdc", 25)
        waiter.join(0.5)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(outcome[0]["user_usdc"], 25)

    def test_stale_matching_balance_is_rejected_without_newer_revision(self):
        tracker = BalanceTracker(["user_usdg"])
        tracker.seed("user_usdg", 5_000_000)
        cursor = tracker.snapshot().revisions

        result = tracker.wait_for(
            lambda snapshot: snapshot["user_usdg"] == 5_000_000,
            cursor,
            timeout=0,
        )

        self.assertIsNone(result)

    def test_compound_cursor_requires_both_keys_to_advance(self):
        tracker = BalanceTracker(["user_usdc", "user_usdg"])
        tracker.seed("user_usdc", 100)
        tracker.seed("user_usdg", 0)
        cursor = tracker.snapshot().revisions

        tracker.update("user_usdc", 50)
        predicate = lambda snapshot: (
            snapshot["user_usdc"] == 50 and snapshot["user_usdg"] == 50
        )
        self.assertIsNone(tracker.wait_for(predicate, cursor, timeout=0))

        tracker.update("user_usdg", 50)
        result = tracker.wait_for(predicate, cursor, timeout=0)

        self.assertIsNotNone(result)
        self.assertEqual(dict(result.revisions), {"user_usdc": 1, "user_usdg": 1})

    def test_increase_settle_window_uses_latest_increase(self):
        clock = ManualClock(100)
        tracker = BalanceTracker(["pool_usdg"], monotonic=clock)
        tracker.seed("pool_usdg", 10)
        self.assertEqual(tracker.seconds_until_increase_settled("pool_usdg", 2.5), 0)

        tracker.update("pool_usdg", 20)
        self.assertEqual(tracker.seconds_until_increase_settled("pool_usdg", 2.5), 2.5)

        clock.advance(1)
        tracker.update("pool_usdg", 19)
        self.assertEqual(tracker.seconds_until_increase_settled("pool_usdg", 2.5), 1.5)

        tracker.update("pool_usdg", 21)
        self.assertEqual(tracker.seconds_until_increase_settled("pool_usdg", 2.5), 2.5)
        clock.advance(3)
        self.assertEqual(tracker.seconds_until_increase_settled("pool_usdg", 2.5), 0)

    def test_seed_cannot_overwrite_a_live_update(self):
        tracker = BalanceTracker(["user_usdg"])
        tracker.update("user_usdg", 8, slot=200)

        self.assertFalse(tracker.seed("user_usdg", 3, slot=100))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["user_usdg"], 8)
        self.assertEqual(snapshot.revisions["user_usdg"], 1)
        self.assertEqual(snapshot.slots["user_usdg"], 200)

    def test_confirmation_accepts_updates_that_arrived_before_wait(self):
        tracker = BalanceTracker(["user_usdc", "user_usdg"])
        tracker.seed("user_usdc", 100)
        tracker.seed("user_usdg", 50)
        cursor = tracker.snapshot().revisions
        tracker.update("user_usdc", 151, slot=20)
        tracker.update("user_usdg", 0, slot=20)
        rpc_calls = []

        result = confirm_balances_ws_first(
            tracker,
            {
                "user_usdc": lambda balance: balance > 100,
                "user_usdg": lambda balance: balance == 0,
            },
            cursor,
            timeout=0,
            rpc_reader=lambda key: rpc_calls.append(key),
        )

        self.assertTrue(result.confirmed)
        self.assertEqual(result.source, "ws")
        self.assertEqual(rpc_calls, [])

    def test_confirmation_waits_for_delayed_ws_update_before_rpc(self):
        tracker = BalanceTracker(["user_usdc", "user_usdg"])
        tracker.seed("user_usdc", 100)
        tracker.seed("user_usdg", 50)
        cursor = tracker.snapshot().revisions
        rpc_calls = []

        timer = threading.Timer(
            0.02,
            lambda: (
                tracker.update("user_usdc", 151, slot=20),
                tracker.update("user_usdg", 0, slot=20),
            ),
        )
        timer.start()
        try:
            result = confirm_balances_ws_first(
                tracker,
                {
                    "user_usdc": lambda balance: balance > 100,
                    "user_usdg": lambda balance: balance == 0,
                },
                cursor,
                timeout=0.25,
                rpc_reader=lambda key: rpc_calls.append(key),
            )
        finally:
            timer.join(1)

        self.assertTrue(result.confirmed)
        self.assertEqual(result.source, "ws")
        self.assertEqual(rpc_calls, [])

    def test_stale_matching_cache_uses_exactly_one_rpc_snapshot(self):
        tracker = BalanceTracker(["user_usdc", "user_usdg"])
        tracker.seed("user_usdc", 100)
        tracker.seed("user_usdg", 0)
        cursor = tracker.snapshot().revisions
        rpc_values = {"user_usdc": 100, "user_usdg": 50}
        rpc_calls = []

        result = confirm_balances_ws_first(
            tracker,
            {
                "user_usdc": lambda balance: balance > 100,
                "user_usdg": lambda balance: balance == 0,
            },
            cursor,
            timeout=0,
            rpc_reader=lambda key: (
                rpc_calls.append(key),
                rpc_values[key],
            )[1],
        )

        self.assertFalse(result.confirmed)
        self.assertEqual(result.source, "rpc")
        self.assertEqual(rpc_calls, ["user_usdc", "user_usdg"])

    def test_ws_confirmation_rejects_mixed_solana_slots(self):
        tracker = BalanceTracker(["user_usdc", "user_usdg"])
        tracker.seed("user_usdc", 100)
        tracker.seed("user_usdg", 50)
        cursor = tracker.snapshot().revisions
        tracker.update("user_usdc", 151, slot=101)
        tracker.update("user_usdg", 0, slot=900)
        rpc_values = {"user_usdc": 151, "user_usdg": 0}

        result = confirm_balances_ws_first(
            tracker,
            {
                "user_usdc": lambda balance: balance > 100,
                "user_usdg": lambda balance: balance == 0,
            },
            cursor,
            timeout=0,
            rpc_reader=rpc_values.__getitem__,
        )

        self.assertTrue(result.confirmed)
        self.assertEqual(result.source, "rpc")

    def test_confirmation_uses_rpc_after_bounded_ws_wait(self):
        tracker = BalanceTracker(["user_usdc", "user_usdg"])
        tracker.seed("user_usdc", 100)
        tracker.seed("user_usdg", 50)
        rpc_values = {"user_usdc": 151, "user_usdg": 0}

        result = confirm_balances_ws_first(
            tracker,
            {
                "user_usdc": lambda balance: balance > 100,
                "user_usdg": lambda balance: balance == 0,
            },
            tracker.snapshot().revisions,
            timeout=0,
            rpc_reader=rpc_values.__getitem__,
        )

        self.assertTrue(result.confirmed)
        self.assertEqual(result.source, "rpc")

    def test_rpc_error_is_ambiguous_not_confirmed(self):
        tracker = BalanceTracker(["user_usdc"])
        tracker.seed("user_usdc", 100)

        result = confirm_balances_ws_first(
            tracker,
            {"user_usdc": lambda balance: balance > 100},
            tracker.snapshot().revisions,
            timeout=0,
            rpc_reader=lambda key: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        self.assertFalse(result.confirmed)
        self.assertEqual(result.source, "rpc_error")
        self.assertIn("offline", result.error)

    def test_rpc_observed_increase_starts_settlement_without_ws_revision(self):
        clock = ManualClock(10)
        tracker = BalanceTracker(["pool_usdg"], monotonic=clock)
        tracker.seed("pool_usdg", 1_900_000)

        self.assertTrue(tracker.observe_balance("pool_usdg", 20_000_000_000))
        snapshot = tracker.snapshot(["pool_usdg"])
        self.assertEqual(snapshot["pool_usdg"], 1_900_000)
        self.assertEqual(snapshot.revisions["pool_usdg"], 0)
        self.assertEqual(tracker.seconds_until_increase_settled("pool_usdg", 15), 15)

        # Seeing the same value later over WS does not restart the timer.
        clock.advance(4)
        tracker.update("pool_usdg", 20_000_000_000, slot=100)
        self.assertEqual(tracker.seconds_until_increase_settled("pool_usdg", 15), 11)


if __name__ == "__main__":
    unittest.main()
