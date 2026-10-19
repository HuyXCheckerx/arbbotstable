#!/usr/bin/env python3
"""Unit tests for THORChain USDC -> USDP Sniper Engine."""

from decimal import Decimal
import unittest

from src.engines.thorchain_usdp_sniper import (
    PoolSpot,
    SniperError,
    build_protected_memo,
    deposit_candidates,
    memo_stream_fields,
    missing_affiliate_thor_aliases,
    parse_quote,
    plan_for_quote,
    quote_fee_tier_bps,
    simulate_streaming_fill,
    slice_fill_rate,
    THORCHAIN_SCALE,
)

# Pools at height 28304659, the block before swap 0xce981330... started streaming.
CE98_POOLS = PoolSpot(
    Decimal(0), Decimal(0), Decimal(0),
    usdc_rune=357643869723855, usdc_asset=233607478302786,
    usdp_rune=9348532880101, usdp_asset=6120645122973,
)


class FakeThorClient:
    def __init__(self, thornames):
        self.thornames = thornames

    def get(self, path, params=None):
        name = path.split("/", 1)[1]
        if name not in self.thornames:
            raise SniperError("THORNode HTTP 404: not found")
        return self.thornames[name]


class TestThorchainUsdpSniper(unittest.TestCase):
    def test_build_protected_memo_elevates_limit(self):
        # Raw memo with 0 limit
        raw_memo = "=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:0/0/1638:symbiosis:1"
        min_target = Decimal("10010")  # 10,010 * 1e8 = 1,001,000,000,000
        protected = build_protected_memo(raw_memo, min_target, floor_mode="target")
        expected = "=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:1001000000000/0/1638:symbiosis:1"
        self.assertEqual(protected, expected)

    def test_build_protected_memo_replaces_lower_limit(self):
        # Raw memo with 997269282187 limit (< 1001000000000)
        raw_memo = "=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:997269282187/0/1638:symbiosis:1"
        min_target = Decimal("10010")
        protected = build_protected_memo(raw_memo, min_target, floor_mode="target")
        expected = "=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:1001000000000/0/1638:symbiosis:1"
        self.assertEqual(protected, expected)

    def test_build_protected_memo_preserves_higher_limit(self):
        # Raw memo with 1002000000000 limit (> 1001000000000)
        raw_memo = "=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:1002000000000/0/1638:symbiosis:1"
        min_target = Decimal("10010")
        protected = build_protected_memo(raw_memo, min_target, floor_mode="target")
        expected = "=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:1002000000000/0/1638:symbiosis:1"
        self.assertEqual(protected, expected)

    def test_parse_quote_success(self):
        mock_response = {
            "expected_amount_out": "1001500000000",
            "fees": {
                "asset": "ETH.USDP-0X8E870D67F660D95D5BE530380D0EC0BD388289E1",
                "liquidity": "500000000",
                "affiliate": "10000000",
                "outbound": "200000000",
                "total": "710000000",
                "slippage_bps": 5,
            },
            "total_swap_seconds": 1800,
            "inbound_address": "0x4d39d5c55cfeb48f44d680fcac20493635dafdee",
            "router": "0xD37BbE5744D730a1d98d8DC97c42F0Ca46aD7146",
            "memo": "=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:1001000000000/0/1638:symbiosis:1",
            "expiry": 1792382000,
        }
        quote = parse_quote(mock_response, Decimal("10000"))
        self.assertEqual(quote.expected_usdp, Decimal("10015"))
        self.assertEqual(quote.expected_raw_1e8, 1001500000000)
        self.assertEqual(quote.slippage_bps, 5)
        self.assertEqual(quote.total_swap_seconds, 1800)
        self.assertEqual(quote.inbound_address, "0x4d39d5c55cfeb48f44d680fcac20493635dafdee")
        self.assertEqual(quote.router, "0xD37BbE5744D730a1d98d8DC97c42F0Ca46aD7146")

    def test_dynamic_min_usdp_from_amount(self):
        amount = Decimal("300")
        bps = Decimal("10")
        min_usdp = (amount * (Decimal(10000) + bps) / Decimal(10000)).quantize(Decimal("0.0001"))
        self.assertEqual(min_usdp, Decimal("300.3000"))

        amount_large = Decimal("50000")
        min_usdp_large = (amount_large * (Decimal(10000) + bps) / Decimal(10000)).quantize(Decimal("0.0001"))
        self.assertEqual(min_usdp_large, Decimal("50050.0000"))

    def test_affiliate_without_thor_alias_is_reported(self):
        # The state 'salactite' was in when swap 0x3dfaf115... was refunded.
        client = FakeThorClient({"salactite": {"aliases": [{"chain": "ETH", "address": "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"}]}})
        self.assertEqual(missing_affiliate_thor_aliases(client, "salactite"), ["salactite"])

    def test_affiliate_with_thor_alias_passes(self):
        client = FakeThorClient({"salactite": {"aliases": [{"chain": "THOR", "address": "thor1dl7ujlyywmkp8r7mfe5sxd42j0n97tlqzx96sl"}]}})
        self.assertEqual(missing_affiliate_thor_aliases(client, "salactite"), [])

    def test_affiliate_unknown_name_and_raw_thor_address(self):
        client = FakeThorClient({})
        self.assertEqual(missing_affiliate_thor_aliases(client, "nosuchname/thor1abc"), ["nosuchname"])

    def test_memo_stream_fields(self):
        memo = "=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:1000854156084/0/1637:symbiosis:1"
        self.assertEqual(memo_stream_fields(memo), (1000854156084, 0, 1637))
        self.assertEqual(memo_stream_fields("=:ETH.USDP:0xabc"), (0, 0, 0))

    def test_simulated_fill_matches_ce98_sub_swap_targets(self):
        # 0xce98: 10,000 USDC, 1,637 sub-swaps, limit 10,008.54 USDP, symbiosis 3 bps/leg tier,
        # 1 bp affiliate, 2.41 USDP outbound fee. THORNode reported its first misses against a
        # per-sub-swap target of 611394678 after ~4 fills; the model's target after 4 fills is 611394789.
        filled, emitted = simulate_streaming_fill(
            CE98_POOLS, 10**12, 1637, 1000854156084, min_slip_bps=3, affiliate_bps=1, outbound_fee=241474082
        )
        sub_swap = 10**12 // 1637
        self.assertEqual(filled, 3 * sub_swap)  # Own price impact stops it within a handful of sub-swaps.
        self.assertGreater(emitted, filled)

    def test_unlimited_swap_fills_everything(self):
        filled, _ = simulate_streaming_fill(CE98_POOLS, 10**12, 1637, 0, 3, 1, 241474082)
        self.assertEqual(filled, 10**12)

    def test_deposit_candidates_are_whole_usdc_within_bounds(self):
        sizes = deposit_candidates(Decimal("10000"), Decimal("6.1"))
        self.assertEqual(sizes[-1], Decimal("10000"))
        self.assertGreaterEqual(sizes[0], Decimal("1"))
        self.assertTrue(all(s == s.to_integral_value() for s in sizes))
        self.assertEqual(sizes, sorted(set(sizes)))

    def _quote(self, amount, quantity, liquidity, outbound="2.41474082", expected=None):
        scale = Decimal(THORCHAIN_SCALE)
        expected = expected if expected is not None else amount
        return parse_quote(
            {
                "expected_amount_out": str(int(Decimal(expected) * scale)),
                "fees": {
                    "liquidity": str(int(Decimal(liquidity) * scale)),
                    "affiliate": str(int(Decimal(amount) / 10000 * scale)),
                    "outbound": str(int(Decimal(outbound) * scale)),
                    "total": "0",
                },
                "memo": f"=:ETH.USDP:0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C:0/0/{quantity}:symbiosis:1",
            },
            Decimal(amount),
        )

    def test_quote_fee_tier_bps(self):
        self.assertEqual(quote_fee_tier_bps(self._quote("10000", 1652, "6.01", expected="10000.84")), 3)
        self.assertEqual(quote_fee_tier_bps(self._quote("10000", 165, "20.01", expected="9978.62")), 10)

    def test_plan_rejects_ce98_style_deposit(self):
        # At the ce98 pool state a +8.5 bps floor fills only a few sub-swaps, so the fixed
        # outbound fee makes every deposit size lose money.
        plan = plan_for_quote(
            CE98_POOLS,
            self._quote("10000", 1637, "6.01"),
            Decimal("10000"),
            target_rate=Decimal("1.0000854156084"),
            floor_mode="target",
            buffer_bps=2,
            affiliate_bps=1,
            usdc_refund_fee=Decimal("0.2496295"),
            gas_usd=Decimal("0.39"),
        )
        self.assertLess(plan.net_profit, 0)

    def test_plan_sizes_deposit_down_to_pool_capacity(self):
        # USDP 3% cheaper than USDC in the pool: a 10k deposit would push the price past
        # the floor long before filling, so the best deposit is smaller and profitable.
        pools = PoolSpot(
            Decimal(0), Decimal(0), Decimal(0),
            usdc_rune=357643869723855, usdc_asset=233607478302786,
            usdp_rune=9348532880101, usdp_asset=int(6120645122973 * Decimal("1.03")),
        )
        plan = plan_for_quote(
            pools,
            self._quote("10000", 1637, "6.01"),
            Decimal("10000"),
            target_rate=Decimal("1.0002"),
            floor_mode="2bps",
            buffer_bps=2,
            affiliate_bps=1,
            usdc_refund_fee=Decimal("0.25"),
            gas_usd=Decimal("0.39"),
        )
        self.assertLess(plan.deposit, Decimal("10000"))
        self.assertGreater(plan.net_profit, 0)
        self.assertLess(plan.deposit - plan.filled, Decimal("50"))  # Little left over to refund.

    def test_slice_fill_rate_is_the_first_slice_threshold(self):
        from dataclasses import replace
        import math
        quote = self._quote("10000", 720, "6.01")
        pools = replace(CE98_POOLS, spot_rate=Decimal("1.0016"))
        rate = slice_fill_rate(pools, quote, Decimal("1.0002"), "2bps", 2, 1)
        self.assertGreater(rate, Decimal("1.0002"))  # Must also cover fees and the outbound share.

        def fills_at(r):
            m = math.sqrt(float(r / pools.spot_rate))
            moved = replace(pools, usdp_asset=int(pools.usdp_asset * m), usdp_rune=int(pools.usdp_rune / m))
            return simulate_streaming_fill(moved, 10**12, 720, 1000200000000, 3, 1, 241474082)[0] > 0

        self.assertTrue(fills_at(rate * Decimal("1.00001")))
        self.assertFalse(fills_at(rate * Decimal("0.99999")))


if __name__ == "__main__":
    unittest.main()
