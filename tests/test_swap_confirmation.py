import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SOLANA_PRIVATE_KEY", "[]")

import swapstable
from balance_tracker import BalanceTracker


class FakeMonitor(BalanceTracker):
    def is_ready(self):
        return True


class StaticBalanceClient:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def get_token_account_balance(self, account):
        self.calls.append(account)
        return SimpleNamespace(
            value=SimpleNamespace(amount=str(self.values[account]))
        )


class FakePendingStore:
    def __init__(self, pending):
        self.pending = pending
        self.cleared = []
        self.statuses = []

    def get_pending_submission(self):
        return self.pending

    def clear_pending_submission(self, signature):
        self.cleared.append(signature)
        self.pending = None
        return True

    def set_status(self, *args, **kwargs):
        self.statuses.append((args, kwargs))


class SwapConfirmationTests(unittest.TestCase):
    def test_strategy_builder_places_both_cross_cycles_before_jupiter_entries(self):
        token_configs = {
            token: {
                "mint": f"{token}-mint",
                "stable_pool": 100_000,
                "token_ata": f"{token}-ata",
                "balance_key": f"user_{token.lower()}",
            }
            for token in ("USDG", "PYUSD", "USDT")
        }

        strategies = swapstable.build_usdc_strategies(
            token_configs,
            stable_usdc_pool=200_000,
            stable_exit_reserve=1.9,
        )

        self.assertEqual(
            [
                (
                    strategy["route_kind"],
                    strategy["token"],
                    strategy["output_token"],
                    strategy["venue_order"],
                )
                for strategy in strategies
            ],
            [
                ("direct", "USDG", "USDC", "stable_first"),
                ("direct", "PYUSD", "USDC", "stable_first"),
                ("cross", "USDG", "PYUSD", "stable_first"),
                ("cross", "PYUSD", "USDG", "stable_first"),
                ("direct", "USDG", "USDG", "jupiter_first"),
                ("direct", "PYUSD", "PYUSD", "jupiter_first"),
                ("direct", "USDT", "USDT", "jupiter_first"),
            ],
        )
        for strategy in strategies[2:4]:
            self.assertEqual(strategy["stable_exit_pool"], 200_000)
            self.assertEqual(strategy["stable_exit_reserve"], 1.9)

    def test_jupiter_cap_mode_requests_and_enforces_configured_ceiling(self):
        with (
            patch.object(swapstable, "JUPITER_PRIORITY_FEE_MODE", "cap"),
            patch.object(
                swapstable,
                "JUPITER_PRIORITY_FEE_CAP_LAMPORTS",
                12_345,
            ),
        ):
            self.assertEqual(
                swapstable.jupiter_order_fee_params(),
                {
                    "priorityFeeLamports": "12345",
                    "broadcastFeeType": "maxCap",
                },
            )
            self.assertEqual(
                swapstable.jupiter_swap_priority_fee_request(),
                {
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": 12_345,
                        "priorityLevel": "medium",
                    }
                },
            )
            with patch.object(
                swapstable,
                "cap_jup_priority_fee",
                return_value=b"capped",
            ) as cap:
                self.assertEqual(
                    swapstable.apply_jup_priority_fee_policy(b"generated"),
                    b"capped",
                )
                cap.assert_called_once_with(b"generated", 12_345)

    def test_jupiter_recommended_mode_preserves_generated_fee(self):
        with patch.object(
            swapstable,
            "JUPITER_PRIORITY_FEE_MODE",
            "recommended",
        ):
            self.assertEqual(swapstable.jupiter_order_fee_params(), {})
            self.assertIsNone(swapstable.jupiter_swap_priority_fee_request())
            with patch.object(swapstable, "cap_jup_priority_fee") as cap:
                tx_bytes = b"jupiter-generated-transaction"
                self.assertIs(
                    swapstable.apply_jup_priority_fee_policy(tx_bytes),
                    tx_bytes,
                )
                cap.assert_not_called()

    def test_accounting_snapshot_uses_confirmed_exit_balances_coherently(self):
        before = {
            "usdc_raw": 50_605_424_067,
            "usdg_raw": 0,
            "pyusd_raw": 0,
            "usdt_raw": 0,
        }
        confirmed = {
            "user_usdc": 50_605_731_737,
            "user_usdg": 0,
        }

        snapshot = swapstable.accounting_raw_snapshot(
            before, "USDG", confirmed
        )

        self.assertEqual(snapshot["usdc"], 50_605_731_737)
        self.assertEqual(snapshot["usdg"], 0)
        self.assertEqual(snapshot["pyusd"], 0)
        self.assertEqual(snapshot["usdt"], 0)

    def test_accounting_snapshot_can_merge_both_cross_route_tokens(self):
        before = {
            "usdc_raw": 50_000_000_000,
            "usdg_raw": 10,
            "pyusd_raw": 20,
            "usdt_raw": 0,
        }
        confirmed = {
            "user_usdc": 50_000_200_000,
            "user_usdg": 11,
            "user_pyusd": 21,
        }

        snapshot = swapstable.accounting_raw_snapshot(
            before,
            confirmed_balances=confirmed,
            tokens=("USDG", "PYUSD"),
        )

        self.assertEqual(snapshot["usdc"], 50_000_200_000)
        self.assertEqual(snapshot["usdg"], 11)
        self.assertEqual(snapshot["pyusd"], 21)

    def test_usdt_route_profit_floor_is_at_least_five_dollars(self):
        self.assertGreaterEqual(swapstable.USDT_MIN_NET_PROFIT_USD, 5.0)

    def test_jupiter_entry_retries_only_definitive_failures(self):
        for source in ("not_submitted", "signature_error", "expired"):
            self.assertTrue(swapstable.jupiter_entry_retry_is_safe(source))
        for source in ("rpc_error", "rpc", "ws", "ws_reconciled"):
            self.assertFalse(swapstable.jupiter_entry_retry_is_safe(source))

    def test_stable_result_preserves_ambiguous_submission(self):
        submission = swapstable.SwapSubmissionResult(
            False,
            signature="local-signature",
            blockhash="local-blockhash",
            ambiguous=True,
        )

        result = swapstable.StableSwapResult(False, submission=submission)

        self.assertFalse(result)
        self.assertTrue(result.may_have_landed)
        self.assertIs(result.submission, submission)

    def test_processed_submission_is_not_abandoned_when_blockhash_expires(self):
        monitor = FakeMonitor(["user_usdg", "user_usdc"])
        monitor.seed("user_usdg", 0)
        monitor.seed("user_usdc", 100)
        cursor = monitor.snapshot().revisions
        client = StaticBalanceClient({"usdg_ata": 0, "usdc_ata": 100})
        submission = swapstable.SwapSubmissionResult(
            False,
            signature="1111111111111111111111111111111111111111111111111111111111111111",
            blockhash="11111111111111111111111111111111",
            ambiguous=True,
        )

        def publish_landed_transaction():
            monitor.update("user_usdg", 50, slot=10)
            monitor.update("user_usdc", 50, slot=10)

        timer = threading.Timer(0.02, publish_landed_transaction)
        timer.start()
        try:
            with (
                patch.object(
                    swapstable,
                    "get_submission_signature_status",
                    return_value=("processed", ""),
                ),
                patch.object(
                    swapstable,
                    "is_submission_blockhash_valid",
                    side_effect=AssertionError(
                        "processed transactions must not use blockhash expiry"
                    ),
                ),
                patch.object(swapstable, "SIGNATURE_CHECK_INTERVAL_SECONDS", 0.25),
            ):
                confirmed, balances, source = swapstable.confirm_transfer_ws_first(
                    client,
                    monitor,
                    {
                        "user_usdg": ("usdg_ata", lambda value: value > 0),
                        "user_usdc": ("usdc_ata", lambda value: value < 100),
                    },
                    cursor,
                    "test entry",
                    timeout_seconds=0,
                    submission=submission,
                )
        finally:
            timer.join(1)

        self.assertTrue(confirmed)
        self.assertEqual(balances, {"user_usdg": 50, "user_usdc": 50})
        self.assertEqual(source, "ws_reconciled")
        self.assertEqual(client.calls, ["usdg_ata", "usdc_ata"])

    def test_expired_ambiguous_submission_is_safe_to_abandon(self):
        monitor = FakeMonitor(["user_usdg", "user_usdc"])
        monitor.seed("user_usdg", 0)
        monitor.seed("user_usdc", 100)
        client = StaticBalanceClient({"usdg_ata": 0, "usdc_ata": 100})
        submission = swapstable.SwapSubmissionResult(
            False,
            signature="1111111111111111111111111111111111111111111111111111111111111111",
            blockhash="11111111111111111111111111111111",
            ambiguous=True,
        )

        with (
            patch.object(
                swapstable,
                "get_submission_signature_status",
                return_value=("not_found", ""),
            ),
            patch.object(
                swapstable,
                "is_submission_blockhash_valid",
                return_value=False,
            ),
        ):
            confirmed, _, source = swapstable.confirm_transfer_ws_first(
                client,
                monitor,
                {
                    "user_usdg": ("usdg_ata", lambda value: value > 0),
                    "user_usdc": ("usdc_ata", lambda value: value < 100),
                },
                monitor.snapshot().revisions,
                "test entry",
                timeout_seconds=0,
                submission=submission,
            )

        self.assertFalse(confirmed)
        self.assertEqual(source, "expired")
        # Initial fallback plus one final snapshot after proven expiry.
        self.assertEqual(
            client.calls,
            ["usdg_ata", "usdc_ata", "usdg_ata", "usdc_ata"],
        )

    def test_repeat_status_confirmation_cannot_regress_to_expired(self):
        monitor = FakeMonitor(["user_usdg", "user_usdc"])
        monitor.seed("user_usdg", 0)
        monitor.seed("user_usdc", 100)
        client = StaticBalanceClient({"usdg_ata": 0, "usdc_ata": 100})
        submission = swapstable.SwapSubmissionResult(
            False,
            signature="1111111111111111111111111111111111111111111111111111111111111111",
            blockhash="11111111111111111111111111111111",
            ambiguous=True,
        )
        status_calls = []

        def status_lookup(*_args):
            status_calls.append(len(status_calls) + 1)
            if len(status_calls) == 1:
                return "not_found", ""
            client.values.update({"usdg_ata": 50, "usdc_ata": 50})
            return "confirmed", ""

        with (
            patch.object(
                swapstable,
                "get_submission_signature_status",
                side_effect=status_lookup,
            ),
            patch.object(
                swapstable,
                "is_submission_blockhash_valid",
                return_value=False,
            ),
        ):
            confirmed, _, source = swapstable.confirm_transfer_ws_first(
                client,
                monitor,
                {
                    "user_usdg": ("usdg_ata", lambda value: value > 0),
                    "user_usdc": ("usdc_ata", lambda value: value < 100),
                },
                monitor.snapshot().revisions,
                "test entry",
                timeout_seconds=0,
                submission=submission,
            )

        self.assertTrue(confirmed)
        self.assertEqual(source, "rpc")
        self.assertEqual(status_calls, [1, 2])

    def test_restart_lock_waits_for_processed_transaction(self):
        store = FakePendingStore(
            {
                "signature": "sig-a",
                "blockhash": "blockhash-a",
                "label": "Jupiter entry",
            }
        )

        with (
            patch.object(
                swapstable,
                "get_submission_signature_status",
                side_effect=[("processed", ""), ("confirmed", "")],
            ),
            patch.object(
                swapstable,
                "is_submission_blockhash_valid",
                side_effect=AssertionError(
                    "processed transactions must not use blockhash expiry"
                ),
            ),
            patch.object(swapstable.time, "sleep", return_value=None),
        ):
            swapstable.reconcile_persisted_submission(object(), store)

        self.assertEqual(store.cleared, ["sig-a"])
        self.assertIsNone(store.pending)

    def test_restart_repeat_confirmation_is_handled_immediately(self):
        store = FakePendingStore(
            {
                "signature": "sig-a",
                "blockhash": "blockhash-a",
                "label": "Stable entry",
            }
        )

        with (
            patch.object(
                swapstable,
                "get_submission_signature_status",
                side_effect=[("not_found", ""), ("confirmed", "")],
            ) as status_lookup,
            patch.object(
                swapstable,
                "is_submission_blockhash_valid",
                return_value=False,
            ),
        ):
            swapstable.reconcile_persisted_submission(object(), store)

        self.assertEqual(status_lookup.call_count, 2)
        self.assertEqual(store.cleared, ["sig-a"])


if __name__ == "__main__":
    unittest.main()
