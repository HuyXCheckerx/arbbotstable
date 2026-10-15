import os
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


FAKE_API_CREDENTIALS = {
    "OKX_API_KEY": "test-okx-key",
    "OKX_SECRET_KEY": "test-okx-secret",
    "OKX_PASSPHRASE": "test-okx-passphrase",
    "OKX_PROJECT_ID": "test-okx-project",
    "ENSO_API_KEY": "test-enso-key",
}


class USDPTestEngineTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, FAKE_API_CREDENTIALS):
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

    def test_execute_live_arbitrage_flashbots_success(self):
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
        mock_w3.eth.account.from_key.return_value = MagicMock(address="0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C", sign_transaction=lambda t: MagicMock(raw_transaction=b"raw", hash=b"\xaa\xbb"))

        mock_receipt = MagicMock(
            status=1,
            gasUsed=250_000,
            effectiveGasPrice=100_000_000,
            blockNumber=20000000,
        )
        mock_w3.eth.wait_for_transaction_receipt.return_value = mock_receipt

        with patch.dict(os.environ, {"ETH_OPERATOR_PRIVATE_KEY": "0x" + "11" * 32, "ETH_ENABLE_FLASHBOTS": "true"}), \
             patch.object(self.engine, "simulate_live_arbitrage", return_value=mock_sim), \
             patch.object(self.engine, "get_working_web3", return_value=mock_w3), \
             patch("src.engines.eth_flash_arb_pyusd_usdc.broadcast_flashbots_or_fallback", return_value=(b"\xaa\xbb", "flashbots-protect")), \
             patch.object(self.engine, "get_eth_usd", return_value=Decimal("2500")):
            res = self.engine.execute_live_arbitrage(eval_res, dry_run=False)
            self.assertTrue(res.success)
            self.assertEqual(res.mode, "live")
            self.assertEqual(res.broadcast_method, "flashbots-protect")
            self.assertEqual(res.tx_hash, "0xaabb")
            self.assertEqual(res.gas_used, 250_000)

    def test_execute_live_arbitrage_flashbots_timeout_dropped(self):
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
        mock_w3.eth.account.from_key.return_value = MagicMock(address="0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C", sign_transaction=lambda t: MagicMock(raw_transaction=b"raw", hash=b"\xcc\xdd"))
        mock_w3.eth.wait_for_transaction_receipt.side_effect = TimeoutError("Transaction timed out")

        with patch.dict(os.environ, {"ETH_OPERATOR_PRIVATE_KEY": "0x" + "11" * 32, "ETH_ENABLE_FLASHBOTS": "true"}), \
             patch.object(self.engine, "simulate_live_arbitrage", return_value=mock_sim), \
             patch.object(self.engine, "get_working_web3", return_value=mock_w3), \
             patch("src.engines.eth_flash_arb_pyusd_usdc.broadcast_flashbots_or_fallback", return_value=(b"\xcc\xdd", "flashbots-relay")):
            res = self.engine.execute_live_arbitrage(eval_res, dry_run=False)
            self.assertFalse(res.success)
            self.assertEqual(res.mode, "live")
            self.assertEqual(res.gas_used, 0)
            self.assertEqual(res.actual_gas_cost_usd, Decimal("0"))
            self.assertIn("Transaction not mined within", str(res.error))

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

    def test_quote_dex_enso_success(self):
        mock_enso_resp = {
            "gas": "310000",
            "amountOut": "10029500000000000000000",
            "tx": {
                "to": "0xEnsoRouter",
                "data": "0xenso1234",
                "value": "0",
            },
            "preTransactions": [
                {
                    "type": "tokenApproval",
                    "spender": "0xEnsoSpender",
                }
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_enso_resp

        with patch.object(self.engine.session, "post", return_value=mock_resp):
            quote = self.engine.quote_dex_enso(Decimal("10000.0"))
            self.assertEqual(quote.aggregator, "Enso")
            self.assertEqual(quote.target, "0xEnsoRouter")
            self.assertEqual(quote.allowance_target, "0xEnsoSpender")
            self.assertEqual(quote.buy_amount_human, Decimal("10029.5"))
            self.assertEqual(quote.gas_estimate, 310_000)

    def test_api_credentials_have_no_builtin_defaults(self):
        with patch.dict(os.environ, {}):
            for name in FAKE_API_CREDENTIALS:
                os.environ.pop(name, None)
            engine = USDPTestEngine()
        self.assertEqual(engine.okx_key, "")
        self.assertEqual(engine.okx_secret, "")
        self.assertEqual(engine.okx_passphrase, "")
        self.assertEqual(engine.okx_project_id, "")
        self.assertEqual(engine.enso_key, "")

    def test_quote_dex_okx_fails_cleanly_without_credentials(self):
        env = dict(FAKE_API_CREDENTIALS, OKX_SECRET_KEY="", OKX_PROJECT_ID="")
        with patch.dict(os.environ, env):
            engine = USDPTestEngine()
        with patch.object(engine.session, "get") as mock_get:
            with self.assertRaises(RuntimeError) as ctx:
                engine.quote_dex_okx(Decimal("10000.0"))
        mock_get.assert_not_called()
        message = str(ctx.exception)
        self.assertEqual(message, "OKX quote unavailable: set OKX_SECRET_KEY, OKX_PROJECT_ID in .env")
        self.assertNotIn(FAKE_API_CREDENTIALS["OKX_API_KEY"], message)
        self.assertNotIn(FAKE_API_CREDENTIALS["OKX_PASSPHRASE"], message)

    def test_quote_dex_enso_fails_cleanly_without_api_key(self):
        with patch.dict(os.environ, dict(FAKE_API_CREDENTIALS, ENSO_API_KEY="")):
            engine = USDPTestEngine()
        with patch.object(engine.session, "post") as mock_post:
            with self.assertRaisesRegex(RuntimeError, "set ENSO_API_KEY in .env"):
                engine.quote_dex_enso(Decimal("10000.0"))
        mock_post.assert_not_called()

    def test_quote_dex_matcha_fallback_prefers_best(self):
        mock_enso = DexQuote(
            aggregator="Enso",
            target="0xEnsoRouter",
            allowance_target="0xEnsoSpender",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_030_000_000_000_000_000_000,
            buy_amount_human=Decimal("10030.0"),
            data="0x11",
            value=0,
            gas_estimate=300_000,
        )
        mock_okx = DexQuote(
            aggregator="OKX",
            target="0xOkxRouter",
            allowance_target="0xOkxSpender",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_020_000_000_000_000_000_000,
            buy_amount_human=Decimal("10020.0"),
            data="0x22",
            value=0,
            gas_estimate=250_000,
        )
        with patch.object(self.engine, "quote_dex_enso", return_value=mock_enso), \
             patch.object(self.engine, "quote_dex_okx", return_value=mock_okx), \
             patch("src.engines.matcha_browser_bridge.fetch_bridge_quotes", side_effect=RuntimeError("no bridge")), \
             patch.object(self.engine.session, "post", side_effect=RuntimeError("WAF challenge")):
            quote = self.engine.quote_dex_matcha(Decimal("10000.0"))
            # Enso yields 10030 > OKX 10020, so Enso should be selected as fallback winner
            self.assertEqual(quote.aggregator, "meta.matcha (Enso)")
            self.assertEqual(quote.buy_amount_human, Decimal("10030.0"))
            self.assertEqual(quote.target, "0xEnsoRouter")

    def test_default_executor_is_usdp_whitelisted(self):
        from src.engines.usdp_arb_engine import DEFAULT_EXECUTOR
        self.assertEqual(DEFAULT_EXECUTOR, "0x20f5Cc012bBb5323EfBFcC91A7d0C960175D3b2c")

    def test_insufficient_matcha_output_decoding(self):
        # 0x3e0aa470 followed by 10029.464152 (received) and 10029.474843 (required)
        received_hex = hex(10029464152885333216060)[2:].zfill(64)
        required_hex = hex(10029474843337594652115)[2:].zfill(64)
        err_str = f"eth_call reverted: ('0x3e0aa470{received_hex}{required_hex}',)"

        dex_quote = DexQuote(
            aggregator="meta.matcha",
            target="0xTarget",
            allowance_target="0xSpender",
            sell_amount_raw=10_000_000_000,
            buy_amount_raw=10_029_474_843_337_594_652_115,
            buy_amount_human=Decimal("10029.474843"),
            data="0x1234",
            value=0,
            gas_estimate=300_000,
        )
        eval_res = ArbEvaluation(
            loan_amount_usdc=Decimal("10000.0"),
            dex_quote=dex_quote,
            stable_quote=StableQuote(
                amount_in_human=Decimal("10029.474843"),
                amount_out_human=Decimal("10029.474843"),
                pool_balance_human=Decimal("200000.0"),
                min_order_human=Decimal("1000.0"),
                token_fee_human=Decimal("0.0"),
                native_fee_usd=Decimal("0.0"),
                raw_response={},
            ),
            gross_profit_usdc=Decimal("29.474843"),
            gas_estimate_usd=Decimal("0.20"),
            net_profit_usd=Decimal("29.274843"),
            is_profitable=True,
        )

        mock_w3 = MagicMock()
        mock_w3.eth.get_code.return_value = b"\x60\x80"
        mock_contract = MagicMock()
        mock_contract.functions.owner().call.return_value = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
        mock_contract.functions.supportsLoanToken().call.return_value = True
        mock_contract.functions.executeArbitrageWithTokens().call.side_effect = RuntimeError(err_str)
        mock_w3.eth.contract.return_value = mock_contract
        mock_w3.to_checksum_address.side_effect = lambda a: a

        with patch.object(self.engine, "get_working_web3", return_value=mock_w3), \
             patch.object(self.engine, "create_stable_order", return_value={
                 "nonce": 1,
                 "deadline": 1800000000,
                 "maintainerSignature": "0x" + "aa" * 65,
                 "executionFeeNative": 0,
             }):
            sim = self.engine.simulate_live_arbitrage(eval_res)
            self.assertFalse(sim.success)
            self.assertEqual(sim.error_selector, "0x3e0aa470")
            self.assertIn("InsufficientMatchaOutput", sim.error_message)
            self.assertIn("DEX swap delivered 10029.464153 USDP", sim.error_message)
            self.assertIn("short of Stable.com order requirement", sim.error_message)
            self.assertIn("--leg1-buffer-bps", sim.remedy)


if __name__ == "__main__":
    unittest.main()


