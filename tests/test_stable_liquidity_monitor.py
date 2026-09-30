import unittest
from unittest.mock import patch, Mock
from src.engines.stable_liquidity_monitor import StableLiquidityMonitor, solana_vault
from src.engines.crosschain_sniper import Route, failure_category, outcome_label, Outcome


class LiquidityMonitorTests(unittest.TestCase):
    def test_replenishment_wakes_route_before_periodic_backend_retry(self):
        route = Route('solana', 'USDG/PYUSD', 'stable-first')
        monitor = StableLiquidityMonitor('solana', '')
        with patch('time.monotonic', return_value=100):
            monitor.arm(route)
            with patch.object(monitor, '_read', return_value={'PYUSD': 10}):
                monitor.poll()
            self.assertFalse(monitor.due(route))
        with patch('time.monotonic', return_value=102):
            with patch.object(monitor, '_read', return_value={'PYUSD': 20}):
                monitor.poll()
            self.assertTrue(monitor.due(route))
            monitor.arm(route)  # backend still behind; no long pause
            self.assertFalse(monitor.due(route))
        with patch('time.monotonic', return_value=112):
            self.assertTrue(monitor.due(route))  # same balance, backend can now catch up

    def test_rpc_failure_still_allows_periodic_check(self):
        route = Route('ethereum', 'PYUSD/USDG', 'dex-first')
        monitor = StableLiquidityMonitor('ethereum', '')
        with patch('time.monotonic', return_value=100):
            monitor.arm(route)
            with patch.object(monitor, '_read', side_effect=RuntimeError('offline')):
                monitor.poll()
            self.assertEqual(monitor.waiting[route.key]['token'], 'PYUSD')
            self.assertFalse(monitor.due(route))
        with patch('time.monotonic', return_value=110):
            self.assertTrue(monitor.due(route))
            monitor.clear(route)
            self.assertFalse(monitor.waiting)

    def test_batch_deduplicates_payout_and_maps_out_of_order_replies(self):
        monitor = StableLiquidityMonitor('ethereum', 'https://rpc.invalid')
        monitor.arm(Route('ethereum', 'USDC/PYUSD', 'stable-first'))
        monitor.arm(Route('ethereum', 'PYUSD/USDG', 'dex-first'))
        monitor.arm(Route('ethereum', 'USDC/USDG', 'stable-first'))
        response = Mock()
        response.json.return_value = [{'id': 1, 'result': '0x20'}, {'id': 0, 'result': '0x10'}]
        with patch('src.engines.stable_liquidity_monitor.requests.post', return_value=response) as post:
            self.assertEqual(monitor._read(), {'USDG': 32, 'PYUSD': 16})
            self.assertEqual(len(post.call_args.kwargs['json']), 2)
            self.assertTrue(all(x['method'] == 'eth_call' for x in post.call_args.kwargs['json']))

    def test_backend_capacity_is_watched_but_rate_limits_are_preserved(self):
        self.assertEqual(failure_category('Stable.com create order failed: insufficient_pool_balance'), 'capacity')
        self.assertNotEqual(failure_category('Stable.com HTTP 429 insufficient_pool_balance'), 'capacity')
        self.assertEqual(outcome_label(Outcome(False, 'low liquidity', 'capacity')), 'WATCHING')
        self.assertNotEqual(solana_vault('PYUSD'), solana_vault('USDG'))

    def test_capacity_outcomes_never_install_five_minute_cooldown(self):
        import threading
        from decimal import Decimal
        from src.engines.crosschain_sniper import _handle_route_outcome, CooldownPolicy, AdaptiveBackoff
        route = Route('solana', 'USDG/PYUSD', 'stable-first')
        for category in ('capacity', 'unstable-capacity'):
            deadlines = {route.key: 9999999999}
            dashboard = Mock()
            _handle_route_outcome(route, Decimal('1'), Outcome(False, 'waiting for PYUSD', category),
                chain='solana', cooldown_policy=CooldownPolicy(30, 300, 3600, 300, 300, 30, 60),
                backoff=AdaptiveBackoff(), route_deadlines=deadlines, dashboard=dashboard,
                logger=Mock(), cooldown_seconds=15, stop=threading.Event())
            self.assertNotIn(route.key, deadlines)
            dashboard.record_cooldown.assert_not_called()
