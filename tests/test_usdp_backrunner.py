import contextlib
import io
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from eth_abi import decode, encode
from eth_utils import keccak

from src.engines.usdp_arb_engine import StableQuote
from src.engines.usdp_backrunner import (
    CURVE_ROUTER_NG,
    DEFAULT_V4_POOLS,
    DEFAULT_VENUES,
    TRANSFER_TOPIC,
    USDG_ADDRESS,
    USDP_ADDRESS,
    V4_QUOTE_GAS,
    STABLE_POOL_ADDRESS,
    USDC_ADDRESS,
    USDP_3CRV_POOL,
    FloatInventory,
    USDPBackrunner,
    Opportunity,
    choose_payout,
    choose_size,
    main,
    refine_sizes,
    token_overrides,
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


WALLET = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
SEL_BALANCE = keccak(text="balanceOf(address)")[:4]
SEL_ALLOWANCE = keccak(text="allowance(address,address)")[:4]
SEL_ETH_BALANCE = keccak(text="getEthBalance(address)")[:4]
SWAP_LOCAL = "(uint256,address,address,uint64,address,uint64,uint256)"


class FakeChain:
    """Multicall stand-in: the 3Crv pool sells USDP `discount` under $1 up to `dip_until` USDC, then 0.5% above $1.

    Token balances and allowances come from dicts keyed by lower-case addresses; other venues fail to quote.
    """

    def __init__(self, balances=None, allowances=None, discount=Decimal("0.003"), dip_until=usdc(500), eth=10**18):
        self.balances = {(t.lower(), o.lower()): v for (t, o), v in (balances or {}).items()}
        self.allowances = {(t.lower(), o.lower(), s.lower()): v for (t, o, s), v in (allowances or {}).items()}
        self.balances.setdefault((USDC_ADDRESS.lower(), STABLE_POOL_ADDRESS.lower()), usdc(158_000))
        self.discount, self.dip_until, self.eth = discount, dip_until, eth

    def __call__(self, calls, block=None):
        out = []
        for target, data in calls:
            sel, args = data[:4], data[4:]
            if sel == SEL_BALANCE:
                owner = decode(["address"], args)[0]
                out.append(encode(["uint256"], [self.balances.get((target.lower(), owner.lower()), 0)]))
            elif sel == SEL_ALLOWANCE:
                owner, spender = decode(["address", "address"], args)
                out.append(encode(["uint256"], [self.allowances.get((target.lower(), owner.lower(), spender.lower()), 0)]))
            elif sel == SEL_ETH_BALANCE:
                out.append(encode(["uint256"], [self.eth]))
            elif target == USDP_3CRV_POOL:
                amount = decode(["int128", "int128", "uint256"], args)[2]
                rate = 1 + self.discount if amount <= self.dip_until else Decimal("0.995")
                out.append(encode(["uint256"], [int(Decimal(amount) * rate) * 10**12]))
            else:
                out.append(None)
        return out


def float_bot(chain, **kwargs):
    kwargs.setdefault("float_usdc", Decimal("2000"))
    bot = USDPBackrunner(engine=fake_engine(), v4_pools=(), float_wallet=WALLET, **kwargs)
    bot.multicall = MagicMock(side_effect=chain)
    bot._w3 = MagicMock()
    return bot


def transfer_log(token, to, amount):
    return {"address": token, "topics": [TRANSFER_TOPIC, bytes(12) + bytes.fromhex(USDP_3CRV_POOL[2:]),
                                          bytes(12) + bytes.fromhex(to[2:])], "data": encode(["uint256"], [amount])}


class FloatScanTests(unittest.TestCase):
    def test_small_dip_becomes_a_float_buy_refined_inside_the_dip(self):
        bot = float_bot(FakeChain(balances={(USDC_ADDRESS, WALLET): usdc(5000)}))
        opps = bot.scan(block=123)
        self.assertEqual([(o.venue, o.kind) for o in opps], [("curve-3crv", "float")])
        # 400 USDC is the last float ladder point inside the 500 USDC dip; refinement towards 550 finds 485.7
        self.assertEqual(opps[0].amount_in_raw, 485_714_285)
        self.assertGreaterEqual(opps[0].net_profit_usd, Decimal("0.10"))

    def test_float_mode_is_off_by_default(self):
        bot = USDPBackrunner(engine=fake_engine(), v4_pools=())
        bot.multicall = MagicMock(side_effect=FakeChain())
        self.assertEqual(bot.scan(block=123), [])

    def test_unsold_inventory_cost_shrinks_the_budget(self):
        bot = float_bot(FakeChain())
        bot.inventory.cost_raw = usdc(1800)
        self.assertEqual(bot.scan(block=123)[0].amount_in_raw, usdc(200))

    def test_live_budget_is_also_capped_by_the_wallet_usdc(self):
        bot = float_bot(FakeChain(balances={(USDC_ADDRESS, WALLET): usdc(300)}), paper=False)
        opp = bot.scan(block=123)[0]
        self.assertEqual(opp.amount_in_raw, usdc(300))
        self.assertEqual(bot.wallet_usdc_raw, usdc(300))

    def test_dip_big_enough_for_the_flash_path_is_not_also_a_float_buy(self):
        bot = float_bot(FakeChain(discount=Decimal("0.004"), dip_until=usdc(5000)))
        self.assertEqual([o.kind for o in bot.scan(block=123)], ["flash"])

    def test_stable_com_fee_share_is_part_of_the_float_cost(self):
        bot = float_bot(FakeChain())
        bot.stable_fee_usd = Decimal("5")  # 0.5% of a 1,000 USDP order eats the whole 0.3% dip
        self.assertEqual(bot.scan(block=123), [])


class FloatBuyTests(unittest.TestCase):
    OPP = Opportunity("curve-3crv", 123, usdc(400), usdp(401.2), Decimal("0.10"), kind="float")

    def test_dry_run_simulates_a_missing_float_and_books_a_paper_buy(self):
        bot = float_bot(FakeChain())
        bot._w3.eth.call.return_value = encode(["uint256"], [usdp(401.1)])
        res = bot.execute_float_buy(self.OPP, live=False, block=123)
        self.assertTrue(res.success)
        self.assertTrue(res.virtual)
        tx, block, overrides = bot._w3.eth.call.call_args.args
        self.assertEqual((tx["from"], tx["to"], block), (WALLET, USDP_3CRV_POOL, 123))
        i, j, amount, min_out = decode(["int128", "int128", "uint256", "uint256"], bytes.fromhex(tx["data"][10:]))
        self.assertEqual((i, j, amount, min_out), (2, 0, usdc(400), usdp(401.2) * 9999 // 10000))
        self.assertEqual(overrides, token_overrides(USDC_ADDRESS, WALLET, USDP_3CRV_POOL, usdc(400), usdc(400)))
        self.assertEqual((bot.inventory.usdp_raw, bot.inventory.cost_raw), (usdp(401.1), usdc(400)))

    def test_paper_mode_counts_an_unchanged_dip_once(self):
        bot = float_bot(FakeChain())
        bot._w3.eth.call.return_value = encode(["uint256"], [usdp(401.1)])
        bot.execute_float_buy(self.OPP, live=False)
        self.assertIsNone(bot.execute_float_buy(self.OPP, live=False))
        self.assertEqual(bot.inventory.buys, 1)

    def test_funded_wallet_is_preflighted_without_overrides(self):
        chain = FakeChain(balances={(USDC_ADDRESS, WALLET): usdc(1000)},
                          allowances={(USDC_ADDRESS, WALLET, USDP_3CRV_POOL): usdc(1000)})
        bot = float_bot(chain)
        bot._w3.eth.call.return_value = encode(["uint256"], [usdp(401.1)])
        res = bot.execute_float_buy(self.OPP, live=False)
        self.assertFalse(res.virtual)
        self.assertIsNone(bot._w3.eth.call.call_args.args[2])

    def test_min_out_never_drops_below_the_usdc_paid(self):
        bot = float_bot(FakeChain(), buffer_bps=500)
        bot._w3.eth.call.return_value = encode(["uint256"], [usdp(401.1)])
        bot.execute_float_buy(self.OPP, live=False)
        tx = bot._w3.eth.call.call_args.args[0]
        self.assertEqual(decode(["int128", "int128", "uint256", "uint256"], bytes.fromhex(tx["data"][10:]))[3], usdp(400))

    def test_preflight_revert_is_reported_and_not_booked(self):
        bot = float_bot(FakeChain())
        bot._w3.eth.call.side_effect = Exception("execution reverted: Exchange resulted in fewer coins than expected")
        res = bot.execute_float_buy(self.OPP, live=False)
        self.assertFalse(res.success)
        self.assertIn("pre-flight reverted", res.error)
        self.assertEqual(bot.inventory.buys, 0)

    def test_live_refuses_when_the_wallet_lacks_usdc(self):
        bot = float_bot(FakeChain(balances={(USDC_ADDRESS, WALLET): usdc(100)}), paper=False)
        bot._send = MagicMock()
        res = bot.execute_float_buy(self.OPP, live=True)
        self.assertFalse(res.success)
        self.assertIn("wallet holds 100.00 USDC", res.error)
        bot._send.assert_not_called()

    def test_live_approves_the_venue_then_swaps_and_books_the_usdp_received(self):
        bot = float_bot(FakeChain(balances={(USDC_ADDRESS, WALLET): usdc(1000)}), paper=False)
        bot._w3.eth.call.return_value = encode(["uint256"], [usdp(401.1)])
        swap_receipt = {"status": 1, "logs": [transfer_log(USDP_ADDRESS, WALLET, usdp(401.05))]}
        bot._send = MagicMock(side_effect=[("0xa", {"status": 1, "logs": []}, Decimal("0.01")),
                                           ("0xb", swap_receipt, Decimal("0.05"))])
        res = bot.execute_float_buy(self.OPP, live=True)
        approve_tx, swap_tx = (c.args[0] for c in bot._send.call_args_list)
        self.assertEqual(approve_tx["to"], USDC_ADDRESS)
        self.assertEqual(decode(["address", "uint256"], bytes.fromhex(approve_tx["data"][10:])),
                         (USDP_3CRV_POOL.lower(), usdc(2000)))  # approves the float cap, not unlimited
        self.assertEqual(swap_tx["to"], USDP_3CRV_POOL)
        self.assertEqual((res.success, res.usdp_raw, res.gas_usd, res.tx_hashes), (True, usdp(401.05), Decimal("0.05"), ["0xa", "0xb"]))
        self.assertEqual(bot.inventory.usdp_raw, usdp(401.05))
        self.assertEqual(bot.inventory.gas_usd, Decimal("0.05"))
        self.assertEqual(bot.inventory.realized_pnl_usd, Decimal("-0.01"))  # the approval serves later buys too

    def test_live_approval_gas_is_booked_even_when_the_buy_then_fails(self):
        bot = float_bot(FakeChain(balances={(USDC_ADDRESS, WALLET): usdc(1000)}), paper=False)
        bot._w3.eth.call.side_effect = Exception("execution reverted")
        bot._send = MagicMock(return_value=("0xa", {"status": 1, "logs": []}, Decimal("0.01")))
        self.assertFalse(bot.execute_float_buy(self.OPP, live=True).success)
        self.assertEqual(bot.inventory.realized_pnl_usd, Decimal("-0.01"))

    def test_live_revert_books_the_gas_as_a_loss(self):
        bot = float_bot(FakeChain(balances={(USDC_ADDRESS, WALLET): usdc(1000)},
                                  allowances={(USDC_ADDRESS, WALLET, USDP_3CRV_POOL): usdc(2000)}), paper=False)
        bot._w3.eth.call.return_value = encode(["uint256"], [usdp(401.1)])
        bot._send = MagicMock(return_value=("0xb", {"status": 0, "logs": []}, Decimal("0.05")))
        res = bot.execute_float_buy(self.OPP, live=True)
        self.assertFalse(res.success)
        self.assertEqual(bot.inventory.realized_pnl_usd, Decimal("-0.05"))
        self.assertEqual(bot.inventory.usdp_raw, 0)


def fake_stable(amount_to=None, fee_native=123):
    def post(path, body):
        if path == "/swap/status":
            return {"asset": {"amountTo": amount_to or body["amountFrom"], "nativeFeeUsd": "0.40"}}
        return {"data": {"maintainerSignature": "0x" + "11" * 65, "deadline": 1792000000, "nonce": 7,
                         "executionFeeNative": fee_native}}
    return MagicMock(side_effect=post)


class FloatSellTests(unittest.TestCase):
    def paper_bot(self, held=usdp(1000.4), cost=usdc(999), chain=None, **kwargs):
        bot = float_bot(chain or FakeChain(), **kwargs)
        bot.inventory.usdp_raw, bot.inventory.cost_raw = held, cost
        bot._stable_post = fake_stable()
        bot._w3.eth.call.return_value = b""
        return bot

    def test_nothing_to_sell_under_the_minimum_order(self):
        bot = self.paper_bot(held=usdp(999.99))
        self.assertIsNone(bot.maybe_sell_float(live=False))
        bot._stable_post.assert_not_called()

    def test_paper_sell_preflights_the_signed_order_from_the_wallet(self):
        bot = self.paper_bot()
        res = bot.maybe_sell_float(live=False)
        self.assertTrue(res.success)
        self.assertTrue(res.virtual)
        quote_body = bot._stable_post.call_args_list[0].args[1]
        self.assertEqual((quote_body["assetTo"], quote_body["amountFrom"], quote_body["addressFrom"], quote_body["addressTo"]),
                         ("USDC", "1000.400000", WALLET, WALLET))
        tx, block, overrides = bot._w3.eth.call.call_args.args
        self.assertEqual((tx["to"], tx["value"], block), (STABLE_POOL_ADDRESS, 123, "latest"))
        swap, signature, fee = decode([SWAP_LOCAL, "bytes", "uint256"], bytes.fromhex(tx["data"][10:]))
        self.assertEqual(swap, (usdp(1000.4), USDP_ADDRESS.lower(), USDC_ADDRESS.lower(), 1, WALLET.lower(), 1792000000, 7))
        self.assertEqual((signature, fee), (bytes([0x11]) * 65, 123))
        self.assertIn(USDP_ADDRESS, overrides)
        self.assertEqual(overrides[WALLET], {"balance": hex(10**18)})  # keeps the real ETH when it covers the fee
        # 1,000.40 USDC back for 999 USDC paid, minus 150k gas ($0.0375) and the $0.40 Stable.com fee
        self.assertEqual(res.pnl_usd, Decimal("0.9625"))
        self.assertEqual((bot.inventory.usdp_raw, bot.inventory.cost_raw, bot.inventory.sells), (0, 0, 1))

    def test_order_is_cut_to_stable_com_six_decimals(self):
        bot = self.paper_bot(held=usdp(1000.4) + 999_999_999_999)
        bot.maybe_sell_float(live=False)
        self.assertEqual(bot._stable_post.call_args_list[0].args[1]["amountFrom"], "1000.400000")
        self.assertEqual(bot.inventory.usdp_raw, 999_999_999_999)

    def test_falls_back_to_the_next_payout_token_when_stable_com_is_short_of_usdc(self):
        chain = FakeChain(balances={(USDC_ADDRESS, STABLE_POOL_ADDRESS): usdc(500), (USDG_ADDRESS, STABLE_POOL_ADDRESS): usdc(50_000)})
        bot = self.paper_bot(chain=chain, float_sell_assets=("USDC", "USDG"))
        res = bot.maybe_sell_float(live=False)
        self.assertEqual(res.target, "USDG")
        swap = decode([SWAP_LOCAL, "bytes", "uint256"], bytes.fromhex(bot._w3.eth.call.call_args.args[0]["data"][10:]))[0]
        self.assertEqual(swap[2], USDG_ADDRESS.lower())

    def test_holds_when_stable_com_quotes_below_one_to_one(self):
        bot = self.paper_bot()
        bot._stable_post = fake_stable(amount_to="999.000000")
        res = bot.maybe_sell_float(live=False)
        self.assertFalse(res.success)
        self.assertIn("holding", res.error)
        self.assertIsNone(bot.maybe_sell_float(live=False))  # same reason is reported once
        self.assertEqual(bot._stable_post.call_count, 2)  # quotes only, no order created
        self.assertEqual(bot.inventory.usdp_raw, usdp(1000.4))

    def test_failed_sell_waits_before_creating_another_order(self):
        bot = self.paper_bot()
        bot._w3.eth.call.side_effect = Exception("execution reverted")
        self.assertIn("pre-flight reverted", bot.maybe_sell_float(live=False).error)
        self.assertIsNone(bot.maybe_sell_float(live=False))
        self.assertEqual(bot._stable_post.call_count, 2)

    def test_live_sell_approves_usdp_and_books_the_usdc_received(self):
        chain = FakeChain(balances={(USDP_ADDRESS, WALLET): usdp(1000.4)})
        bot = self.paper_bot(chain=chain, paper=False)
        bot.wallet_usdp_raw = usdp(1000.4)
        swap_receipt = {"status": 1, "logs": [transfer_log(USDC_ADDRESS, WALLET, usdc(1000.4))]}
        bot._send = MagicMock(side_effect=[("0xa", {"status": 1, "logs": []}, Decimal("0.01")),
                                           ("0xb", swap_receipt, Decimal("0.03"))])
        res = bot.maybe_sell_float(live=True)
        approve_tx, swap_tx = (c.args[0] for c in bot._send.call_args_list)
        self.assertEqual((approve_tx["to"], swap_tx["to"], swap_tx["value"]), (USDP_ADDRESS, STABLE_POOL_ADDRESS, 123))
        self.assertIsNone(bot._w3.eth.call.call_args.args[2])  # live pre-flight never simulates balances
        self.assertEqual(res.pnl_usd, Decimal("1000.4") - 999 - Decimal("0.03") - Decimal("0.40"))
        self.assertEqual(bot.inventory.realized_pnl_usd, res.pnl_usd - Decimal("0.01"))  # plus the approval
        self.assertEqual(bot.wallet_usdp_raw, 0)


class ChoosePayoutTests(unittest.TestCase):
    def test_first_token_that_covers_the_whole_order_wins(self):
        room = {"USDC": usdc(500), "USDG": usdc(2000), "PYUSD": usdc(9000)}
        self.assertEqual(choose_payout(("USDC", "USDG", "PYUSD"), room, usdp(1500)), ("USDG", usdp(1500)))

    def test_partial_order_from_the_deepest_token(self):
        room = {"USDC": usdc(1200), "USDG": usdc(300)}
        self.assertEqual(choose_payout(("USDC", "USDG"), room, usdp(1500)), ("USDC", usdp(1200)))

    def test_nothing_when_no_token_reaches_the_minimum(self):
        self.assertIsNone(choose_payout(("USDC",), {"USDC": usdc(999)}, usdp(1500)))


class FloatInventoryTests(unittest.TestCase):
    def test_sell_books_cost_and_gas_of_the_tracked_usdp_and_untracked_usdp_at_cost(self):
        inv = FloatInventory()
        inv.record_buy(usdc(400), usdp(401), Decimal("0.10"))
        inv.record_buy(usdc(600), usdp(601), Decimal("0.10"))
        # 1,002 USDP bought by the bot plus 100 USDP that was already in the wallet
        pnl = inv.record_sell(usdp(1102), usdc(1102), Decimal("0.05"), Decimal("0.40"))
        self.assertEqual(pnl, Decimal("2") - Decimal("0.20") - Decimal("0.05") - Decimal("0.40"))
        self.assertEqual((inv.usdp_raw, inv.cost_raw, inv.gas_usd, inv.realized_pnl_usd), (0, 0, Decimal(0), pnl))

    def test_partial_sell_keeps_the_rest_at_average_cost(self):
        inv = FloatInventory()
        inv.record_buy(usdc(2000), usdp(2004), Decimal("0.20"))
        inv.record_sell(usdp(1002), usdc(1002), Decimal(0), Decimal(0))
        self.assertEqual((inv.usdp_raw, inv.cost_raw, inv.gas_usd), (usdp(1002), usdc(1000), Decimal("0.10")))

    def test_state_file_round_trip(self):
        inv = FloatInventory()
        inv.record_buy(usdc(400), usdp(401.2), Decimal("0.123"), venue="curve-frax")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "float.json"
            inv.save(path)
            loaded = FloatInventory.load(path)
        self.assertEqual(loaded, inv)
        self.assertEqual(FloatInventory.load(Path("/nonexistent/float.json")), FloatInventory())


class TokenOverrideTests(unittest.TestCase):
    def test_balance_slots_match_the_ones_verified_on_mainnet(self):
        # USDC balances live in mapping slot 9 and USDP balances in slot 1 (checked with eth_call overrides)
        usdc_diff = token_overrides(USDC_ADDRESS, WALLET, USDP_3CRV_POOL, 5, 6)[USDC_ADDRESS]["stateDiff"]
        usdp_diff = token_overrides(USDP_ADDRESS, WALLET, STABLE_POOL_ADDRESS, 5, 6)[USDP_ADDRESS]["stateDiff"]
        self.assertEqual(usdc_diff["0x4282f4532a176fc3ed0ec96af1f91f5d860712abeb21c8c84ce4db8770206c6a"], "0x" + "00" * 31 + "05")
        self.assertEqual(usdp_diff["0x1f36e07cf1e841f190dd296a85495c7e24c6341f16c39820bb65390fa75bb6fc"], "0x" + "00" * 31 + "05")
        self.assertEqual(sorted(usdc_diff.values()), ["0x" + "00" * 31 + "05", "0x" + "00" * 31 + "06"])


class CliTests(unittest.TestCase):
    def test_live_requires_confirmation_token(self):
        self.assertEqual(main(["--live", "--once"]), 2)

    def test_float_live_requires_the_operator_key(self):
        with patch.dict(os.environ, {"ETH_OPERATOR_PRIVATE_KEY": ""}):
            self.assertEqual(main(["--live", "--confirm-live", "LIVE", "--float-usdc", "2000", "--once"]), 2)

    def test_float_status_prints_the_inventory_file(self):
        inv = FloatInventory()
        inv.record_buy(usdc(400), usdp(401.2), Decimal("0.1"))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            inv.save(Path(tmp) / "float.json")
            self.assertEqual(main(["--float-status", "--float-state", str(Path(tmp) / "float.json")]), 0)
        self.assertIn("float inventory 401.20 USDP (cost 400.00 USDC", out.getvalue())


if __name__ == "__main__":
    unittest.main()
