import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from eth_abi import decode, encode
from eth_utils import keccak

from src.engines.usdp_arb_engine import StableQuote
from src.engines.usdp_backrunner import (
    CURVE_ROUTER_NG,
    DEFAULT_V4_POOLS,
    DEFAULT_VENUES,
    V4_QUOTE_GAS,
    STABLE_POOL_ADDRESS,
    USDC_ADDRESS,
    USDP_3CRV_POOL,
    USDPBackrunner,
    Opportunity,
    choose_size,
    main,
    refine_sizes,
)

E6, E18 = 10**6, 10**18
VENUES = {v.name: v for v in DEFAULT_VENUES}


def usdc(x):
    return int(x * E6)


def usdp(x):
    return int(Decimal(str(x)) * E18)


def fake_engine():
    engine = MagicMock()
    engine.executor = "0x20f5Cc012bBb5323EfBFcC91A7d0C960175D3b2c"
    engine.get_gas_price_gwei.return_value = Decimal("0.1")
    engine.get_eth_usd.return_value = Decimal("2500")
    return engine


def stable_quote(amount_out):
    return StableQuote(
        amount_in_human=amount_out,
        amount_out_human=amount_out,
        pool_balance_human=Decimal("158000"),
        min_order_human=Decimal("1000"),
        token_fee_human=Decimal("0"),
        native_fee_usd=Decimal("0.10"),
        raw_response={},
    )


class ChooseSizeTests(unittest.TestCase):
    LADDER = [(usdc(1000), usdp(1003)), (usdc(2000), usdp(2004)), (usdc(3000), usdp(3001)), (usdc(4000), usdp(3995))]

    def test_profit_mode_picks_highest_profit(self):
        self.assertEqual(choose_size(self.LADDER, "profit", usdp(1000), usdp(10**6), usdc(0.5)), (usdc(2000), usdp(2004)))

    def test_volume_mode_picks_largest_size_that_still_clears_min_profit(self):
        self.assertEqual(choose_size(self.LADDER, "volume", usdp(1000), usdp(10**6), usdc(0.5)), (usdc(3000), usdp(3001)))

    def test_stable_minimum_order_excludes_small_trades(self):
        self.assertIsNone(choose_size([(usdc(500), usdp(503))], "profit", usdp(1000), usdp(10**6), usdc(0.5)))

    def test_stable_capacity_caps_trade_size(self):
        pick = choose_size(self.LADDER, "volume", usdp(1000), usdp(2500), usdc(0.5))
        self.assertEqual(pick, (usdc(2000), usdp(2004)))

    def test_failed_quotes_are_ignored(self):
        self.assertIsNone(choose_size([(usdc(1000), None)], "volume", usdp(1000), usdp(10**6), 0))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            choose_size(self.LADDER, "fastest", 0, usdp(10**6), 0)


class RefineSizesTests(unittest.TestCase):
    LADDER = [usdc(1000), usdc(2000), usdc(4000)]

    def test_volume_refines_towards_next_point(self):
        sizes = refine_sizes(self.LADDER, usdc(2000), "volume", points=3)
        self.assertEqual(sizes, [usdc(2500), usdc(3000), usdc(3500)])

    def test_profit_refines_both_sides(self):
        sizes = refine_sizes(self.LADDER, usdc(2000), "profit", points=5)
        self.assertEqual(sizes, [usdc(1500), usdc(2500), usdc(3000), usdc(3500)])

    def test_last_ladder_point_has_nothing_above(self):
        self.assertEqual(refine_sizes(self.LADDER, usdc(4000), "volume"), [])


class VenueCalldataTests(unittest.TestCase):
    def test_router_swap_calldata_routes_usdc_through_frax_to_receiver(self):
        venue = VENUES["curve-frax"]
        receiver = "0x20f5Cc012bBb5323EfBFcC91A7d0C960175D3b2c"
        data = venue.swap_calldata(usdc(5000), usdp(5010), receiver)
        sig = "exchange(address[11],uint256[5][5],uint256,uint256,address[5],address)"
        self.assertEqual(data[:4], keccak(text=sig)[:4])
        route, params, amount, min_out, _, to = decode(
            ["address[11]", "uint256[5][5]", "uint256", "uint256", "address[5]", "address"], data[4:]
        )
        self.assertEqual(venue.target, CURVE_ROUTER_NG)
        self.assertEqual(route[0].lower(), USDC_ADDRESS.lower())
        self.assertEqual(route[4].lower(), "0x8e870d67f660d95d5be530380d0ec0bd388289e1")  # USDP after two hops
        self.assertEqual([list(p) for p in params[:2]], [[1, 0, 1, 1, 2], [0, 1, 1, 1, 2]])
        self.assertEqual((amount, min_out, to.lower()), (usdc(5000), usdp(5010), receiver.lower()))

    def test_metapool_uses_exchange_underlying_usdc_to_usdp(self):
        venue = VENUES["curve-3crv"]
        data = venue.swap_calldata(usdc(2000), usdp(2001), "0x20f5Cc012bBb5323EfBFcC91A7d0C960175D3b2c")
        self.assertEqual(data[:4], keccak(text="exchange_underlying(int128,int128,uint256,uint256)")[:4])
        self.assertEqual(decode(["int128", "int128", "uint256", "uint256"], data[4:]), (2, 0, usdc(2000), usdp(2001)))
        quote = venue.quote_calldata(usdc(2000))
        self.assertEqual(quote[:4], keccak(text="get_dy_underlying(int128,int128,uint256)")[:4])


def fake_multicall(calls, block):
    """3Crv pool sells USDP at a 0.4% discount up to 5,000 USDC, then 0.5% above $1; other venues fail to quote."""
    out = []
    for target, data in calls:
        if target == USDC_ADDRESS:
            out.append(encode(["uint256"], [usdc(158_000)]))
        elif target == USDP_3CRV_POOL:
            amount = decode(["int128", "int128", "uint256"], data[4:])[2]
            rate = Decimal("1.004") if amount <= usdc(5000) else Decimal("0.995")
            out.append(encode(["uint256"], [int(Decimal(amount) * rate) * 10**12]))
        else:
            out.append(None)
    return out


class ScanAndExecuteTests(unittest.TestCase):
    def setUp(self):
        self.engine = fake_engine()
        self.bot = USDPBackrunner(engine=self.engine, mode="volume", min_profit_usd=Decimal("0.50"), v4_pools=())
        self.bot.multicall = MagicMock(side_effect=fake_multicall)

    def test_scan_sizes_the_dip_and_refines_between_ladder_points(self):
        opps = self.bot.scan(block=123)
        self.assertEqual([o.venue for o in opps], ["curve-3crv"])
        opp = opps[0]
        # coarse ladder stops at 4,000 USDC (5,500 is past the dip); refinement finds 4,857 USDC still inside it
        self.assertEqual(opp.amount_in_raw, 4_857_142_857)
        self.assertGreater(opp.net_profit_usd, Decimal("0.50"))
        self.assertEqual(opp.block, 123)

    def test_scan_respects_stable_com_usdc_capacity(self):
        def low_capacity(calls, block):
            res = fake_multicall(calls, block)
            return [encode(["uint256"], [usdc(1500)]) if t == USDC_ADDRESS else r for (t, _), r in zip(calls, res)]
        self.bot.multicall = MagicMock(side_effect=low_capacity)
        opp = self.bot.scan(block=123)[0]
        self.assertLessEqual(opp.usdp_out, Decimal(1500))

    def test_order_after_buffer_must_clear_stable_minimum(self):
        def exactly_1000(calls, block):
            res = fake_multicall(calls, block)
            out = []
            for (t, data), r in zip(calls, res):
                if t == USDP_3CRV_POOL:
                    amount = decode(["int128", "int128", "uint256"], data[4:])[2]
                    r = encode(["uint256"], [usdp(1000) if amount == usdc(1000) else 0])
                out.append(r)
            return out
        self.bot.min_profit_usd = Decimal("-10")  # isolate the minimum-order rule
        self.bot.multicall = MagicMock(side_effect=exactly_1000)
        self.assertEqual(self.bot.scan(block=123), [])  # 1,000 USDP minus the 1 bp buffer is under Stable.com's minimum

    def test_build_evaluation_targets_pool_with_min_out_buffer(self):
        opp = Opportunity("curve-3crv", 123, usdc(4000), usdp(4016), Decimal("0.14"))
        self.engine.quote_stable.return_value = stable_quote(Decimal("4016"))
        ev = self.bot.build_evaluation(opp)
        self.assertEqual(ev.dex_quote.target.lower(), USDP_3CRV_POOL.lower())
        self.assertEqual(ev.dex_quote.allowance_target.lower(), USDP_3CRV_POOL.lower())
        min_out = decode(["int128", "int128", "uint256", "uint256"], bytes.fromhex(ev.dex_quote.data[2 + 8:]))[3]
        self.assertEqual(min_out, usdp(4016) * 9999 // 10000)
        self.assertEqual(ev.net_profit_usd, Decimal("16") - Decimal("0.14") - Decimal("0.10"))

    def test_execute_dry_run_goes_through_engine_without_broadcast(self):
        opp = Opportunity("curve-3crv", 123, usdc(4000), usdp(4016), Decimal("0.14"))
        self.engine.quote_stable.return_value = stable_quote(Decimal("4016"))
        self.bot.execute(opp, live=False)
        kwargs = self.engine.execute_live_arbitrage.call_args.kwargs
        self.assertTrue(kwargs["dry_run"])
        self.assertEqual(kwargs["leg1_buffer_bps"], 1)

    def test_execute_skips_when_requote_no_longer_clears_min_profit(self):
        opp = Opportunity("curve-3crv", 123, usdc(4000), usdp(4016), Decimal("0.14"))
        self.engine.quote_stable.return_value = stable_quote(Decimal("4000.3"))
        self.assertIsNone(self.bot.execute(opp, live=True))
        self.engine.execute_live_arbitrage.assert_not_called()


class V4Tests(unittest.TestCase):
    def setUp(self):
        self.bot = USDPBackrunner(engine=fake_engine(), venues=(), v4_pools=DEFAULT_V4_POOLS[:1])
        self.bot._w3 = MagicMock()

    def head(self, liquidity):
        # with no Curve venues the first multicall is [Stable.com USDC balance, V4 pool liquidity]
        return [encode(["uint256"], [usdc(158_000)]), encode(["uint256"], [liquidity])]

    def test_pool_id_matches_live_usdp_usdt_pool(self):
        self.assertEqual(DEFAULT_V4_POOLS[0].pool_id.hex(), "90479e47ad4b86ffbeff853adcc42b4071272fc11d4d035f5608bce586b29a4f")

    def test_pool_without_liquidity_is_never_quoted(self):
        self.bot.multicall = MagicMock(return_value=self.head(0))
        self.assertEqual(self.bot.scan(block=5), [])
        self.bot._w3.eth.call.assert_not_called()

    def test_dip_is_quoted_one_capped_call_at_a_time(self):
        self.bot.multicall = MagicMock(return_value=self.head(10**20))

        def v4_quote(tx, block_identifier):
            amount = decode(["((address,address,uint24,int24,address),bool,uint128,bytes)"], tx["data"][4:])[0][2]
            return encode(["uint256", "uint256"], [amount * 10**12 * 1002 // 1000, 0])

        self.bot._w3.eth.call.side_effect = v4_quote
        opps = self.bot.scan(block=5)
        self.assertEqual(opps[0].venue, "v4-usdt-0.10%")
        self.assertTrue(all(c.args[0]["gas"] == V4_QUOTE_GAS for c in self.bot._w3.eth.call.call_args_list))

    def test_no_discount_on_probe_skips_the_ladder(self):
        self.bot.multicall = MagicMock(return_value=self.head(10**20))
        self.bot._w3.eth.call.return_value = encode(["uint256", "uint256"], [usdp(998), 0])
        self.assertEqual(self.bot.scan(block=5), [])
        self.assertEqual(self.bot._w3.eth.call.call_count, 1)


class CliTests(unittest.TestCase):
    def test_live_requires_confirmation_token(self):
        self.assertEqual(main(["--live", "--once"]), 2)


if __name__ == "__main__":
    unittest.main()
