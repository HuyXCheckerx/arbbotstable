import unittest

from sizing import (
    calculate_quote_metrics,
    generate_candidate_sizes,
    generate_refinement_sizes,
    is_profitable_candidate,
)


class DynamicSizingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
