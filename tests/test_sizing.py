import unittest

from sizing import (
    absolute_profit_key,
    acquired_balance_delta,
    acquired_delta_is_cleared,
    adjusted_drain_minimum_raw,
    calculate_quote_metrics,
    drain_candidate_is_valid,
    generate_candidate_sizes,
    generate_drain_candidate_amounts_raw,
    generate_refinement_sizes,
    is_profitable_candidate,
    normalize_drain_window_raw,
    parse_stable_reserve_constraint,
    stable_pool_can_settle,
    usdc_strategy_directions,
)


class DynamicSizingTests(unittest.TestCase):
    def test_strategy_scope_is_usdc_with_two_venue_orders_per_token(self):
        self.assertEqual(
            usdc_strategy_directions(),
            [
                ("USDG", "stable_first"),
                ("USDG", "jupiter_first"),
                ("PYUSD", "stable_first"),
                ("PYUSD", "jupiter_first"),
            ],
        )

    def test_stable_pool_requirement_depends_on_venue_order(self):
        self.assertTrue(stable_pool_can_settle("stable_first", 5_000, 5_000.20, 5_001))
        self.assertFalse(stable_pool_can_settle("jupiter_first", 5_000, 5_000.20, 5_001))
        self.assertTrue(stable_pool_can_settle("jupiter_first", 5_000, 5_000.20, 5_002))

    def test_execution_uses_only_balance_created_by_current_cycle(self):
        baseline = 900_000
        after_entry = 1_000_900_000
        self.assertEqual(acquired_balance_delta(after_entry, baseline), 1_000_000_000)
        self.assertTrue(acquired_delta_is_cleared(950_000, baseline))
        self.assertFalse(acquired_delta_is_cleared(2_000_000, baseline))

    def test_usdg_drain_candidates_leave_only_small_raw_remainders(self):
        pool = 5_000 * 10**6
        wallet = 50_027 * 10**6
        candidates = generate_drain_candidate_amounts_raw(
            pool,
            wallet,
            1_000 * 10**6,
            dust_raw=1_900_000,
            max_remainder_raw=1_990_000,
        )
        self.assertEqual(
            candidates,
            [
                4_998_010_000,
                4_998_055_000,
                4_998_100_000,
            ],
        )
        self.assertTrue(
            all(
                1_900_000 <= pool - amount <= 1_990_000
                for amount in candidates
            )
        )

    def test_usdg_drain_rejects_partial_drain_when_wallet_is_too_small(self):
        candidates = generate_drain_candidate_amounts_raw(
            5_000 * 10**6,
            3_000 * 10**6,
            1_000 * 10**6,
            dust_raw=1_900_000,
            max_remainder_raw=1_990_000,
        )
        self.assertEqual(candidates, [])

    def test_usdg_drain_validity_rejects_large_remainder(self):
        self.assertTrue(
            drain_candidate_is_valid(
                5_000 * 10**6,
                4_998_100_000,
                dust_raw=1_900_000,
                max_remainder_raw=1_990_000,
            )
        )
        self.assertFalse(
            drain_candidate_is_valid(
                5_000 * 10**6,
                4_998_000_000,
                dust_raw=1_900_000,
                max_remainder_raw=1_990_000,
            )
        )

    def test_reported_one_dollar_remainder_is_outside_safe_window(self):
        pool_raw = 20_000_000_435
        rejected_amount_raw = 19_999_000_435
        self.assertFalse(
            drain_candidate_is_valid(
                pool_raw,
                rejected_amount_raw,
                dust_raw=1_900_000,
                max_remainder_raw=1_990_000,
            )
        )
        candidates = generate_drain_candidate_amounts_raw(
            pool_raw,
            50_000 * 10**6,
            1_000 * 10**6,
            dust_raw=1_900_000,
            max_remainder_raw=1_990_000,
        )
        self.assertEqual(
            candidates,
            [19_998_010_435, 19_998_055_435, 19_998_100_435],
        )

    def test_drain_window_hard_clamps_unsafe_one_dollar_bounds(self):
        self.assertEqual(
            normalize_drain_window_raw(1_000_000, 1_000_000),
            (1_900_000, 1_900_000),
        )

    def test_drain_window_rejects_floor_that_reaches_refill_trigger(self):
        with self.assertRaises(ValueError):
            normalize_drain_window_raw(
                1_900_000,
                1_990_000,
                protocol_floor_raw=1_900_000,
                safety_buffer_raw=100_000,
                refill_trigger_raw=2_000_000,
            )

    def test_parses_reported_stable_reserve_constraint(self):
        constraint = parse_stable_reserve_constraint(
            {
                "message": "Service Unavailable",
                "details": {
                    "insufficient_pool_balance": (
                        "remaining reserves would be below required threshold: "
                        "remainingAfterOperation=1000000, thresholdYMinusZ=1800000 "
                        "(thresholdY=2000000, thresholdZ=200000)"
                    )
                },
            }
        )
        self.assertEqual(
            constraint,
            {
                "remaining_raw": 1_000_000,
                "required_raw": 1_800_000,
                "threshold_y_raw": 2_000_000,
                "threshold_z_raw": 200_000,
            },
        )

    def test_reserve_parser_falls_back_to_threshold_difference(self):
        constraint = parse_stable_reserve_constraint(
            {
                "details": {
                    "insufficient_pool_balance": (
                        "remainingAfterOperation=1700000 "
                        "(thresholdY=2000000, thresholdZ=200000)"
                    )
                }
            }
        )
        self.assertEqual(constraint["required_raw"], 1_800_000)
        self.assertIsNone(parse_stable_reserve_constraint({"details": {}}))

    def test_reserve_rejection_raises_floor_or_reports_no_safe_window(self):
        self.assertEqual(
            adjusted_drain_minimum_raw(
                1_900_000,
                1_990_000,
                {"remaining_raw": 1_800_000, "required_raw": 1_850_000},
                safety_buffer_raw=100_000,
                checked_remainder_raw=1_900_000,
            ),
            1_950_000,
        )
        self.assertIsNone(
            adjusted_drain_minimum_raw(
                1_900_000,
                1_990_000,
                {"remaining_raw": 1_000_000, "required_raw": 1_800_000},
                safety_buffer_raw=100_000,
                checked_remainder_raw=1_900_000,
            )
        )
        self.assertIsNone(
            adjusted_drain_minimum_raw(
                1_900_000,
                1_990_000,
                {"remaining_raw": 1_790_000, "required_raw": 1_800_000},
                safety_buffer_raw=100_000,
                checked_remainder_raw=1_990_000,
            )
        )

    def test_generates_coarse_grid_including_minimum_and_maximum(self):
        sizes = generate_candidate_sizes(5_000, 1_000)
        self.assertEqual(sizes, [1_000, 1_250, 2_000, 2_500, 3_750, 5_000])

    def test_returns_no_sizes_when_minimum_is_not_feasible(self):
        self.assertEqual(generate_candidate_sizes(999, 1_000), [])

    def test_refines_around_best_coarse_size(self):
        sizes = [1_000, 2_000, 5_000]
        self.assertEqual(
            generate_refinement_sizes(2_000, sizes, 1_000, 5_000),
            [1_500, 3_500],
        )

    def test_minimum_profit_is_net_of_execution_cost(self):
        metrics = calculate_quote_metrics(1_000, 1_000.06, 0.01)
        self.assertAlmostEqual(metrics["gross_profit_usd"], 0.06)
        self.assertAlmostEqual(metrics["net_profit_usd"], 0.05)
        self.assertTrue(is_profitable_candidate(metrics, 0.05))

        insufficient = calculate_quote_metrics(1_000, 1_000.05, 0.01)
        self.assertAlmostEqual(insufficient["net_profit_usd"], 0.04)
        self.assertFalse(is_profitable_candidate(insufficient, 0.05))

    def test_absolute_profit_floor_does_not_scale_with_trade_size(self):
        too_small = calculate_quote_metrics(5_000, 5_000.09, 0.0)
        enough = calculate_quote_metrics(5_000, 5_000.10, 0.0)
        self.assertFalse(is_profitable_candidate(too_small, 0.10, 0.0))
        self.assertTrue(is_profitable_candidate(enough, 0.10, 0.0))

    def test_selection_prefers_highest_absolute_profit_after_gate(self):
        candidates = {
            10_000: calculate_quote_metrics(10_000, 10_000.14, 0.0),
            20_000: calculate_quote_metrics(20_000, 20_000.20, 0.0),
        }
        eligible = {
            size: metrics
            for size, metrics in candidates.items()
            if is_profitable_candidate(metrics, 0.10, 0.0)
        }
        selected = max(
            eligible,
            key=lambda size: absolute_profit_key(size, eligible[size]),
        )
        self.assertEqual(selected, 20_000)

    def test_equal_profit_tie_uses_lower_exposure_without_return_ranking(self):
        smaller = calculate_quote_metrics(1_000, 1_000.20, 0.0)
        larger = calculate_quote_metrics(20_000, 20_000.20, 0.0)
        selected = max(
            [(1_000, smaller), (20_000, larger)],
            key=lambda item: absolute_profit_key(item[0], item[1]),
        )
        self.assertEqual(selected[0], 1_000)


if __name__ == "__main__":
    unittest.main()
