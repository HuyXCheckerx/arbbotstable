import os
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
for sub in ("core", "recovery", "engines", "web", "deployers"):
    subpath = str(SRC_DIR / sub)
    if subpath not in sys.path:
        sys.path.insert(0, subpath)

from crosschain_sniper import (
    Route,
    SniperError,
    build_route_invocation,
    concise_failure,
    execution_detail,
    parse_args,
    route_execution_floor,
    strict_execution_floor,
)


class CrosschainSniperTests(unittest.TestCase):
    def test_threshold_is_strictly_greater_than_five_dollars(self):
        self.assertEqual(
            strict_execution_floor(Decimal("5")),
            Decimal("5.000001"),
        )
        with self.assertRaisesRegex(SniperError, "at least 5"):
            strict_execution_floor(Decimal("4.999999"))

    def test_solana_cross_token_routes_use_one_dollar_floor(self):
        self.assertEqual(
            route_execution_floor(Route("solana", "USDG/PYUSD"), Decimal("5")),
            Decimal("1.000001"),
        )
        self.assertEqual(
            route_execution_floor(Route("solana", "PYUSD/USDG"), Decimal("5")),
            Decimal("1.000001"),
        )
        self.assertEqual(
            route_execution_floor(Route("solana", "PYUSD/USDC"), Decimal("5")),
            Decimal("5.000001"),
        )
        self.assertEqual(
            route_execution_floor(Route("ethereum", "USDG/PYUSD"), Decimal("5")),
            Decimal("5.000001"),
        )
        with self.assertRaisesRegex(SniperError, "at least 1"):
            route_execution_floor(Route("solana", "USDG/PYUSD"), Decimal("0.999999"))

    def test_ethereum_route_has_both_onchain_and_net_profit_guards(self):
        invocation = build_route_invocation(
            Route("ethereum", "USDG/USDC"),
            live=True,
            execution_floor=Decimal("5.000001"),
        )
        command = list(invocation.command)

        self.assertEqual(command[command.index("--loan-token") + 1], "USDC")
        self.assertEqual(command[command.index("--intermediate-token") + 1], "USDG")
        self.assertEqual(command[command.index("--min-profit") + 1], "5.000001")
        self.assertEqual(
            command[command.index("--min-net-profit") + 1], "5.000001"
        )
        self.assertIn("--send", command)
        self.assertIn("EXECUTE_ATOMIC_ARB", command)

    def test_ethereum_cross_token_pair_maps_to_intermediate_and_loan(self):
        invocation = build_route_invocation(
            Route("ethereum", "USDG/PYUSD"),
            live=False,
            execution_floor=Decimal("5.000001"),
        )
        command = list(invocation.command)

        self.assertEqual(command[command.index("--loan-token") + 1], "PYUSD")
        self.assertEqual(
            command[command.index("--intermediate-token") + 1], "USDG"
        )
        self.assertTrue(command[command.index("--output") + 1].endswith(
            "ethereum-usdg-pyusd.json"
        ))

    def test_solana_route_sets_pair_and_strict_profit_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            invocation = build_route_invocation(
                Route("solana", "PYUSD/USDC"),
                live=True,
                execution_floor=Decimal("5.000001"),
            )

        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_INTERMEDIATE_TOKEN"], "PYUSD"
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_MIN_GROSS_PROFIT_USDC"],
            "5.000001",
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_MIN_NET_PROFIT_USDC"],
            "5.000001",
        )
        self.assertIn("EXECUTE_SOLANA_FLASH_ARB", invocation.command)

    def test_solana_cross_token_pair_sets_both_route_tokens(self):
        with patch.dict(os.environ, {}, clear=True):
            invocation = build_route_invocation(
                Route("solana", "PYUSD/USDG"),
                live=False,
                execution_floor=Decimal("5.000001"),
            )

        self.assertEqual(invocation.environment["SOL_FLASH_ARB_LOAN_TOKEN"], "USDG")
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_INTERMEDIATE_TOKEN"], "PYUSD"
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_MIN_GROSS_PROFIT_USDG"],
            "5.000001",
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_MIN_NET_PROFIT_USDG"],
            "5.000001",
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"], "false"
        )
        self.assertTrue(invocation.environment["SOL_FLASH_ARB_OUTPUT_PATH"].endswith(
            "solana-pyusd-usdg.json"
        ))

    def test_solana_usdc_pairs_preserve_the_configured_direct_route_policy(self):
        with patch.dict(
            os.environ,
            {"SOL_FLASH_ARB_ONLY_DIRECT_ROUTES": "true"},
            clear=True,
        ):
            invocation = build_route_invocation(
                Route("solana", "USDG/USDC"),
                live=False,
                execution_floor=Decimal("5.000001"),
            )

        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"], "true"
        )

    def test_failure_summary_prefers_actionable_engine_error(self):
        detail = concise_failure(
            "irrelevant output",
            "quote details\nERROR: guaranteed net is below 5.000001 USDC\n",
            2,
        )
        self.assertEqual(detail, "guaranteed net is below 5.000001 USDC")

    def test_execution_links_are_extracted_without_exposing_plan_data(self):
        eth = execution_detail(
            Route("ethereum", "PYUSD/USDC"),
            '{"transactionHash":"abc123","transaction":{"data":"secret-noise"}}',
        )
        sol = execution_detail(
            Route("solana", "USDG/USDC"),
            "Submitted: something\nConfirmed: 5HueCGU8rMjxEXxiPuD5BDu",
        )

        self.assertEqual(eth, "https://etherscan.io/tx/0xabc123")
        self.assertEqual(sol, "https://solscan.io/tx/5HueCGU8rMjxEXxiPuD5BDu")

    def test_stop_request_has_an_explicit_cli_flag(self):
        self.assertTrue(parse_args(["--request-stop"]).request_stop)

    def test_cross_token_pairs_are_in_the_default_rotation(self):
        pairs = parse_args([]).pairs
        self.assertIn("USDG/PYUSD", pairs)
        self.assertIn("PYUSD/USDG", pairs)


if __name__ == "__main__":
    unittest.main()
