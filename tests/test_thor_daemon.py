"""Offline daemon regressions. Never spawn a process or contact an RPC."""
from decimal import Decimal as D
from io import BytesIO
from unittest.mock import MagicMock, patch
import unittest

from src.engines import thorchain_usdp_sniper as engine
from src.web import thor_app as web

SCALE = engine.THORCHAIN_SCALE
ADDRESS = '0x' + '1' * 40


def quote(memo=None):
    return engine.parse_quote({
        'expected_amount_out': str(10010*SCALE),
        'fees': {'liquidity': str(6*SCALE), 'outbound': str(2*SCALE)},
        'memo': memo if memo is not None else f'=:ETH.USDP:{ADDRESS}:0/20/720:none:0',
    }, D(10000))


def arguments(*argv):
    # Avoid environment-dependent user defaults in deterministic unit tests.
    with patch.dict(engine.os.environ, {'THORCHAIN_SNIPER_MIN_PROFIT_BPS': '', 'THORCHAIN_FLOOR_MODE': '2bps',
                                     'THORCHAIN_SNIPER_MIN_NET_PROFIT': '1'}):
        return engine.build_parser().parse_args(['--amount', '10000', '--affiliate', 'none', *argv])


class DaemonSettingsTests(unittest.TestCase):
    def test_explicit_trigger_is_not_silently_replaced_by_floor_mode(self):
        args = arguments('--floor-mode', '1to1', '--min-profit-bps', '2')
        engine.resolve_sniper_settings(args)
        self.assertEqual(args.min_profit_bps, D(2))
        self.assertEqual(args.min_usdp, D(10002))

    def test_omitted_trigger_uses_the_floor_default(self):
        args = arguments('--floor-mode', '1to1')
        engine.resolve_sniper_settings(args)
        self.assertEqual(args.min_profit_bps, 0)
        self.assertEqual(args.min_usdp, D(10000))

    def test_invalid_settings_fail_before_rpc_or_signing_setup(self):
        for argv in [('--amount', 'NaN'), ('--min-net-profit', '-1'), ('--poll-interval', '0')]:
            with self.subTest(argv=argv), patch.object(engine, 'init_web3') as rpc, self.assertLogs(engine.logger, level='ERROR'):
                self.assertEqual(engine.run_sniper(arguments(*argv)), 1)
                rpc.assert_not_called()

    def test_resting_daemon_cli_does_not_launch_anything_when_parsed(self):
        with patch.object(engine, 'init_web3') as rpc:
            args = arguments('--daemon', '--resting')
            self.assertTrue(args.daemon)
            self.assertTrue(args.resting)
            rpc.assert_not_called()

    def test_streaming_defaults_request_maximum_slices_every_two_blocks(self):
        for argv in ((), ('--resting',)):
            with self.subTest(argv=argv):
                args = arguments(*argv)
                self.assertEqual(args.interval, 2)
                self.assertEqual(args.quantity, 0)


class FillDecisionTests(unittest.TestCase):
    def test_real_fill_candidate_is_not_hidden_by_a_cheaper_zero_fill_refund(self):
        with patch.object(engine, 'simulate_streaming_fill', side_effect=[(0, 0), (10*SCALE, 11*SCALE)]):
            plan = engine.plan_deposit(engine.PoolSpot(D(1), D(1), D(1)), [D(10), D(20)], D(10),
                                       lambda deposit: int(deposit*SCALE), 3, 0, D('2.5'), D('.25'), D('.4'))
        self.assertEqual(plan.deposit, 20)
        self.assertEqual(plan.filled, 10)
        self.assertLess(plan.net_profit, D('-.65'))
        self.assertFalse(engine.plan_clears_profit_check(plan, D(1)))

    def test_zero_fill_is_never_a_valid_trade_even_at_zero_minimum(self):
        plan = engine.FillPlan(D(10), 1, D(0), D(0), D(10), D(0), D(0))
        self.assertFalse(engine.plan_clears_profit_check(plan, D(0)))
        self.assertIn('Price floor blocks', engine.describe_plan(plan))

    def test_missing_memo_or_quantity_does_not_become_a_one_slice_plan(self):
        for memo in ('', f'=:ETH.USDP:{ADDRESS}:0/0/0'):
            with self.subTest(memo=memo):
                pools = engine.PoolSpot(D(1), D(1), D(1))
                with self.assertRaises(engine.SniperError):
                    engine.plan_for_quote(pools, quote(memo), D(10000),
                                          D('1.0002'), '2bps', 2, 0, D('.25'), D('.4'))
                self.assertIsNone(engine.slice_fill_rate(pools, quote(memo), D('1.0002'), '2bps', 2, 0))

    def test_enrolled_minimum_fees_are_loaded_per_leg_and_cached(self):
        client = engine.ThorClient()
        data = {'whitelist_state': '1', 'pairs': [
            {'pair': engine.USDC_ASSET+'|THOR.RUNE', 'dynamic_bps': '2'},
            {'pair': 'THOR.RUNE|'+engine.USDP_ASSET, 'dynamic_bps': '5'}]}
        first, second = quote(), quote()
        config = {'int_64_values': {'L1SlipMinBps': 10, 'L1DynamicFeeEnabled': 1,
                                   'StreamingSwapMaxLength': 14400}}
        with patch.object(client, 'get', side_effect=[config, {}, data]) as read:
            engine.load_quote_fee_floors(client, first, 'symbiosis', 1)
            engine.load_quote_fee_floors(client, second, 'symbiosis', 1)
        self.assertEqual(first.fee_floors, (2, 5))
        self.assertEqual(second.fee_floors, (2, 5))
        self.assertEqual([call.args[0] for call in read.call_args_list],
                         ['constants', 'mimir', 'dynamic_l1_fees/symbiosis'])

    def test_monitor_only_affiliate_does_not_use_inactive_dynamic_floors(self):
        client, row = engine.ThorClient(), quote()
        config = {'int_64_values': {'L1SlipMinBps': 10, 'L1DynamicFeeEnabled': 1,
                                   'StreamingSwapMaxLength': 14400}}
        with patch.object(client, 'get', side_effect=[config, {}, {'whitelist_state': '2', 'pairs': []}]):
            engine.load_quote_fee_floors(client, row, 'symbiosis', 1)
        self.assertEqual(row.fee_floors, (10, 10))

    def test_disabled_dynamic_master_switch_uses_network_floor(self):
        client, row = engine.ThorClient(), quote()
        config = {'int_64_values': {'L1SlipMinBps': 10, 'L1DynamicFeeEnabled': 1,
                                   'StreamingSwapMaxLength': 14400}}
        with patch.object(client, 'get', side_effect=[config, {'L1DYNAMICFEEENABLED': 0}]) as read:
            engine.load_quote_fee_floors(client, row, 'symbiosis', 1)
        self.assertEqual(row.fee_floors, (10, 10))
        self.assertEqual(read.call_count, 2)

    def test_unavailable_live_fee_config_blocks_deposit_modeling(self):
        client, row = engine.ThorClient(), quote()
        with patch.object(client, 'get', side_effect=engine.SniperError('unavailable')), \
             self.assertLogs(engine.logger, level='WARNING'):
            engine.load_quote_fee_floors(client, row, 'symbiosis', 1)
        pools = engine.PoolSpot(D(1), D(1), D(1))
        self.assertIsNone(engine.slice_fill_rate(pools, row, D('1.0002'), '2bps', 2, 1))
        with self.assertRaises(engine.SniperError):
            engine.plan_for_quote(pools, row, D(10000), D('1.0002'), '2bps', 2, 1, D('.25'), D('.38'))

    def test_stable_override_requires_both_assets_to_be_tor_anchors(self):
        config = {'int_64_values': {'L1SlipMinBps': 10, 'L1DynamicFeeEnabled': 0,
                                   'StreamingSwapMaxLength': 14400, 'StableSlipMinBps': 2}}
        anchors = {'TORANCHOR-'+asset.replace('.', '-'): 1 for asset in (engine.USDC_ASSET, engine.USDP_ASSET)}
        for mimir, expected in ((anchors, (2, 2)), ({next(iter(anchors)): 1}, (10, 10))):
            client, row = engine.ThorClient(), quote()
            with self.subTest(mimir=mimir), patch.object(client, 'get', side_effect=[config, mimir]):
                engine.load_quote_fee_floors(client, row, 'none', 0)
                self.assertEqual(row.fee_floors, expected)


class DaemonControlTests(unittest.TestCase):
    def test_resting_selection_is_forwarded_and_marked_as_a_daemon(self):
        args = web.daemon_arguments({'mode': 'resting', 'amount': '100000', 'min_profit_bps': '0', 'min_net_profit': '1.5'})
        self.assertIn('--resting', args)
        self.assertIn('--daemon', args)
        self.assertEqual(args[args.index('--interval')+1], '2')
        self.assertEqual(args[args.index('--quantity')+1], '0')
        self.assertEqual(args[args.index('--min-profit-bps')+1], '0')
        self.assertEqual(args[args.index('--min-net-profit')+1], '1.5')
        self.assertNotIn('--no-capacity-check', args)

    def test_immediate_mode_keeps_the_profit_check(self):
        args = web.daemon_arguments({'mode': 'instant', 'min_net_profit': '2.5'})
        self.assertNotIn('--resting', args)
        self.assertNotIn('--no-capacity-check', args)
        self.assertEqual(args[args.index('--min-net-profit')+1], '2.5')

    def test_bad_modes_and_negative_thresholds_are_rejected_without_spawning(self):
        with patch.object(web, 'spawn_sniper') as spawn:
            for payload in ({'mode': 'broken'}, {'min_net_profit': '-1'}, {'poll_interval': 'NaN'}, {'amount': '0'}):
                with self.subTest(payload=payload), self.assertRaises((ValueError, engine.SniperError)):
                    web.daemon_arguments(payload)
            spawn.assert_not_called()

    def test_resting_daemon_is_detected_but_one_offs_and_zombies_are_excluded(self):
        listing = '\n'.join([
            '200 S python /repo/thor_usdp_sniper.py --live',
            '201 S python /repo/thor_usdp_sniper.py --daemon --resting --live',
            '202 S python /repo/thor_usdp_sniper.py --resting --live',
            '203 S python /repo/thor_usdp_sniper.py --once',
            '204 Z python /repo/thor_usdp_sniper.py --daemon --resting'])
        with patch.object(web.subprocess, 'check_output', return_value=listing):
            self.assertEqual(web.sniper_daemon_pids(), [200, 201])


class StopControlTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(web.ThorDashboardHandler)
        handler.path = '/api/sniper/stop'
        handler.headers = {'Content-Length': '2'}
        handler.rfile = BytesIO(b'{}')
        handler._json = MagicMock()
        return handler

    def test_stop_does_not_claim_success_when_a_deposit_is_still_placing(self):
        handler = self.handler()
        with patch('src.web.thor_calculator.serve_calculator', return_value=False), \
             patch.object(web, 'is_sniper_process_running', return_value=(True, 123)), \
             patch.object(web.os, 'kill') as kill, \
             patch.object(web, 'sniper_daemon_pids', return_value=[123]), \
             patch.object(web.time, 'monotonic', side_effect=[0, 3]), \
             patch.object(web, 'SNIPER_PID_PATH') as pid_file:
            handler.do_POST()
        kill.assert_called_once_with(123, web.signal.SIGTERM)
        self.assertEqual(handler._json.call_args.args[0], 409)
        self.assertIn('not force-killed', handler._json.call_args.args[1]['error'])
        pid_file.unlink.assert_not_called()

    def test_confirmed_stop_explains_that_submitted_streams_continue(self):
        handler = self.handler()
        with patch('src.web.thor_calculator.serve_calculator', return_value=False), \
             patch.object(web, 'is_sniper_process_running', return_value=(True, 123)), \
             patch.object(web.os, 'kill'), \
             patch.object(web, 'sniper_daemon_pids', return_value=[]), \
             patch.object(web, 'SNIPER_PID_PATH') as pid_file:
            handler.do_POST()
        self.assertEqual(handler._json.call_args.args[0], 200)
        self.assertIn('continues on-chain', handler._json.call_args.args[1]['message'])
        pid_file.unlink.assert_called_once()


class MonitoringLoopTests(unittest.TestCase):
    def setup_rpc(self):
        rpc = MagicMock()
        rpc.eth.account.from_key.return_value.address = ADDRESS
        rpc.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 10000*10**6
        rpc.eth.get_balance.return_value = 10**17
        rpc.to_wei.return_value = 3*10**15
        rpc.from_wei.return_value = D('.1')
        return rpc

    def test_profitable_spot_cannot_bypass_rate_limit_cooldown(self):
        rpc, client = self.setup_rpc(), MagicMock()
        client.get.side_effect = engine.SniperError('HTTP 429')
        pools = engine.PoolSpot(D('1.1'), D(1000000), D(60000))
        with patch.object(engine, 'init_web3', return_value=(rpc, 'mock-rpc')), \
             patch.object(engine, 'ThorClient', return_value=client), \
             patch.object(engine, 'fetch_pool_spot_rate', return_value=pools), \
             patch.dict(engine.os.environ, {'ETH_OPERATOR_PRIVATE_KEY': 'mocked-not-used'}), \
             patch.object(engine.time, 'monotonic', return_value=10), \
             patch.object(engine.time, 'sleep', side_effect=[None, StopIteration]), \
             self.assertLogs(engine.logger, level='INFO') as logs:
            with self.assertRaises(StopIteration):
                engine.run_sniper(arguments('--poll-interval', '1'))
        self.assertEqual(client.get.call_count, 1)
        self.assertTrue(any('cooldown' in message.lower() for message in logs.output))

    def test_resting_mode_places_one_stream_and_exits_without_monitoring(self):
        rpc, client = self.setup_rpc(), MagicMock()
        client.get.return_value = {**quote(f'=:ETH.USDP:{ADDRESS}:0/2/0:none:0').raw,
                                   'max_streaming_quantity': 3303}
        pools = engine.PoolSpot(D('1.01'), D(1000000), D(60000))
        with patch.object(engine, 'init_web3', return_value=(rpc, 'mock-rpc')), \
             patch.object(engine, 'ThorClient', return_value=client), \
             patch.object(engine, 'fetch_pool_spot_rate', return_value=pools), \
             patch.object(engine, 'slice_fill_rate', return_value=D('1.001')), \
             patch.object(engine, 'execute_deposit', return_value='0xmock') as deposit, \
             patch.object(engine, 'monitor_outbound') as monitor, \
             patch.object(engine.signal, 'signal'), \
             patch.dict(engine.os.environ, {'ETH_OPERATOR_PRIVATE_KEY': 'mocked-not-used'}), \
             self.assertLogs(engine.logger, level='INFO'):
            code = engine.run_sniper(arguments('--daemon', '--resting', '--live', '--confirm-live', 'EXECUTE_THOR_SNIPER'))
        self.assertEqual(code, 0)
        deposit.assert_called_once()
        monitor.assert_not_called()
        self.assertIn('/2/0:', deposit.call_args.kwargs['memo'])
        requested_params = client.get.call_args_list[0].args[1]
        self.assertEqual(requested_params['streaming_interval'], 2)
        self.assertEqual(requested_params['streaming_quantity'], 0)


if __name__ == '__main__':
    unittest.main()
