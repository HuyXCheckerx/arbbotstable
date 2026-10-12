import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.engines.usdp_arb_engine import (
    USDPTestEngine,
    DexQuote,
    StableQuote,
    ArbEvaluation,
    format_evaluation,
    format_scaling_table,
    USDC_DECIMALS,
    USDP_DECIMALS,
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


if __name__ == "__main__":
    unittest.main()


