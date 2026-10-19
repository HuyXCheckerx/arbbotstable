import copy
from decimal import Decimal
import time
import unittest
from unittest.mock import patch

from scripts.quote_thorchain_usdp import (
    EthereumRpc, QuoteError, SCALE, USDC, USDP, amount_raw, approval_budget, choose_best, collect,
    eth_usdc_from_pools, main, memo_target, parser, quote_params,
    summarize_quote, validate_pools,
)


def fixture_quote():
    return {
        "expected_amount_out": "1001000000000",  # 10,010 USDP, NET of fees.
        "fees": {
            "asset": USDP, "liquidity": "2000000000", "affiliate": "100000000",
            "outbound": "200000000", "total": "2300000000", "slippage_bps": 20,
        },
        "expiry": int(time.time()) + 600,
        "recommended_min_amount_in": "1000000000",
        "recommended_gas_rate": "15", "gas_rate_units": "gwei",
        "router": "0xD37BbE5744D730a1d98d8DC97c42F0Ca46aD7146",
        "total_swap_seconds": 354, "max_streaming_quantity": 164,
        "memo": "=:ETH.USDP:0x1111111111111111111111111111111111111111:998998000000/0/164:symbiosis:1",
    }


def fixture_pools():
    return [
        {"asset": "ETH.ETH", "balance_rune": str(24000 * SCALE), "balance_asset": str(10 * SCALE), "status": "Available"},
        {"asset": USDC, "balance_rune": str(24000 * SCALE), "balance_asset": str(24000 * SCALE), "status": "Available"},
        {"asset": USDP, "balance_rune": str(24000 * SCALE), "balance_asset": str(24000 * SCALE), "status": "Available", "trading_halted": False},
    ]


class ThorchainUsdpQuoteTests(unittest.TestCase):
    def setUp(self):
        self.args = parser().parse_args(["--gas-cost-usdc", "5", "--slippage-bps", "20"])
        self.params = quote_params(self.args, "symbiosis", 0)

    def test_default_slippage_is_ten_bps_in_request_and_scenario(self):
        args = parser().parse_args(["--gas-cost-usdc", "5"])
        params = quote_params(args, "symbiosis", 0)
        self.assertEqual(args.slippage_bps, 10)
        self.assertEqual(params["liquidity_tolerance_bps"], 10)
        quote = fixture_quote()
        quote["memo"] = None
        row = summarize_quote(quote, params, args, None)
        self.assertEqual(Decimal(row["slippage_scenario_received_usdp"]), Decimal("9999.99"))

    def test_thornode_units_are_eight_decimals_but_input_respects_usdc_six(self):
        self.assertEqual(amount_raw(Decimal("10000")), 1_000_000_000_000)
        self.assertEqual(amount_raw(Decimal("0.000001")), 100)
        for invalid in ("0.0000001", "0", "-1", "NaN", "Infinity"):
            with self.assertRaises(QuoteError):
                amount_raw(Decimal(invalid))

    def test_does_not_subtract_embedded_fees_twice_or_gas_from_native_delivery(self):
        row = summarize_quote(fixture_quote(), self.params, self.args, Decimal("2400"))
        self.assertEqual(Decimal(row["expected_received_usdp"]), Decimal("10010"))
        self.assertEqual(Decimal(row["fees_usdp"]["total"]), Decimal("23"))
        self.assertEqual(Decimal(row["expected_parity_surplus_usdc_estimated"]), Decimal("5"))
        self.assertEqual(Decimal(row["slippage_scenario_received_usdp"]), Decimal("9989.98"))
        self.assertEqual(Decimal(row["slippage_scenario_parity_surplus_usdc_estimated"]), Decimal("-15.02"))
        self.assertEqual(Decimal(row["server_memo_trade_target_usdp"]), Decimal("9989.98"))

    def test_gas_budget_includes_optional_approval_and_uses_gwei_conversion(self):
        self.args.gas_cost_usdc = None
        self.args.gas_price_gwei = Decimal("15")  # Explicit override, not a quote fallback.
        row = summarize_quote(fixture_quote(), self.params, self.args, Decimal("2400"))
        self.assertEqual(Decimal(row["sender_gas_cost_usdc"]), Decimal("7.2"))
        self.args.approval_gas_units = 0
        row = summarize_quote(fixture_quote(), self.params, self.args, Decimal("2400"))
        self.assertEqual(Decimal(row["sender_gas_cost_usdc"]), Decimal("5.4"))

    def test_live_rpc_price_overrides_thorchain_fifteen_gwei_hint(self):
        self.args.gas_cost_usdc = None
        row = summarize_quote(fixture_quote(), self.params, self.args, Decimal("2400"), Decimal("0.015"))
        self.assertEqual(Decimal(row["sender_gas_cost_usdc"]), Decimal("0.0072"))
        self.assertEqual(row["sender_gas_model"]["source"], "rpc_gas_price")
        self.assertEqual(row["sender_gas_model"]["thorchain_recommended_gas_rate_unused"], "15")

    def test_missing_rpc_does_not_silently_fall_back_to_thorchain_hint(self):
        self.args.gas_cost_usdc = None
        row = summarize_quote(fixture_quote(), self.params, self.args, Decimal("2400"))
        self.assertIsNone(row["sender_gas_cost_usdc"])
        self.assertIsNone(row["expected_parity_surplus_usdc_estimated"])
        self.assertIsNone(choose_best([row], None))

    def test_allowance_sufficiency_uses_usdc_six_decimals_and_exact_router(self):
        self.args.owner = "0x1111111111111111111111111111111111111111"
        router = fixture_quote()["router"]
        for allowance, expected_gas, status in (
            (9_999_999_999, 50000, "insufficient"),
            (10_000_000_000, 0, "sufficient"),
            (2**256 - 1, 0, "sufficient"),
        ):
            with self.subTest(allowance=allowance):
                rpc = EthereumRpc("https://example.invalid")
                with patch.object(rpc, "allowance", return_value=allowance) as read:
                    result = approval_budget(fixture_quote(), self.args, rpc)
                self.assertEqual(result["gas_units"], expected_gas)
                self.assertEqual(result["status"], status)
                read.assert_called_once_with(self.args.owner, router)

    def test_sufficient_allowance_removes_approval_gas_from_net_estimate(self):
        self.args.gas_cost_usdc = None
        self.args.owner = "0x1111111111111111111111111111111111111111"
        rpc = EthereumRpc("https://example.invalid")
        with patch.object(rpc, "allowance", return_value=2**256 - 1):
            approval = approval_budget(fixture_quote(), self.args, rpc)
        row = summarize_quote(fixture_quote(), self.params, self.args, Decimal("2400"), Decimal("0.015"), approval)
        self.assertEqual(Decimal(row["sender_gas_cost_usdc"]), Decimal("0.0054"))
        self.assertEqual(row["sender_gas_model"]["approval_gas_units_assumed"], 0)

    def test_failed_allowance_check_never_assumes_preapproval(self):
        self.args.owner = "0x1111111111111111111111111111111111111111"
        rpc = EthereumRpc("https://example.invalid")
        with patch.object(rpc, "allowance", side_effect=QuoteError("RPC down")):
            result = approval_budget(fixture_quote(), self.args, rpc)
        self.assertEqual(result["gas_units"], 50000)
        self.assertEqual(result["status"], "unknown_rpc_error")

    def test_rpc_reads_allowance_abi_and_caches_per_owner_and_spender(self):
        owner = "0x1111111111111111111111111111111111111111"
        spender = fixture_quote()["router"]
        rpc = EthereumRpc("https://example.invalid")
        with patch.object(rpc, "call", side_effect=["0x1", "0x" + format(10_000_000_000, "064x")]) as calls:
            self.assertEqual(rpc.allowance(owner, spender), 10_000_000_000)
            self.assertEqual(rpc.allowance(owner, spender), 10_000_000_000)
        self.assertEqual(calls.call_count, 2)
        method, params = calls.call_args.args
        self.assertEqual(method, "eth_call")
        self.assertEqual(params[0]["data"], "0xdd62ed3e" + owner[2:].lower().zfill(64) + spender[2:].lower().zfill(64))
        self.assertEqual(params[1], "latest")

    def test_rpc_rejects_wrong_chain_and_disallows_transaction_submission(self):
        rpc = EthereumRpc("https://example.invalid")
        with patch.object(rpc, "call", return_value="0x89"):
            with self.assertRaisesRegex(QuoteError, "mainnet"):
                rpc.gas_price_gwei()
        with self.assertRaisesRegex(QuoteError, "not permitted"):
            rpc.call("eth_sendRawTransaction", ["0x"])

    def test_rpc_gas_price_is_converted_from_hex_wei_to_gwei(self):
        rpc = EthereumRpc("https://example.invalid")
        with patch.object(rpc, "call", side_effect=["0x1", hex(15_000_000)]):
            self.assertEqual(rpc.gas_price_gwei(), Decimal("0.015"))

    def test_rejects_wrong_asset_missing_fees_and_mismatched_total(self):
        for mutate in (
            lambda q: q["fees"].update(asset="ETH.USDP-SPOOF"),
            lambda q: q["fees"].pop("outbound"),
            lambda q: q["fees"].update(total="1"),
            lambda q: q["fees"].update(liquidity=1.5),
        ):
            q = fixture_quote()
            mutate(q)
            with self.assertRaises(QuoteError):
                summarize_quote(q, self.params, self.args, Decimal("2400"))

    def test_rejects_expiry_and_unapplied_affiliate(self):
        q = fixture_quote()
        q["expiry"] = int(time.time()) - 1
        with self.assertRaisesRegex(QuoteError, "expired"):
            summarize_quote(q, self.params, self.args, None)
        q = fixture_quote()
        q["fees"]["affiliate"] = "0"
        q["fees"]["total"] = "2200000000"
        with self.assertRaisesRegex(QuoteError, "affiliate was not charged"):
            summarize_quote(q, self.params, self.args, None)

    def test_request_preserves_affiliate_streaming_and_liquidity_tolerance(self):
        p = quote_params(self.args, "symbiosis", 164)
        self.assertEqual(p["amount"], 1_000_000_000_000)
        self.assertEqual(p["streaming_quantity"], 164)
        self.assertEqual(p["liquidity_tolerance_bps"], 20)
        self.assertEqual(p["affiliate_bps"], 1)
        self.assertNotIn("destination", p)
        self.assertNotIn("affiliate", quote_params(self.args, None, 0))

    def test_implied_eth_usdc_rate_and_halt_detection(self):
        pools = fixture_pools()
        self.assertEqual(eth_usdc_from_pools(pools), Decimal("2400"))
        validate_pools(pools, {})
        with self.assertRaisesRegex(QuoteError, "HALTETHTRADING"):
            validate_pools(pools, {"HALTETHTRADING": 1})
        pools[-1]["trading_halted"] = True
        with self.assertRaisesRegex(QuoteError, "halted"):
            validate_pools(pools, {})

    def test_best_respects_eta_and_expiry_even_when_expired_route_is_cheaper(self):
        rows = [
            {"profile": "slow", "expiry_unix": 200, "estimated_seconds": 3300,
             "slippage_scenario_break_even_exit_usdc_per_usdp": "0.999"},
            {"profile": "fast", "expiry_unix": 200, "estimated_seconds": 354,
             "slippage_scenario_break_even_exit_usdc_per_usdp": "1.001"},
            {"profile": "expired", "expiry_unix": 99, "estimated_seconds": 10,
             "slippage_scenario_break_even_exit_usdc_per_usdp": "0.9"},
        ]
        self.assertEqual(choose_best(rows, None, now=100)["profile"], "slow")
        self.assertEqual(choose_best(rows, 600, now=100)["profile"], "fast")
        self.assertIsNone(choose_best(rows, 100, now=100))

    def test_missing_memo_is_not_invented_and_scientific_memo_target_is_supported(self):
        q = fixture_quote()
        q.pop("memo")
        row = summarize_quote(q, self.params, self.args, None)
        self.assertIsNone(row["server_memo_trade_target_usdp"])
        self.assertEqual(memo_target("=:ETH.USDP:address:123e8/0/1"), 123 * SCALE)

    def test_zero_slippage_would_disable_protocol_limit_and_is_rejected_before_network(self):
        with patch("scripts.quote_thorchain_usdp.ThorClient") as client:
            with patch("sys.stderr"):
                self.assertEqual(main(["--slippage-bps", "0"]), 2)
            client.assert_not_called()

    def test_quote_failure_does_not_poison_other_routes(self):
        class FakeClient:
            base_url = "https://example.invalid/thorchain"

            def get(inner, path, params=None):
                if path == "mimir":
                    return {"L1DYNAMICFEEENABLED": 1, "DYNAMICFEE-WHITELIST-SYMBIOSIS": 1}
                if path == "pools":
                    return fixture_pools()
                if params.get("affiliate"):
                    raise QuoteError("provider temporarily unavailable")
                q = copy.deepcopy(fixture_quote())
                q["fees"]["affiliate"] = "0"
                q["fees"]["total"] = "2200000000"
                return q

        args = parser().parse_args(["--gas-cost-usdc", "5", "--slippage-bps", "20", "--all-routes"])
        with patch("sys.stderr"):
            report = collect(args, FakeClient())
        self.assertEqual(report["best_profile"], "direct/auto")
        self.assertEqual(len(report["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
