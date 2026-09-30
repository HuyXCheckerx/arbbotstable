import os
import json
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch


SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
for sub in ("core", "recovery", "engines", "web", "deployers"):
    subpath = str(SRC_DIR / sub)
    if subpath not in sys.path:
        sys.path.insert(0, subpath)

from crosschain_sniper import (
    DEFAULT_ROUTE_PAIRS,
    DEFAULT_SWAP_ORDERS,
    AdaptiveBackoff,
    CooldownPolicy,
    active_routes,
    arbitrage_groups,
    Outcome,
    Route,
    SniperDashboardFeed,
    SniperError,
    build_route_invocation,
    concise_failure,
    failure_category,
    execution_detail,
    execution_reference,
    fetch_ethereum_base_fee_gwei,
    jupiter_market_key,
    dex_market_key,
    parse_args,
    parse_gas_fee_gwei,
    process_is_running,
    profit_metrics,
    readable_failure,
    retry_after_seconds,
    route_execution_floor,
    run_ethereum_route_direct,
    run_route,
    selected_routes,
    strict_execution_floor,
    subprocess_output_text,
    unresolved_submission,
    worker,
    is_subprocess_mocked,
)


METAMATCHA_FORBIDDEN_ERRORS = {
    "ethereum": (
        'https://meta.matcha.xyz/api/gas?chainId=1 returned HTTP 403: '
        '{"error":{"code":"403","message":"Forbidden",'
        '"id":"arn1::4wwp8-1788836640966-b9cf7ffd52c6"}}'
    ),
    "solana": (
        'MetaMatcha quote failed: MetaMatcha quote failed: HTTP 403: '
        '{"error":{"code":"403","message":"Forbidden",'
        '"id":"arn1::ngxw9-1788836646033-ffb2c3857a0d"}}'
    ),
}


class CrosschainSniperTests(unittest.TestCase):
    def test_profit_metrics_support_both_engine_output_formats(self):
        ethereum = json.dumps(
            {"grossProfit": "6.25", "predictedNetProfit": "5.75"}
        )
        self.assertEqual(
            profit_metrics(Route("ethereum", "PYUSD/USDC"), ethereum),
            ("6.25", "5.75"),
        )
        solana = (
            "Guaranteed gross result: 2.500000 USDC\n"
            "Guaranteed net result: 1.250000 USDC\n"
        )
        self.assertEqual(
            profit_metrics(Route("solana", "USDC/PYUSD"), solana),
            ("2.5", "1.25"),
        )
        self.assertEqual(
            profit_metrics(
                Route("ethereum", "USDC/USDG"),
                '{"grossProfit":"0","predictedNetProfit":"0.000000"}',
            ),
            ("0", "0"),
        )
        self.assertEqual(
            profit_metrics(
                Route("ethereum", "PYUSD/USDC"),
                "",
                "quoted route is below the on-chain profit floor: "
                "-10.956067 PYUSD < 5.000001 PYUSD",
            ),
            ("-10.956067", None),
        )

    def test_dashboard_feed_records_check_profit_and_execution_result(self):
        route = Route("ethereum", "PYUSD/USDC", "dex-first")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dashboard.json"
            feed = SniperDashboardFeed(
                path,
                [route],
                live=True,
                base_threshold=Decimal("5"),
            )
            feed.begin_check(route, Decimal("5.000001"))
            checking = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(checking["active"]["ethereum"]["state"], "CHECKING")

            feed.record_result(
                route,
                Decimal("5.000001"),
                Outcome(
                    True,
                    "confirmed on chain",
                    "confirmed",
                    gross_profit="7",
                    net_profit="6",
                    profit_token="PYUSD",
                    elapsed_seconds=2.5,
                    transaction="https://etherscan.io/tx/0xabc",
                ),
            )
            recorded = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(recorded["active"]["ethereum"])
            self.assertEqual(recorded["summary"]["checks"], 1)
            self.assertEqual(recorded["summary"]["confirmed"], 1)
            self.assertEqual(recorded["routes"][route.key]["net_profit"], "6")
            self.assertEqual(recorded["last_execution"]["state"], "CONFIRMED")

            feed.begin_check(route, Decimal("5.000001"))
            refreshing = feed.snapshot()
            self.assertEqual(refreshing["routes"][route.key]["state"], "CHECKING")
            self.assertEqual(refreshing["routes"][route.key]["net_profit"], "6")
            self.assertEqual(refreshing["routes"][route.key]["checked_at"], recorded["routes"][route.key]["checked_at"])
            refreshing["routes"][route.key]["net_profit"] = "999"
            self.assertEqual(feed.snapshot()["routes"][route.key]["net_profit"], "6")
            feed.record_result(route, Decimal("5.000001"), Outcome(False, "RPC unavailable", "transient-rpc"))
            self.assertIsNone(feed.snapshot()["routes"][route.key]["net_profit"])

            feed.stop()
            stopped = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stopped["session"]["status"], "stopped")

    def test_threshold_is_strictly_greater_than_four_dollars(self):
        self.assertEqual(
            strict_execution_floor(Decimal("4")),
            Decimal("4.000001"),
        )
        with self.assertRaisesRegex(SniperError, "at least 4"):
            strict_execution_floor(Decimal("3.999999"))

    def test_solana_routes_use_one_dollar_base_floor(self):
        self.assertEqual(
            route_execution_floor(Route("solana", "USDG/PYUSD"), Decimal("4")),
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
        with self.assertRaisesRegex(SniperError, "at least 0.01"):
            route_execution_floor(Route("solana", "PYUSD/USDC"), Decimal("0.009999"))

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
        self.assertEqual(command[command.index("--base-amount") + 1], "100000")
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
            invocation.environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"], "true"
        )
        self.assertNotIn("SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS", invocation.environment)
        self.assertTrue(invocation.environment["SOL_FLASH_ARB_OUTPUT_PATH"].endswith(
            "solana-stable-first-loan-pyusd-via-usdg.json"
        ))

    def test_solana_dex_first_route_reaches_metamatcha_before_stable(self):
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

    def test_explicit_jupiter_cross_pair_allows_constrained_multihop(self):
        with patch.dict(
            os.environ,
            {"SOL_FLASH_ARB_DEX_PROVIDER": "jupiter"},
            clear=True,
        ):
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

    def test_failure_summary_preserves_multiline_candidate_details(self):
        stderr = (
            "ERROR: All candidate quotes failed atomic prerun simulation or fell below the profit floor:\n"
            "  - Aggregator #1: Atomic simulation reverted: {\"InstructionError\":[3,{\"Custom\":6026}]}\n"
        )
        detail = concise_failure("", stderr, 1)
        self.assertIn("Custom", detail)
        self.assertIn("6026", detail)
        cat = failure_category(detail)
        self.assertEqual(cat, "marginfi-utilization")
        readable = readable_failure(Route("solana", "PYUSD/USDG", "dex-first"), detail, cat)
        self.assertIn("Marginfi PYUSD bank utilization is >100%", readable)

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

    def test_unresolved_submission_does_not_stop_the_script(self):
        stop = threading.Event()
        policy = CooldownPolicy(
            transient_base_seconds=30,
            transient_max_seconds=300,
            provider_access_seconds=3600,
            no_route_seconds=300,
            capacity_seconds=300,
            unstable_capacity_seconds=30,
            reverted_seconds=60,
        )
        with patch(
            "crosschain_sniper.run_route",
            return_value=Outcome(False, "receipt was not observed", "submitted"),
        ):
            worker(
                "ethereum",
                [Route("ethereum", "PYUSD/USDG")],
                live=True,
                base_threshold=Decimal("5"),
                interval_seconds=1,
                cooldown_seconds=15,
                timeout_seconds=300,
                cooldown_policy=policy,
                backoff=AdaptiveBackoff(),
                once=True,
                stop=stop,
                logger=Mock(),
            )

        self.assertFalse(stop.is_set())

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

    def test_provider_access_block_defaults_to_ten_seconds(self):
        self.assertEqual(parse_args([]).provider_access_cooldown_seconds, 10)

    def test_plain_metamatcha_denials_pause_with_chain_specific_guidance(self):
        for chain, detail in METAMATCHA_FORBIDDEN_ERRORS.items():
            with self.subTest(chain=chain):
                category = failure_category(detail)
                self.assertEqual(category, "access-blocked-matcha")
                message = readable_failure(Route(chain, "PYUSD/USDG"), detail, category)
                self.assertIn("HTTP 403", message)
                self.assertIn("check provider access", message)
                self.assertNotIn("Cloudflare", message)
                self.assertNotIn("network", message)
                if chain == "solana":
                    self.assertIn("SOL_FLASH_ARB_DEX_PROVIDER=jupiter", message)
                    self.assertNotIn("0x", message)
                else:
                    self.assertIn("official 0x quote provider", message)
                    self.assertNotIn("Jupiter", message)

    def test_access_denials_require_a_matcha_or_zero_ex_provider_marker(self):
        for detail in (
            'Stable.com quote failed: HTTP 403: {"message":"Forbidden"}',
            'Jupiter quote failed: HTTP 401: {"message":"Unauthorized"}',
            'RPC returned HTTP 403: {"message":"Forbidden"}',
            'MetaMatcha quote failed: HTTP 400: {"message":"Invalid amount"}',
        ):
            with self.subTest(detail=detail):
                self.assertEqual(failure_category(detail), "failure")
        self.assertEqual(
            failure_category('MetaMatcha quote failed: HTTP 401: Unauthorized'),
            "access-blocked-matcha",
        )

    def test_vercel_checkpoint_is_an_access_block_instead_of_a_rate_limit(self):
        detail = (
            "https://meta.matcha.xyz/api/competitions?token=secret "
            "access blocked by Vercel Security Checkpoint "
            "(HTTP 429; x-vercel-mitigated=challenge); request-id=test::123"
        )
        for chain in ("ethereum", "solana"):
            with self.subTest(chain=chain):
                category = failure_category(detail)
                self.assertEqual(category, "access-blocked-matcha")
                readable = readable_failure(Route(chain, "USDC/USDG"), detail, category)
                self.assertIn("Vercel Security Checkpoint (HTTP 429)", readable)
                self.assertIn("https://meta.matcha.xyz/api/competitions;", readable)
                self.assertIn("browser verification is required", readable)
                self.assertIn("request-id=test::123", readable)
                self.assertNotIn("token=secret", readable)
                self.assertNotIn("temporarily unavailable", readable)
        self.assertEqual(
            failure_category("MetaMatcha quote failed: HTTP 429: Too Many Requests"),
            "transient-matcha",
        )
        self.assertEqual(
            failure_category("Stable.com HTTP 429: Vercel Security Checkpoint"),
            "transient-stable",
        )
        self.assertEqual(
            failure_category(
                "https://api-defi.stable.com/swap/status returned HTTP 520: "
                '{"type":"...cloudflare...error-520/","title":"Error 520"}'
            ),
            "transient-stable",
        )
        self.assertEqual(
            failure_category("MetaMatcha HTTP 429; x-vercel-mitigated=challenge"),
            "access-blocked-matcha",
        )

    def test_retry_after_accepts_only_finite_nonnegative_normalized_waits(self):
        self.assertEqual(retry_after_seconds("MetaMatcha HTTP 429; retry-after=120.5s"), 120.5)
        self.assertEqual(retry_after_seconds("retry-after=0s"), 0)
        self.assertEqual(retry_after_seconds("retry-after=20s; retry-after=45s"), 45)
        for value in ("NaN", "inf", "-1", "1e999", "invalid"):
            with self.subTest(value=value):
                self.assertIsNone(retry_after_seconds(f"retry-after={value}s"))

    def test_rate_limit_wait_survives_readable_error_and_defers_next_route(self):
        route = Route("ethereum", "USDC/USDG", "dex-first")
        detail = "https://meta.matcha.xyz/api/competitions returned HTTP 429: Too Many Requests; retry-after=900s"
        policy = CooldownPolicy(30, 300, 3600, 300, 300, 30, 60)
        backoff = AdaptiveBackoff()
        dashboard = Mock()
        with patch.dict(os.environ, {}, clear=True), patch(
            "crosschain_sniper.time.monotonic", return_value=100,
        ), patch(
            "crosschain_sniper.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=("engine",), returncode=1, stdout="", stderr=f"ERROR: {detail}\n",
            ),
        ) as engine:
            worker(
                "ethereum", [route, Route("ethereum", "USDG/USDC", "dex-first")],
                live=False, base_threshold=Decimal("5"), interval_seconds=1,
                cooldown_seconds=15, timeout_seconds=300, cooldown_policy=policy,
                backoff=backoff, once=True, stop=threading.Event(), logger=Mock(),
                dashboard=dashboard,
            )
            self.assertEqual(engine.call_count, 1)
            outcome = dashboard.record_result.call_args.args[2]
            self.assertEqual(outcome.category, "transient-matcha")
            self.assertEqual(outcome.retry_after_seconds, 900)
            self.assertNotIn("retry-after", outcome.detail)
            self.assertEqual(backoff.remaining(("metamatcha:ethereum",)), 10)
            self.assertEqual(dashboard.record_cooldown.call_args.args[1], 10)

    def test_retry_after_and_exponential_backoff_obey_hard_ceiling(self):
        backoff = AdaptiveBackoff()
        with patch("crosschain_sniper.time.monotonic", return_value=100):
            backoff.block("provider", 900)
            self.assertEqual(backoff.fail("provider", 30, 300, minimum_seconds=10), 10)
            self.assertEqual(backoff.remaining(("provider",)), 10)
            self.assertEqual(backoff.fail("another-provider", 30, 300, minimum_seconds=10), 10)
            self.assertEqual(backoff.fail("another-provider", 30, 300, minimum_seconds=10), 10)
        with patch("crosschain_sniper.time.monotonic", return_value=131):
            self.assertEqual(backoff.fail("another-provider", 30, 300, minimum_seconds=10), 10)

    def test_all_route_pause_categories_obey_ten_seconds_with_legacy_settings(self):
        from crosschain_sniper import _handle_route_outcome
        policy = CooldownPolicy(30, 300, 3600, 300, 300, 30, 60)
        self.assertTrue(all(value <= 10 for value in vars(policy).values()))
        for category in ("submitted", "transient-stable", "transient-jupiter", "transient-matcha",
                         "transient-rpc", "no-route", "access-blocked-matcha", "flash-liquidity",
                         "reverted", "confirmed"):
            with self.subTest(category=category), patch("crosschain_sniper.time.monotonic", return_value=100):
                route = Route("solana", "USDC/USDG")
                deadlines, backoff, dashboard, stop = {}, AdaptiveBackoff(), Mock(), Mock()
                stop.wait.return_value = False
                _handle_route_outcome(route, Decimal("1"),
                    Outcome(category == "confirmed", "test", category, retry_after_seconds=900),
                    chain="solana", cooldown_policy=policy, backoff=backoff,
                    route_deadlines=deadlines, dashboard=dashboard, logger=Mock(),
                    cooldown_seconds=120, stop=stop)
                self.assertTrue(all(deadline <= 110 for deadline in deadlines.values()))
                self.assertTrue(all(deadline <= 110 for deadline in backoff._deadlines.values()))
                for call in dashboard.record_cooldown.call_args_list:
                    self.assertLessEqual(call.args[1], 10)
                for call in stop.wait.call_args_list:
                    self.assertLessEqual(call.args[0], 10)

    def test_parallel_successes_share_one_pause_instead_of_stacking_sleeps(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        routes = selected_routes(["solana"], ["USDC/USDG", "USDC/PYUSD", "USDG/PYUSD"], ["stable-first"])
        with patch("crosschain_sniper.run_route", return_value=Outcome(True, "confirmed", "confirmed")) as run, \
                patch("crosschain_sniper.time.monotonic", return_value=100):
            worker("solana", routes, live=False, base_threshold=Decimal("5"),
                   interval_seconds=60, cooldown_seconds=120, timeout_seconds=300,
                   cooldown_policy=CooldownPolicy(30, 300, 3600, 300, 300, 30, 60),
                   backoff=AdaptiveBackoff(), once=False, stop=stop, logger=Mock(), parallel_scanning=True)
        self.assertEqual(run.call_count, 3)
        stop.wait.assert_called_once_with(10)

    def test_short_retry_settings_stay_short_and_backoff_caps_after_growth(self):
        backoff = AdaptiveBackoff()
        for now, expected in ((100, 2), (103, 4), (108, 8), (117, 10), (128, 10)):
            with patch("crosschain_sniper.time.monotonic", return_value=now):
                self.assertEqual(backoff.fail("rpc:solana", 2, 300), expected)
        with patch("crosschain_sniper.time.monotonic", return_value=200):
            self.assertEqual(backoff.block("rpc:ethereum", 3), 3)

    def test_vercel_checkpoint_uses_the_access_cooldown_for_its_chain(self):
        policy = CooldownPolicy(30, 300, 3600, 300, 300, 30, 60)
        detail = (
            "https://meta.matcha.xyz/api/competitions access blocked by "
            "Vercel Security Checkpoint (HTTP 429; x-vercel-mitigated=challenge)"
        )
        for chain in ("ethereum", "solana"):
            with self.subTest(chain=chain):
                backoff = AdaptiveBackoff()
                dashboard = Mock()
                routes = selected_routes([chain], ["USDC/USDG", "USDG/USDC"], ["dex-first"])
                with patch.dict(os.environ, {}, clear=True), patch(
                    "crosschain_sniper.time.monotonic", return_value=100,
                ), patch(
                    "crosschain_sniper.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        args=("engine",), returncode=1, stdout="", stderr=f"ERROR: {detail}\n",
                    ),
                ) as engine:
                    worker(
                        chain, routes, live=False, base_threshold=Decimal("5"),
                        interval_seconds=1, cooldown_seconds=15, timeout_seconds=300,
                        cooldown_policy=policy, backoff=backoff, once=True,
                        stop=threading.Event(), logger=Mock(), dashboard=dashboard,
                    )
                    self.assertEqual(engine.call_count, 1)
                    self.assertEqual(dashboard.record_result.call_args.args[2].category, "access-blocked-matcha")
                    self.assertEqual(backoff.remaining((f"metamatcha:{chain}",)), 10)
                    other_chain = "ethereum" if chain == "solana" else "solana"
                    self.assertLessEqual(backoff.remaining((f"metamatcha:{other_chain}",)), 0)

    def test_plain_forbidden_errors_stop_route_checks_until_chain_cooldown_expires(self):
        policy = CooldownPolicy(
            transient_base_seconds=30,
            transient_max_seconds=300,
            provider_access_seconds=3600,
            no_route_seconds=300,
            capacity_seconds=300,
            unstable_capacity_seconds=30,
            reverted_seconds=60,
        )
        for chain, detail in METAMATCHA_FORBIDDEN_ERRORS.items():
            with self.subTest(chain=chain):
                routes = selected_routes(
                    [chain], ["PYUSD/USDG", "USDG/PYUSD"],
                    ["dex-first", "stable-first"],
                )
                backoff = AdaptiveBackoff()
                dashboard = Mock()
                kwargs = dict(
                    live=False,
                    base_threshold=Decimal("5"),
                    interval_seconds=1,
                    cooldown_seconds=15,
                    timeout_seconds=300,
                    cooldown_policy=policy,
                    backoff=backoff,
                    once=True,
                    stop=threading.Event(),
                    logger=Mock(),
                    dashboard=dashboard,
                )
                with patch.dict(os.environ, {}, clear=True), patch(
                    "crosschain_sniper.time.monotonic", return_value=100,
                ) as monotonic, patch(
                    "crosschain_sniper.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        args=("engine",), returncode=1,
                        stdout="", stderr=f"ERROR: {detail}\n",
                    ),
                ) as engine:
                    worker(chain, routes, **kwargs)
                    self.assertEqual(engine.call_count, 1)
                    outcome = dashboard.record_result.call_args.args[2]
                    self.assertEqual(outcome.category, "access-blocked-matcha")
                    self.assertFalse(outcome.executed)
                    self.assertEqual(
                        backoff.remaining((f"metamatcha:{chain}",)), 10,
                    )
                    other_chain = "solana" if chain == "ethereum" else "ethereum"
                    self.assertLessEqual(
                        backoff.remaining((f"metamatcha:{other_chain}",)), 0,
                    )

                    worker(chain, routes, **kwargs)
                    self.assertEqual(engine.call_count, 1)
                    monotonic.return_value = 111
                    worker(chain, routes, **kwargs)
                    self.assertEqual(engine.call_count, 2)

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
        with patch.dict(os.environ, {}, clear=True):
            solana = Route("solana", "PYUSD/USDC")
            self.assertEqual(
                solana.display,
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

    def test_parse_gas_fee_gwei(self):
        self.assertIsNone(parse_gas_fee_gwei(None))
        self.assertIsNone(parse_gas_fee_gwei(""))
        self.assertEqual(parse_gas_fee_gwei("1.0"), Decimal("1.0"))
        self.assertEqual(parse_gas_fee_gwei("1.5 gwei"), Decimal("1.5"))
        self.assertEqual(parse_gas_fee_gwei("1000000000 wei"), Decimal("1.0"))
        self.assertEqual(parse_gas_fee_gwei("1000000000"), Decimal("1.0"))
        self.assertEqual(parse_gas_fee_gwei(1), Decimal("1"))
        with self.assertRaises(SniperError):
            parse_gas_fee_gwei("invalid")
        with self.assertRaises(SniperError):
            parse_gas_fee_gwei("-1.0")

    def test_fetch_ethereum_base_fee_gwei(self):
        mock_response = Mock()
        mock_response.read.return_value = (
            b'{"jsonrpc":"2.0","id":1,"result":{"baseFeePerGas":"0x4b9f8600"}}'
        )
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            fee = fetch_ethereum_base_fee_gwei("http://mock-rpc")
            # 0x4b9f8600 in hex = 1268745728 wei = 1.268745728 Gwei
            self.assertEqual(fee, Decimal("1.268745728"))

    def test_eth_max_base_fee_gwei_flag(self):
        args = parse_args(["--eth-max-base-fee-gwei", "1.5"])
        self.assertEqual(args.eth_max_base_fee_gwei, Decimal("1.5"))

    def test_fetch_ethereum_base_fee_gwei_failover(self):
        # First call fails with exception, second call succeeds
        mock_success = Mock()
        mock_success.read.return_value = (
            b'{"jsonrpc":"2.0","id":1,"result":{"baseFeePerGas":"0x77359400"}}'
        )
        mock_success.__enter__ = Mock(return_value=mock_success)
        mock_success.__exit__ = Mock(return_value=False)

        def mock_urlopen(req, timeout=5.0):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "dead-rpc" in url:
                raise Exception("Connection timed out")
            return mock_success

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            fee = fetch_ethereum_base_fee_gwei(["http://dead-rpc.invalid", "http://live-rpc.invalid"])
            # 0x77359400 = 2000000000 wei = 2.0 Gwei
            self.assertEqual(fee, Decimal("2.0"))

    def test_is_subprocess_mocked_detection(self):
        self.assertFalse(is_subprocess_mocked())
        with patch("crosschain_sniper.subprocess.run"):
            self.assertTrue(is_subprocess_mocked())
        self.assertFalse(is_subprocess_mocked())

    def test_run_ethereum_route_direct_eligible(self):
        route = Route("ethereum", "USDC/USDG", "stable-first")
        mock_plan = {
            "mode": "dry-run",
            "pair": "USDC/USDG",
            "grossProfit": "12.500000",
            "predictedNetProfit": "8.000000",
            "transactionStatus": "dry-run",
        }
        with patch("src.engines.eth_flash_arb_pyusd_usdc.run", return_value=mock_plan), patch(
            "src.engines.eth_flash_arb_pyusd_usdc.write_plan"
        ):
            outcome = run_ethereum_route_direct(
                route,
                live=False,
                execution_floor=Decimal("4.000001"),
                timeout_seconds=30.0,
            )
            self.assertEqual(outcome.category, "eligible")
            self.assertEqual(outcome.gross_profit, "12.5")
            self.assertEqual(outcome.net_profit, "8")
            self.assertIn("guaranteed net 8.000000 USDC", outcome.detail)

    def test_run_ethereum_route_direct_unprofitable(self):
        from src.engines.eth_flash_arb_pyusd_usdc import ArbError
        route = Route("ethereum", "USDC/USDG", "stable-first")
        error_msg = "route is below the maximum-gas net-profit floor: -2.000000 USDC < 4.000001 USDC"
        with patch("src.engines.eth_flash_arb_pyusd_usdc.run", side_effect=ArbError(error_msg)):
            outcome = run_ethereum_route_direct(
                route,
                live=False,
                execution_floor=Decimal("4.000001"),
                timeout_seconds=30.0,
            )
            self.assertEqual(outcome.category, "unprofitable")
            self.assertIn("-2.000000 USDC", outcome.detail)

    def test_parallel_route_scanning_evaluates_all_eligible_routes(self):
        routes = [
            Route("ethereum", "USDC/USDG", "stable-first"),
            Route("ethereum", "USDC/PYUSD", "stable-first"),
        ]
        policy = CooldownPolicy(30, 300, 3600, 300, 300, 30, 60)
        backoff = AdaptiveBackoff()
        dashboard = Mock()
        logger = Mock()

        mock_outcome = Outcome(
            False,
            "dry run simulation passed",
            "eligible",
            gross_profit="10",
            net_profit="5",
        )
        with patch("crosschain_sniper.run_route", return_value=mock_outcome) as mock_run:
            worker(
                "ethereum",
                routes,
                live=False,
                base_threshold=Decimal("4"),
                interval_seconds=1.0,
                cooldown_seconds=15.0,
                timeout_seconds=30.0,
                cooldown_policy=policy,
                backoff=backoff,
                once=True,
                stop=threading.Event(),
                logger=logger,
                dashboard=dashboard,
                parallel_scanning=True,
            )
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(dashboard.record_result.call_count, 2)

    def test_unsupported_loan_token_classified_as_no_route(self):
        detail = "executor 0x6FA26637Db03519B520A44056fc4D93858Ba5833 does not support loan token 0xe343167631d89B6Ffc58B88d6b7fB0228795491D"
        self.assertEqual(failure_category(detail), "no-route")
        self.assertEqual(retry_after_seconds(detail), 86400.0)
        route = Route("ethereum", "USDC/USDG")
        readable = readable_failure(route, detail, "no-route")
        self.assertIn("Ethereum executor does not support USDC/USDG", readable)

    def test_dex_market_key_scoped_by_chain(self):
        eth_route = Route("ethereum", "USDC/USDG")
        sol_route = Route("solana", "USDC/USDG")
        eth_key = dex_market_key(eth_route)
        sol_key = dex_market_key(sol_route)
        self.assertEqual(eth_key, "metamatcha:ethereum:USDG/USDC")
        self.assertEqual(sol_key, "metamatcha:solana:USDG/USDC")
        self.assertNotEqual(eth_key, sol_key)


class EquivalentRouteSchedulingTests(unittest.TestCase):
    POLICY = CooldownPolicy(
        transient_base_seconds=30,
        transient_max_seconds=300,
        provider_access_seconds=3600,
        no_route_seconds=300,
        capacity_seconds=300,
        unstable_capacity_seconds=30,
        reverted_seconds=60,
    )
    PREFERRED = Route("solana", "PYUSD/USDG", "stable-first")
    TWIN = Route("solana", "USDG/PYUSD", "dex-first")

    def run_worker(self, outcomes, *, parallel):
        checked = []

        def fake_run_route(route, **_kwargs):
            checked.append(route.key)
            return outcomes[route.key]

        with patch("crosschain_sniper.run_route", side_effect=fake_run_route):
            worker(
                "solana",
                [self.PREFERRED, self.TWIN],
                live=False,
                base_threshold=Decimal("5"),
                interval_seconds=1,
                cooldown_seconds=15,
                timeout_seconds=300,
                cooldown_policy=self.POLICY,
                backoff=AdaptiveBackoff(),
                once=True,
                stop=threading.Event(),
                logger=Mock(),
                parallel_scanning=parallel,
            )
        return checked

    def test_routes_with_the_same_stable_leg_are_one_arbitrage(self):
        self.assertEqual(self.PREFERRED.arbitrage_key, self.TWIN.arbitrage_key)
        self.assertNotEqual(
            self.PREFERRED.arbitrage_key,
            Route("solana", "PYUSD/USDG", "dex-first").arbitrage_key,
        )
        self.assertNotEqual(
            self.PREFERRED.arbitrage_key,
            Route("ethereum", "PYUSD/USDG", "stable-first").arbitrage_key,
        )

    def test_default_routes_form_twelve_pairs_of_equivalent_routes(self):
        routes = selected_routes(
            ["ethereum", "solana"],
            list(DEFAULT_ROUTE_PAIRS),
            list(DEFAULT_SWAP_ORDERS),
        )
        groups = arbitrage_groups(routes)
        self.assertEqual(len(routes), 24)
        self.assertEqual(len(groups), 12)
        self.assertTrue(all(len(group) == 2 for group in groups))

    def test_active_routes_prefer_the_first_fundable_route(self):
        groups = [[self.PREFERRED, self.TWIN]]
        self.assertEqual(active_routes(groups, {}, 100.0), [self.PREFERRED])
        blocked = {self.PREFERRED.key: 200.0}
        self.assertEqual(active_routes(groups, blocked, 100.0), [self.TWIN])
        self.assertEqual(active_routes(groups, blocked, 200.0), [self.PREFERRED])
        both = {self.PREFERRED.key: 200.0, self.TWIN.key: 200.0}
        self.assertEqual(active_routes(groups, both, 100.0), [])

    def test_flash_liquidity_failures_are_classified_for_both_engines(self):
        for detail in (
            "no zero-fee flash provider has 100000 USDG; available: Morpho 12 USDG",
            "Morpho flash liquidity is 5 USDG, below requested 100000 USDG",
            "Marginfi flash loan is unavailable: headroom is exhausted",
            "Flash-loan liquidity is below the Stable.com minimum order: kamino can lend 3 PYUSD",
        ):
            with self.subTest(detail=detail):
                self.assertEqual(failure_category(detail), "flash-liquidity")

    def test_equivalent_route_is_not_quoted_when_the_preferred_route_is_fundable(self):
        outcomes = {
            self.PREFERRED.key: Outcome(False, "guaranteed gross -1 is below 5", "unprofitable"),
            self.TWIN.key: Outcome(False, "guaranteed gross -1 is below 5", "unprofitable"),
        }
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                self.assertEqual(self.run_worker(outcomes, parallel=parallel), [self.PREFERRED.key])

    def test_equivalent_route_is_quoted_in_the_same_pass_when_funding_fails(self):
        outcomes = {
            self.PREFERRED.key: Outcome(
                False,
                "Flash-loan liquidity is below the Stable.com minimum order",
                "flash-liquidity",
            ),
            self.TWIN.key: Outcome(False, "guaranteed gross -1 is below 5", "unprofitable"),
        }
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                self.assertEqual(
                    self.run_worker(outcomes, parallel=parallel),
                    [self.PREFERRED.key, self.TWIN.key],
                )

    def test_dashboard_marks_the_unchecked_equivalent_route_as_standby(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = SniperDashboardFeed(
                Path(directory) / "feed.json",
                [self.PREFERRED, self.TWIN],
                live=False,
                base_threshold=Decimal("5"),
            )
            feed.mark_standby(self.TWIN, self.PREFERRED)
            record = feed.snapshot()["routes"][self.TWIN.key]
        self.assertEqual(record["state"], "STANDBY")
        self.assertIn(self.PREFERRED.display, record["detail"])
        self.assertIn("PYUSD flash loan is unavailable", record["detail"])


if __name__ == "__main__":
    unittest.main()
