from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.engines.crosschain_sniper import SniperError, parse_args, selected_routes
from src.engines.pyusd_usdg_sniper import main, restricted_sniper_args


class PyusdUsdgSniperTests(unittest.TestCase):
    def test_route_matrix_contains_only_pyusd_usdg_on_both_chains(self):
        args = parse_args(restricted_sniper_args(["--once"]))
        routes = selected_routes(args.chains, args.pairs, args.swap_orders)

        self.assertEqual(args.chains, ["ethereum", "solana"])
        self.assertEqual(args.pairs, ["PYUSD/USDG", "USDG/PYUSD"])
        self.assertEqual(args.swap_orders, ["dex-first", "stable-first"])
        self.assertTrue(args.once)
        self.assertEqual(len(routes), 8)
        self.assertTrue(
            all(
                {route.loan, route.intermediate} == {"PYUSD", "USDG"}
                for route in routes
            )
        )

    def test_route_selection_cannot_be_overridden(self):
        for option in (
            "--chains",
            "--pairs",
            "--routes",
            "--orders",
            "--swap-orders",
            "--pairs=PYUSD/USDC",
        ):
            with self.subTest(option=option):
                with self.assertRaisesRegex(SniperError, "routes are fixed"):
                    restricted_sniper_args([option])

    def test_operational_options_are_forwarded_to_shared_sniper(self):
        with patch(
            "src.engines.pyusd_usdg_sniper.crosschain_main",
            return_value=17,
        ) as shared_main:
            result = main(["--once", "--threshold-usd", "7"])

        self.assertEqual(result, 17)
        forwarded = shared_main.call_args.args[0]
        self.assertEqual(forwarded[:3], ["--once", "--threshold-usd", "7"])
        parsed = parse_args(forwarded)
        self.assertEqual(str(parsed.threshold_usd), "7")
        self.assertTrue(parsed.once)

    def test_help_describes_the_fixed_scope(self):
        output = StringIO()
        with redirect_stdout(output):
            result = main(["--help"])

        self.assertEqual(result, 0)
        self.assertIn("eight fixed routes", output.getvalue())
        self.assertIn(
            "Route-selection flags are intentionally disabled",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
