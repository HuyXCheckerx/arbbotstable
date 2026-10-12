import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.engines.usdp_arb_engine import (
    USDPTestEngine,
    DexQuote,
    StableQuote,
    ArbEvaluation,
    SimulationResult,
    ExecutionResult,
    format_evaluation,
    format_scaling_table,
    format_simulation_report,
    format_execution_report,
    USDC_DECIMALS,
    USDP_DECIMALS,
    main,
)


class USDPTestEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = USDPTestEngine()

    def test_decimal_conversion(self):
        raw_usdc = 10_000_000_000
        human_usdc = Decimal(raw_usdc) / Decimal(10**USDC_DECIMALS)
        self.assertEqual(human_usdc, Decimal("10000.0"))

        raw_usdp = 10_024_500_000_000_000_000_000
        human_usdp = Decimal(raw_usdp) / Decimal(10**USDP_DECIMALS)
        self.assertEqual(human_usdp, Decimal("10024.5"))

    def test_evaluate_arbitrage_profitable(self):
        dex_quote = DexQuote(
            aggregator="OKX",
            target="0x8feAB81D36E7576107D5dE0758c1b839Be31B4F6",
            allowance_target="0x40aA958dd87FC8305b97f2BA922CDdCa374bcD7f",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_025_000_000_000_000_000_000,
            buy_amount_human=Decimal("10025.0"),
            data="0x1234",
            value=0,
            gas_estimate=250_000,
        )
        stable_quote = StableQuote(
            amount_in_human=Decimal("10025.0"),
            amount_out_human=Decimal("10025.0"),
            pool_balance_human=Decimal("200000.0"),
            min_order_human=Decimal("1000.0"),
            token_fee_human=Decimal("0.0"),
            native_fee_usd=Decimal("0.15"),
            raw_response={},
        )

        with patch.object(self.engine, "quote_dex", return_value=dex_quote), \
             patch.object(self.engine, "quote_stable", return_value=stable_quote), \
             patch.object(self.engine, "get_gas_price_gwei", return_value=Decimal("1.0")), \
             patch.object(self.engine, "get_eth_usd", return_value=Decimal("2500.0")):
            
            evaluation = self.engine.evaluate_arbitrage(Decimal("10000.0"))
            
            self.assertEqual(evaluation.gross_profit_usdc, Decimal("25.0"))
            self.assertTrue(evaluation.is_profitable)
            self.assertGreater(evaluation.net_profit_usd, Decimal("23.0"))

    def test_format_evaluation(self):
        dex_quote = DexQuote(
            aggregator="OKX",
            target="0xTarget",
            allowance_target="0xSpender",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_020_000_000_000_000_000_000,
            buy_amount_human=Decimal("10020.0"),
            data="0x",
            value=0,
            gas_estimate=200_000,
        )
        stable_quote = StableQuote(
            amount_in_human=Decimal("10020.0"),
            amount_out_human=Decimal("10020.0"),
            pool_balance_human=Decimal("200000.0"),
            min_order_human=Decimal("1000.0"),
            token_fee_human=Decimal("0.0"),
            native_fee_usd=Decimal("0.10"),
            raw_response={},
        )
        evaluation = ArbEvaluation(
            loan_amount_usdc=Decimal("10000.0"),
            dex_quote=dex_quote,
            stable_quote=stable_quote,
            gross_profit_usdc=Decimal("20.0"),
            gas_estimate_usd=Decimal("0.80"),
            net_profit_usd=Decimal("19.10"),
            is_profitable=True,
        )
        formatted = format_evaluation(evaluation, Decimal("1.0"), Decimal("2500.0"))
        self.assertIn("USDP ARBITRAGE TEST ENGINE", formatted)
        self.assertIn("OKX", formatted)
        self.assertIn("10,020.000000 USDP", formatted)
        self.assertIn("+20.000000 USDC", formatted)
        self.assertIn("PROFITABLE", formatted)

    def test_scale_scan(self):
        def fake_eval(amount_usdc, **kwargs):
            dq = DexQuote(
                aggregator="OKX",
                target="0xTarget",
                allowance_target="0xSpender",
                sell_amount_raw=int(amount_usdc * 10**6),
                buy_amount_raw=int((amount_usdc + Decimal("20.0")) * 10**18),
                buy_amount_human=amount_usdc + Decimal("20.0"),
                data="0x",
                value=0,
                gas_estimate=200_000,
            )
            sq = StableQuote(
                amount_in_human=amount_usdc + Decimal("20.0"),
                amount_out_human=amount_usdc + Decimal("20.0"),
                pool_balance_human=Decimal("200000.0"),
                min_order_human=Decimal("1000.0"),
                token_fee_human=Decimal("0.0"),
                native_fee_usd=Decimal("0.10"),
                raw_response={},
            )
            # Make 30,000 yield the most profit
            profit = Decimal("40.0") if amount_usdc == Decimal("30000.0") else Decimal("15.0")
            return ArbEvaluation(
                loan_amount_usdc=amount_usdc,
                dex_quote=dq,
                stable_quote=sq,
                gross_profit_usdc=profit,
                gas_estimate_usd=Decimal("0.50"),
                net_profit_usd=profit - Decimal("0.60"),
                is_profitable=True,
            )

        with patch.object(self.engine, "evaluate_arbitrage", side_effect=fake_eval):
            results = self.engine.scale_scan(
                min_amount=Decimal("10000.0"),
                max_amount=Decimal("30000.0"),
                step=Decimal("10000.0"),
                delay=0.0,
            )
            self.assertEqual(len(results), 3)
            winner = max(results, key=lambda e: e.net_profit_usd)
            self.assertEqual(winner.loan_amount_usdc, Decimal("30000.0"))
            self.assertGreater(winner.net_profit_usd, Decimal("30.0"))

    def test_format_scaling_table(self):
        dq = DexQuote(
            aggregator="OKX",
            target="0xTarget",
            allowance_target="0xSpender",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_018_000_000_000_000_000_000,
            buy_amount_human=Decimal("10018.0"),
            data="0x",
            value=0,
            gas_estimate=200_000,
        )
        sq = StableQuote(
            amount_in_human=Decimal("10018.0"),
            amount_out_human=Decimal("10018.0"),
            pool_balance_human=Decimal("200000.0"),
            min_order_human=Decimal("1000.0"),
            token_fee_human=Decimal("0.0"),
            native_fee_usd=Decimal("0.10"),
            raw_response={},
        )
        ev1 = ArbEvaluation(
            loan_amount_usdc=Decimal("10000.0"),
            dex_quote=dq,
            stable_quote=sq,
            gross_profit_usdc=Decimal("18.0"),
            gas_estimate_usd=Decimal("0.50"),
            net_profit_usd=Decimal("17.40"),
            is_profitable=True,
        )
        ev2 = ArbEvaluation(
            loan_amount_usdc=Decimal("20000.0"),
            dex_quote=dq,
            stable_quote=sq,
            gross_profit_usdc=Decimal("35.0"),
            gas_estimate_usd=Decimal("0.50"),
            net_profit_usd=Decimal("34.40"),
            is_profitable=True,
        )
        table = format_scaling_table([ev1, ev2], ev2, Decimal("1.0"), Decimal("2500.0"))
        self.assertIn("USDP ARBITRAGE YIELD CURVE SCAN", table)
        self.assertIn("10,000 USDC", table)
        self.assertIn("20,000 USDC", table)
        self.assertIn("★ HIGHEST YIELD ★", table)
        self.assertIn("WINNING ROUTE DETAILS", table)

    def test_quote_dex_matcha_direct(self):
        mock_bridge_response = {
            "0x": {
                "allowanceHolder": {
                    "allowanceTarget": "0xMatchaSpender",
                    "quote": {
                        "to": "0xMatchaRouter",
                        "buyAmount": "10022000000000000000000",  # 10,022 USDP (18 dec)
                        "gas": "225000",
                        "value": "0",
                        "data": "0xabcdef1234",
                    },
                }
            }
        }
        with patch("src.engines.matcha_browser_bridge.fetch_bridge_quotes", return_value=mock_bridge_response), \
             patch.object(self.engine, "get_gas_price_gwei", return_value=Decimal("1.0")):
            quote = self.engine.quote_dex_matcha(Decimal("10000.0"))
            self.assertEqual(quote.aggregator, "meta.matcha (0x)")
            self.assertEqual(quote.target, "0xMatchaRouter")
            self.assertEqual(quote.allowance_target, "0xMatchaSpender")
            self.assertEqual(quote.buy_amount_human, Decimal("10022.0"))
            self.assertEqual(quote.gas_estimate, 225_000)

    def test_quote_dex_matcha_fallback_okx(self):
        mock_okx = DexQuote(
            aggregator="OKX",
            target="0xOkxRouter",
            allowance_target="0xOkxSpender",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_020_000_000_000_000_000_000,
            buy_amount_human=Decimal("10020.0"),
            data="0x1122",
            value=0,
            gas_estimate=240_000,
        )
        with patch.object(self.engine, "quote_dex_okx", return_value=mock_okx), \
             patch("src.engines.matcha_browser_bridge.fetch_bridge_quotes", side_effect=RuntimeError("no bridge")), \
             patch.object(self.engine.session, "post", side_effect=RuntimeError("WAF challenge")):
            quote = self.engine.quote_dex_matcha(Decimal("10000.0"))
            self.assertEqual(quote.aggregator, "meta.matcha (OKX)")
            self.assertEqual(quote.buy_amount_human, Decimal("10020.0"))
            self.assertEqual(quote.target, "0xOkxRouter")

    def test_quote_dex_dispatch_default(self):
        mock_matcha = DexQuote(
            aggregator="meta.matcha (0x)",
            target="0xMatchaRouter",
            allowance_target="0xMatchaSpender",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_025_000_000_000_000_000_000,
            buy_amount_human=Decimal("10025.0"),
            data="0x99",
            value=0,
            gas_estimate=210_000,
        )
        with patch.object(self.engine, "quote_dex_matcha", return_value=mock_matcha) as mock_fn:
            quote = self.engine.quote_dex(Decimal("10000.0"))
            mock_fn.assert_called_once()
            self.assertEqual(quote.aggregator, "meta.matcha (0x)")

    def _sample_eval(self) -> ArbEvaluation:
        dq = DexQuote(
            aggregator="meta.matcha (OKX)",
            target="0xTarget",
            allowance_target="0xSpender",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_025_000_000_000_000_000_000,
            buy_amount_human=Decimal("10025.0"),
            data="0x1234",
            value=0,
            gas_estimate=250_000,
        )
        sq = StableQuote(
            amount_in_human=Decimal("10025.0"),
            amount_out_human=Decimal("10025.0"),
            pool_balance_human=Decimal("200000.0"),
            min_order_human=Decimal("1000.0"),
            token_fee_human=Decimal("0.0"),
            native_fee_usd=Decimal("0.10"),
            raw_response={},
        )
        return ArbEvaluation(
            loan_amount_usdc=Decimal("10000.0"),
            dex_quote=dq,
            stable_quote=sq,
            gross_profit_usdc=Decimal("25.0"),
            gas_estimate_usd=Decimal("0.50"),
            net_profit_usd=Decimal("24.40"),
            is_profitable=True,
        )

    def test_simulate_live_arbitrage_success(self):
        eval_res = self._sample_eval()
        order_data = {
            "nonce": 42,
            "deadline": 1790000000,
            "maintainerSignature": "0x1234567890abcdef",
            "executionFeeNative": 1000,
        }

        mock_w3 = MagicMock()
        mock_w3.to_checksum_address.side_effect = lambda a: str(a)
        mock_w3.eth.get_code.return_value = b"\x60\x80\x60\x40"

        mock_contract = MagicMock()
        mock_contract.functions.owner().call.return_value = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
        mock_contract.functions.supportsLoanToken().call.return_value = True

        mock_fn = MagicMock()
        mock_fn.call.return_value = None
        mock_fn.estimate_gas.return_value = 320_000
        mock_contract.functions.executeArbitrageWithTokens.return_value = mock_fn
        mock_w3.eth.contract.return_value = mock_contract

        with patch.object(self.engine, "get_working_web3", return_value=mock_w3):
            sim = self.engine.simulate_live_arbitrage(eval_res, order_data=order_data)
            self.assertTrue(sim.success)
            self.assertEqual(sim.estimated_gas, 320_000)
            self.assertEqual(sim.order_nonce, 42)
            self.assertEqual(sim.native_value_wei, 1000)

    def test_simulate_live_arbitrage_unsupported_token(self):
        eval_res = self._sample_eval()
        mock_w3 = MagicMock()
        mock_w3.to_checksum_address.side_effect = lambda a: str(a)
        mock_w3.eth.get_code.return_value = b"\x60\x80\x60\x40"

        mock_contract = MagicMock()
        mock_contract.functions.owner().call.return_value = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
        mock_contract.functions.supportsLoanToken().call.return_value = False
        mock_w3.eth.contract.return_value = mock_contract

        with patch.object(self.engine, "get_working_web3", return_value=mock_w3):
            sim = self.engine.simulate_live_arbitrage(eval_res, order_data={"maintainerSignature": "0x", "deadline": 0, "nonce": 1})
            self.assertFalse(sim.success)
            self.assertEqual(sim.error_selector, "0xfcf3a30b")
            self.assertIn("InvalidLoanToken", sim.error_message)

    def test_execute_live_arbitrage_dry_run(self):
        eval_res = self._sample_eval()
        mock_sim = SimulationResult(
            success=True,
            estimated_gas=310_000,
            native_value_wei=500,
            min_profit_raw=1_000_000,
            order_nonce=10,
            order_deadline=1790000000,
            maintainer_signature="0xabcdef",
        )

        mock_w3 = MagicMock()
        mock_w3.to_checksum_address.side_effect = lambda a: str(a)
        mock_w3.eth.get_block.return_value = {"baseFeePerGas": 100_000_000}
        mock_w3.to_wei.return_value = 50_000_000
        mock_w3.from_wei.return_value = Decimal("0.15")
        mock_w3.eth.get_balance.return_value = 10**18
        mock_w3.eth.get_transaction_count.return_value = 5

        with patch.object(self.engine, "simulate_live_arbitrage", return_value=mock_sim), \
             patch.object(self.engine, "get_working_web3", return_value=mock_w3):
            res = self.engine.execute_live_arbitrage(eval_res, dry_run=True)
            self.assertTrue(res.success)
            self.assertEqual(res.mode, "dry-run")
            self.assertEqual(res.gas_used, 310_000)

    def test_format_reports(self):
        sim_ok = SimulationResult(
            success=True,
            estimated_gas=280_000,
            native_value_wei=1000,
            min_profit_raw=1_000_000,
            order_nonce=15,
            order_deadline=1790000000,
            maintainer_signature="0x123",
        )
        sim_rep = format_simulation_report(sim_ok, "0xExecutor", "0xOperator")
        self.assertIn("PASSED", sim_rep)
        self.assertIn("280,000", sim_rep)

        exec_res = ExecutionResult(
            mode="dry-run",
            success=True,
            simulation=sim_ok,
        )
        exec_rep = format_execution_report(exec_res)
        self.assertIn("DRY-RUN", exec_rep)
        self.assertIn("PASSED", exec_rep)


if __name__ == "__main__":
    unittest.main()


