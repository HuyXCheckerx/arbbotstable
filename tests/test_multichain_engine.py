import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from src.engines.multichain_engine import (
    CHAINS,
    MultichainEngine,
    DirectDEXClient,
    StableDefiClient,
    DEXQuote,
    StableQuote,
    to_raw,
    from_raw,
    ArbError,
)


class MultichainEngineUnitTests(unittest.TestCase):
    def test_chain_configurations(self):
        polygon = CHAINS["polygon"]
        bsc = CHAINS["bsc"]
        self.assertEqual(polygon.chain_id, 137)
        self.assertEqual(polygon.decimals, 6)
        self.assertEqual(polygon.provider_kind, 0)
        self.assertEqual(bsc.chain_id, 56)
        self.assertEqual(bsc.decimals, 18)
        self.assertEqual(bsc.provider_kind, 1)

    def test_token_amount_conversions(self):
        # 6 decimals (Polygon)
        self.assertEqual(to_raw("1000", 6), 1000_000_000)
        self.assertEqual(from_raw(1000_000_000, 6), "1000")
        # Fractional rounding down (10 decimals from Stable.com)
        self.assertEqual(to_raw("50002.0691435408", 6), 50002_069143)

        # 18 decimals (BSC)
        self.assertEqual(to_raw("1000", 18), 1000 * 10**18)
        self.assertEqual(from_raw(1000 * 10**18, 18), "1000")

    def test_opportunity_profit_with_and_without_flash_fee(self):
        mock_dex = Mock(spec=DirectDEXClient)
        mock_stable = Mock(spec=StableDefiClient)
        engine = MultichainEngine(dex_client=mock_dex, stable_client=mock_stable)

        # 1. Polygon: Morpho 0-fee flash loan
        poly_config = CHAINS["polygon"]
        loan_amount = 50000
        dex_buy_amount = 50060 * 10**6
        stable_out_amount = 50010 * 10**6

        mock_dex.get_best_quote.return_value = DEXQuote(
            aggregator="ParaSwap (Velora)",
            target="0x" + "1" * 40,
            allowance_target="0x" + "2" * 40,
            data="0x1234",
            value=0,
            sell_amount=50000 * 10**6,
            buy_amount=dex_buy_amount,
        )
        mock_stable.quote.return_value = StableQuote(
            amount_in=dex_buy_amount,
            amount_out=stable_out_amount,
            token_fee=50 * 10**6,
            capacity=Decimal("70000"),
            minimum=Decimal("1000"),
            maximum=Decimal("1000000"),
        )

        poly_op = engine.scan_chain_opportunity(poly_config, loan_amount, counter_sym="USDT")
        self.assertIsNotNone(poly_op)
        self.assertEqual(poly_op["flash_fee"], 0.0)
        self.assertEqual(poly_op["gross_profit"], 10.0)

        # 2. BSC: Aave V3 5-bps flash loan
        bsc_config = CHAINS["bsc"]
        bsc_loan = 50000
        bsc_dex_buy = 50060 * 10**18
        bsc_stable_out = 50010 * 10**18

        mock_dex.get_best_quote.return_value = DEXQuote(
            aggregator="ParaSwap (Velora)",
            target="0x" + "1" * 40,
            allowance_target="0x" + "2" * 40,
            data="0x1234",
            value=0,
            sell_amount=50000 * 10**18,
            buy_amount=bsc_dex_buy,
        )
        mock_stable.quote.return_value = StableQuote(
            amount_in=bsc_dex_buy,
            amount_out=bsc_stable_out,
            token_fee=50 * 10**18,
            capacity=Decimal("70000"),
            minimum=Decimal("1000"),
            maximum=Decimal("1000000"),
        )

        bsc_op = engine.scan_chain_opportunity(bsc_config, bsc_loan, counter_sym="USDT")
        self.assertIsNotNone(bsc_op)
        # 5 bps on 50,000 = 25.0 USDC
        self.assertEqual(bsc_op["flash_fee"], 25.0)
        self.assertEqual(bsc_op["gross_profit"], -15.0)

    def test_live_execution_guard_requires_executor(self):
        engine = MultichainEngine()
        poly_config = CHAINS["polygon"]
        with patch.object(engine, "get_executor_address", return_value=""):
            with self.assertRaises(ArbError) as ctx:
                engine.execute_live(poly_config, {}, confirm_key="EXECUTE_PROFIT_SNIPER")
            self.assertIn("No deployed executor", str(ctx.exception))

    def test_live_execution_guard_requires_valid_confirm_token(self):
        engine = MultichainEngine()
        poly_config = CHAINS["polygon"]
        with patch.object(engine, "get_executor_address", return_value="0x" + "1" * 40):
            with patch.object(engine, "get_operator", return_value=("0x" + "2" * 40, "0x" + "3" * 64)):
                with self.assertRaises(ArbError) as ctx:
                    engine.execute_live(poly_config, {}, confirm_key="WRONG_KEY")
                self.assertIn("Invalid live execution confirmation key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
