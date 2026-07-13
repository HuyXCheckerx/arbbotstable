import unittest

from recovery_logic import (
    planned_amount_is_available,
    raw_amount_to_human,
    recovery_quote_is_eligible,
    recovery_quote_metrics,
)


class RecoveryLogicTests(unittest.TestCase):
    def test_raw_six_decimal_amount_converts_to_human_tokens(self):
        self.assertEqual(raw_amount_to_human(50_078_301_316), 50_078.301316)

    def test_quote_includes_execution_and_slippage_reserves(self):
        metrics = recovery_quote_metrics(20_000_000_000, 20_003_000_000, 0.01, 1)
        self.assertEqual(metrics["gross_profit_usd"], 3.0)
        self.assertEqual(metrics["slippage_reserve_usd"], 2.0)
        self.assertAlmostEqual(metrics["net_profit_usd"], 0.99)
        self.assertTrue(recovery_quote_is_eligible(metrics, 0.10))

    def test_quote_below_target_is_not_eligible(self):
        metrics = recovery_quote_metrics(1_000_000_000, 1_000_200_000, 0.01, 1)
        self.assertFalse(recovery_quote_is_eligible(metrics, 0.10))

    def test_plan_is_never_resized_to_the_wallet_balance(self):
        self.assertTrue(planned_amount_is_available(19_998_050_000, 19_998_100_000, 100_000))
        self.assertFalse(planned_amount_is_available(19_997_900_000, 19_998_100_000, 100_000))


if __name__ == "__main__":
    unittest.main()
