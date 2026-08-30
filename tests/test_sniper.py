import os
import subprocess
import tempfile
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
    failure_category,
    execution_detail,
    execution_reference,
    jupiter_market_key,
    parse_args,
    process_is_running,
    route_execution_floor,
    run_route,
    selected_routes,
    strict_execution_floor,
    subprocess_output_text,
    unresolved_submission,
)


class CrosschainSniperTests(unittest.TestCase):
    def test_threshold_is_strictly_greater_than_five_dollars(self):
        self.assertEqual(
            strict_execution_floor(Decimal("5")),
            Decimal("5.000001"),
        )
        with self.assertRaisesRegex(SniperError, "at least 5"):
            strict_execution_floor(Decimal("4.999999"))

    def test_solana_routes_use_one_dollar_floor(self):
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
            Decimal("1.000001"),
        )
        self.assertEqual(
            route_execution_floor(Route("solana", "USDC/PYUSD"), Decimal("5")),
            Decimal("1.000001"),
        )
        self.assertEqual(
            route_execution_floor(Route("ethereum", "USDG/PYUSD"), Decimal("5")),
            Decimal("5.000001"),
        )
        self.assertEqual(
            route_execution_floor(Route("ethereum", "PYUSD/USDC"), Decimal("5")),
            Decimal("5.000001"),
        )
        with self.assertRaisesRegex(SniperError, "at least 1"):
            route_execution_floor(Route("solana", "PYUSD/USDC"), Decimal("0.999999"))

    def test_ethereum_route_has_both_onchain_and_net_profit_guards(self):
        invocation = build_route_invocation(
            Route("ethereum", "USDG/USDC"),
            live=True,
            execution_floor=Decimal("5.000001"),
        )
        command = list(invocation.command)

        self.assertEqual(command[command.index("--loan-token") + 1], "USDG")
        self.assertEqual(command[command.index("--intermediate-token") + 1], "USDC")
        self.assertEqual(command[command.index("--swap-order") + 1], "stable-first")
        self.assertEqual(command[command.index("--min-profit") + 1], "5.000001")
        self.assertEqual(
            command[command.index("--min-net-profit") + 1], "5.000001"
        )
        self.assertIn("--send", command)
        self.assertIn("EXECUTE_ATOMIC_ARB", command)

    def test_ethereum_pair_maps_to_stable_input_and_output(self):
        invocation = build_route_invocation(
            Route("ethereum", "USDG/PYUSD"),
            live=False,
            execution_floor=Decimal("5.000001"),
        )
        command = list(invocation.command)

        self.assertEqual(command[command.index("--loan-token") + 1], "USDG")
        self.assertEqual(
            command[command.index("--intermediate-token") + 1], "PYUSD"
        )
        self.assertTrue(command[command.index("--output") + 1].endswith(
            "ethereum-stable-first-loan-usdg-via-pyusd.json"
        ))

    def test_ethereum_dex_first_route_keeps_the_same_loan_and_reverses_venues(self):
        route = Route("ethereum", "PYUSD/USDC", "dex-first")
        invocation = build_route_invocation(
            route,
            live=False,
            execution_floor=Decimal("5.000001"),
        )
        command = list(invocation.command)

        self.assertEqual(command[command.index("--loan-token") + 1], "PYUSD")
        self.assertEqual(command[command.index("--intermediate-token") + 1], "USDC")
        self.assertEqual(command[command.index("--swap-order") + 1], "dex-first")
        self.assertEqual(
            route.display,
            "PYUSD -> USDC (MetaMatcha) -> PYUSD (Stable.com)",
        )

    def test_solana_route_sets_pair_and_strict_profit_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            invocation = build_route_invocation(
                Route("solana", "PYUSD/USDC"),
                live=True,
                execution_floor=Decimal("5.000001"),
            )

        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_INTERMEDIATE_TOKEN"], "USDC"
        )
        self.assertEqual(invocation.environment["SOL_FLASH_ARB_LOAN_TOKEN"], "PYUSD")
        self.assertEqual(invocation.environment["SOL_FLASH_ARB_SWAP_ORDER"], "stable-first")
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_MIN_GROSS_PROFIT_USDC"],
            "5.000001",
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_MIN_NET_PROFIT_USDC"],
            "5.000001",
        )
        self.assertNotIn("SOL_FLASH_ARB_SLIPPAGE_BPS", invocation.environment)
        self.assertIn("EXECUTE_SOLANA_FLASH_ARB", invocation.command)

    def test_solana_cross_token_pair_sets_both_route_tokens(self):
        with patch.dict(os.environ, {}, clear=True):
            invocation = build_route_invocation(
                Route("solana", "PYUSD/USDG"),
                live=False,
                execution_floor=Decimal("5.000001"),
            )

        self.assertEqual(invocation.environment["SOL_FLASH_ARB_LOAN_TOKEN"], "PYUSD")
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_INTERMEDIATE_TOKEN"], "USDG"
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_MIN_GROSS_PROFIT_PYUSD"],
            "5.000001",
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_MIN_NET_PROFIT_PYUSD"],
            "5.000001",
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"], "false"
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS"], "24"
        )
        self.assertTrue(invocation.environment["SOL_FLASH_ARB_OUTPUT_PATH"].endswith(
            "solana-stable-first-loan-pyusd-via-usdg.json"
        ))

    def test_solana_dex_first_route_reaches_jupiter_before_stable(self):
        with patch.dict(os.environ, {}, clear=True):
            invocation = build_route_invocation(
                Route("solana", "USDC/PYUSD", "dex-first"),
                live=False,
                execution_floor=Decimal("1.000001"),
            )

        self.assertEqual(invocation.environment["SOL_FLASH_ARB_LOAN_TOKEN"], "USDC")
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_INTERMEDIATE_TOKEN"], "PYUSD"
        )
        self.assertEqual(invocation.environment["SOL_FLASH_ARB_SWAP_ORDER"], "dex-first")
        command = list(invocation.command)
        self.assertEqual(command[command.index("--swap-order") + 1], "dex-first")

    def test_solana_stable_first_cross_pair_allows_constrained_multihop(self):
        with patch.dict(os.environ, {}, clear=True):
            invocation = build_route_invocation(
                Route("solana", "USDG/PYUSD"),
                live=False,
                execution_floor=Decimal("1.000001"),
            )

        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"], "false"
        )
        self.assertEqual(
            invocation.environment["SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS"], "24"
        )

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
            '{"transactionHash":"abc123","transactionStatus":"confirmed",'
            '"transaction":{"data":"secret-noise"}}',
        )
        sol = execution_detail(
            Route("solana", "USDG/USDC"),
            "Submitted: something\nConfirmed: 5HueCGU8rMjxEXxiPuD5BDu",
        )

        self.assertEqual(eth, "https://etherscan.io/tx/0xabc123")
        self.assertEqual(sol, "https://solscan.io/tx/5HueCGU8rMjxEXxiPuD5BDu")

    def test_submitted_transactions_are_not_reported_as_confirmed(self):
        eth_output = '{"transactionHash":"abc123","transactionStatus":"submitted"}'
        sol_output = "Submitted: https://solscan.io/tx/5HueCGU8rMjxEXxiPuD5BDu"

        self.assertIsNone(
            execution_detail(Route("ethereum", "PYUSD/USDC"), eth_output)
        )
        self.assertEqual(
            execution_reference(Route("ethereum", "PYUSD/USDC"), eth_output),
            ("submitted", "https://etherscan.io/tx/0xabc123"),
        )
        self.assertEqual(
            execution_reference(Route("solana", "PYUSD/USDC"), sol_output),
            ("submitted", "https://solscan.io/tx/5HueCGU8rMjxEXxiPuD5BDu"),
        )
        self.assertEqual(
            execution_reference(
                Route("solana", "PYUSD/USDC"),
                '{"transactionStatus":"submitted",'
                '"transactionSignature":"5HueCGU8rMjxEXxiPuD5BDu"}',
            ),
            ("submitted", "https://solscan.io/tx/5HueCGU8rMjxEXxiPuD5BDu"),
        )
        self.assertEqual(
            execution_reference(
                Route("ethereum", "PYUSD/USDC"),
                "Submitted: https://etherscan.io/tx/0xabc123",
            ),
            ("submitted", "https://etherscan.io/tx/0xabc123"),
        )
        self.assertEqual(subprocess_output_text(b"submitted"), "submitted")

    def test_expired_unrecorded_solana_signature_is_terminal(self):
        signature = "5HueCGU8rMjxEXxiPuD5BDu"
        output = (
            f"Submitted: https://solscan.io/tx/{signature}\n"
            f"Expired: {signature}\n"
        )

        self.assertEqual(
            execution_reference(Route("solana", "PYUSD/USDG"), output),
            ("expired", f"https://solscan.io/tx/{signature}"),
        )
        with patch(
            "crosschain_sniper.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=("engine",),
                returncode=0,
                stdout=output,
                stderr="",
            ),
        ):
            outcome = run_route(
                Route("solana", "PYUSD/USDG"),
                live=True,
                execution_floor=Decimal("1.000001"),
                timeout_seconds=300,
            )

        self.assertEqual(outcome.category, "expired")
        self.assertFalse(outcome.executed)
        self.assertIn("continuing", outcome.detail)

    def test_engine_timeout_preserves_an_already_broadcast_transaction(self):
        timeout = subprocess.TimeoutExpired(
            cmd=("engine",),
            timeout=300,
            stderr=(
                b"Submitted: https://etherscan.io/tx/"
                b"0x67e4b7a66ac23a980fc72dad0b6bf262ad00d2b5c8572d8e40a11d551e307683"
            ),
        )
        with patch("crosschain_sniper.subprocess.run", side_effect=timeout):
            outcome = run_route(
                Route("ethereum", "USDG/PYUSD"),
                live=True,
                execution_floor=Decimal("5.000001"),
                timeout_seconds=300,
            )

        self.assertEqual(outcome.category, "submitted")
        self.assertIn("67e4b7", outcome.detail)

    def test_unresolved_route_plan_blocks_restart(self):
        route = Route("ethereum", "USDG/PYUSD")
        with tempfile.TemporaryDirectory() as temporary:
            plan_path = Path(temporary) / f"{route.key}.json"
            plan_path.write_text(
                '{"transactionStatus":"submitted","transactionHash":"0xabc123"}',
                encoding="utf-8",
            )
            with patch("crosschain_sniper.PLAN_DIR", Path(temporary)):
                unresolved = unresolved_submission([route])

        self.assertIsNotNone(unresolved)
        self.assertEqual(unresolved[0], route)
        self.assertEqual(unresolved[2], "0xabc123")

    def test_expired_solana_plan_does_not_block_restart(self):
        route = Route("solana", "PYUSD/USDG")
        with tempfile.TemporaryDirectory() as temporary:
            plan_path = Path(temporary) / f"{route.key}.json"
            plan_path.write_text(
                '{"transactionStatus":"expired",'
                '"transactionSignature":"5HueCGU8rMjxEXxiPuD5BDu"}',
                encoding="utf-8",
            )
            with patch("crosschain_sniper.PLAN_DIR", Path(temporary)):
                unresolved = unresolved_submission([route])

        self.assertIsNone(unresolved)

    def test_unresolved_legacy_plan_is_not_missed_after_route_key_changes(self):
        route = Route("ethereum", "PYUSD/USDC", "dex-first")
        with tempfile.TemporaryDirectory() as temporary:
            legacy_path = Path(temporary) / "ethereum-pyusd-usdc.json"
            legacy_path.write_text(
                '{"transactionStatus":"submitted","transactionHash":"0xlegacy"}',
                encoding="utf-8",
            )
            with patch("crosschain_sniper.PLAN_DIR", Path(temporary)):
                unresolved = unresolved_submission([route])

        self.assertIsNotNone(unresolved)
        self.assertIsNone(unresolved[0])
        self.assertEqual(unresolved[1], legacy_path)
        self.assertEqual(unresolved[2], "0xlegacy")

    def test_logged_failures_receive_safe_retry_categories(self):
        self.assertEqual(
            failure_category(
                'Stable.com status failed: Stable.com status returned HTTP 500: '
                '{"message":"Connection rate limits exceeded"}'
            ),
            "transient-stable",
        )
        self.assertEqual(
            failure_category(
                'Jupiter quote failed: HTTP 400: {"errorCode":"NO_ROUTES_FOUND"}'
            ),
            "no-route",
        )
        self.assertEqual(
            failure_category(
                "https://meta.matcha.xyz/api/competitions access blocked by "
                "Cloudflare (HTTP 403); the provider may reject "
                "data-center egress IPs"
            ),
            "access-blocked-matcha",
        )
        self.assertEqual(
            failure_category(
                "https://api.0x.org/swap/allowance-holder/quote returned HTTP 401"
            ),
            "access-blocked-matcha",
        )
        self.assertEqual(
            failure_category(
                "https://api.0x.org/swap/allowance-holder/quote returned HTTP 503"
            ),
            "transient-matcha",
        )
        self.assertEqual(
            failure_category(
                "Atomic transaction exceeds Solana 1232-byte size limit"
            ),
            "no-route",
        )
        self.assertEqual(
            failure_category(
                "Stable.com pool capacity is below its minimum order: 0.1 < 1000"
            ),
            "capacity",
        )

    def test_cross_pair_directions_use_their_actual_jupiter_return_markets(self):
        self.assertEqual(
            jupiter_market_key(Route("solana", "USDG/PYUSD")),
            "jupiter:PYUSD/USDG",
        )
        self.assertEqual(
            jupiter_market_key(Route("solana", "PYUSD/USDG")),
            "jupiter:USDG/PYUSD",
        )

    def test_stop_request_has_an_explicit_cli_flag(self):
        self.assertTrue(parse_args(["--request-stop"]).request_stop)

    def test_provider_access_block_uses_a_long_default_cooldown(self):
        self.assertEqual(parse_args([]).provider_access_cooldown_seconds, 3600)

    def test_process_check_distinguishes_this_process_from_a_stale_pid(self):
        self.assertTrue(process_is_running(os.getpid()))
        self.assertFalse(process_is_running(2_000_000_000))

    def test_cross_token_pairs_are_in_the_default_rotation(self):
        args = parse_args([])
        pairs = args.pairs
        self.assertEqual(len(pairs), 6)
        for stable_from in ("USDC", "USDG", "PYUSD"):
            for stable_to in ("USDC", "USDG", "PYUSD"):
                if stable_from != stable_to:
                    self.assertIn(f"{stable_from}/{stable_to}", pairs)
        self.assertEqual(args.swap_orders, ["dex-first", "stable-first"])
        self.assertEqual(
            len(selected_routes(args.chains, args.pairs, args.swap_orders)),
            24,
        )

    def test_route_display_explains_both_venues(self):
        route = Route("ethereum", "PYUSD/USDC")

        self.assertEqual(route.loan, "PYUSD")
        self.assertEqual(route.intermediate, "USDC")
        self.assertEqual(
            route.display,
            "PYUSD -> USDC (Stable.com) -> PYUSD (MetaMatcha)",
        )

    def test_market_key_tracks_the_actual_dex_leg_for_each_order(self):
        self.assertEqual(
            jupiter_market_key(Route("solana", "PYUSD/USDC", "dex-first")),
            "jupiter:PYUSD/USDC",
        )
        self.assertEqual(
            jupiter_market_key(Route("solana", "PYUSD/USDC", "stable-first")),
            "jupiter:USDC/PYUSD",
        )

    def test_pyusd_usdc_family_contains_all_four_requested_loan_order_variants(self):
        routes = selected_routes(
            ["ethereum"],
            ["PYUSD/USDC", "USDC/PYUSD"],
            ["dex-first", "stable-first"],
        )

        self.assertEqual(
            {route.display for route in routes},
            {
                "PYUSD -> USDC (MetaMatcha) -> PYUSD (Stable.com)",
                "PYUSD -> USDC (Stable.com) -> PYUSD (MetaMatcha)",
                "USDC -> PYUSD (MetaMatcha) -> USDC (Stable.com)",
                "USDC -> PYUSD (Stable.com) -> USDC (MetaMatcha)",
            },
        )


if __name__ == "__main__":
    unittest.main()
