import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.engines.usdp_arb_engine import (
    USDPTestEngine,
    DexQuote,
    StableQuote,
    ArbEvaluation,
    format_evaluation,
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


if __name__ == "__main__":
    unittest.main()
