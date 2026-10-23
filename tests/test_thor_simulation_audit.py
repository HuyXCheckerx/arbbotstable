"""Frozen mainnet snapshot and Go-reference checks; no network or processes."""
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import unittest

from src.engines import thorchain_usdp_sniper as e

SNAPSHOT = json.loads((Path(__file__).parent / 'fixtures/thor_snapshot_28361755.json').read_text())
POOLS = e.PoolSpot(**{k: D(v) if k in ('spot_rate', 'usdc_depth', 'usdp_depth', 'eth_usd') else v
                     for k, v in SNAPSHOT['pools'].items()})


class ProtocolMathTests(unittest.TestCase):
    def test_safe_share_rounds_half_up_and_caps_the_ratio(self):
        self.assertEqual(e.thor_share(1, 2, 1), 1)
        self.assertEqual(e.thor_share(1, 3, 2), 1)
        self.assertEqual(e.thor_share(4, 3, 2), 2)
        self.assertEqual(e.thor_share(0, 0, 2), 0)

    def test_advanced_simulation_matches_independent_go_reference(self):
        for name, quantity in {'rapid': 3296, 'maximum_capacity_quantity': 1743,
                               'resting': 720, 'multi_fill': 3296}.items():
            with self.subTest(name=name):
                pools = replace(POOLS, usdp_asset=POOLS.usdp_asset*103//100) if name == 'multi_fill' else POOLS
                actual = e.simulate_streaming_fill(pools, 9996400000000, quantity, 9998399280000,
                                                   (2, 5), 1, 237317259)
                reference = SNAPSHOT['reference'][name]
                self.assertEqual(actual, (reference['Filled'], reference['AfterAffiliate']))

    def test_affiliate_skim_changes_settlement_instead_of_the_slice_fill_decision(self):
        args = (POOLS, 9996400000000, 3296, 9998399280000, (2, 5))
        filled, gross = e.simulate_streaming_fill(*args, 0, 237317259)
        with_fee_filled, after_fee = e.simulate_streaming_fill(*args, 100, 237317259)
        self.assertEqual(filled, with_fee_filled)
        self.assertEqual(after_fee, gross-e.thor_share(100, 10000, gross))

    def test_quantity_matches_pinned_thornode_quote_and_interval_cap(self):
        self.assertEqual(e.streaming_quantity_for(POOLS, D(99964), (2, 5), 0), 3296)
        self.assertEqual(e.streaming_quantity_for(POOLS, D(99964), (2, 5), 20), 720)
        self.assertEqual(e.streaming_quantity_for(POOLS, D(4875), (2, 5), 0), 160)
        self.assertEqual(e.streaming_quantity_for(POOLS, D(99964), (2, 5), 0, 1000), 1000)

    def test_first_quote_slice_reproduces_node_output_and_exposes_upstream_fee_mismatch(self):
        quote = SNAPSHOT['rapid_quote']
        quantity = quote['max_streaming_quantity']
        size = 9996400000000 // quantity  # quoteSimulateSwap truncates its one sampled slice
        rune = e.clp_leg(POOLS.usdc_asset, size, POOLS.usdc_rune, 2) // 100 * 100
        gross = e.clp_leg(POOLS.usdp_rune, rune, POOLS.usdp_asset, 5) * quantity
        affiliate = e.thor_share(1, 10000, gross)
        self.assertEqual(affiliate, int(quote['fees']['affiliate']))
        # v3.20.3 retains the initial full-size simulation's outboundFeeAmount,
        # but replaces Fees with streamRes.Fees. These are different estimates.
        full_size_fee = int(SNAPSHOT['full_single_quote']['fees']['outbound'])
        self.assertEqual(gross-affiliate-full_size_fee, int(quote['expected_amount_out']))
        self.assertEqual(int(quote['fees']['outbound'])-full_size_fee, 99934443)


if __name__ == '__main__':
    unittest.main()
