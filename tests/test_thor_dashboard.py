"""Regression coverage for current-swap discovery, settlement, timing and reverse quotes."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import patch

from src.web import thor_app as app

TX = 'a' * 64
OLD = 'b' * 64
WALLET = '0x' + '1' * 40


def status(finalized=False, delivered=False):
    return {'tx': {'id': TX, 'from_address': WALLET,
                   'coins': [{'asset': app.USDC_ASSET, 'amount': '1000000000000'}],
                   'memo': f'=:ETH.USDP:{WALLET}:1000200000000/20/720:symbiosis:1'},
            'stages': {'inbound_observed': {'completed': True, 'final_count': 95},
                       'swap_status': {'pending': not finalized},
                       'swap_finalised': {'completed': finalized},
                       'outbound_signed': {'completed': delivered}}}


def stream():
    return {'tx_id': TX.upper(), 'destination': WALLET, 'interval': 20, 'quantity': 720,
            'count': 10, 'initial_height': 1000, 'last_height': 1200,
            'deposit': '1000000000000', 'in': '10000000000', 'out': '10010000000',
            'trade_target': '1000200000000', 'failed_swaps': [1, 2]}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        app.queue_cache.update(at=float('-inf'), items=[], error=None)

    def test_live_queue_precedes_completed_midgard_history(self):
        def fetch(path, **kwargs):
            if path.startswith('tx/status/'):
                self.assertTrue(path.endswith(TX.upper()))
                return status()
            if path == 'lastblock':
                return [{'thorchain': 1205}]
            raise AssertionError(path)
        with patch.object(app, 'wallet_queue', return_value=[stream()]), \
             patch.object(app, 'recent_deposits', return_value=[{'tx_hash': '0x'+OLD, 'status': 'success'}]), \
             patch.object(app, 'fetch_thornode', side_effect=fetch), \
             patch.object(app, 'block_timestamp', return_value=100):
            result = app.get_deposit_tx_info()
        self.assertEqual(result['tx_id'], '0x'+TX)
        self.assertEqual(result['status'], 'STREAMING')
        self.assertEqual(result['streaming']['success_count'], 8)
        self.assertEqual(result['streaming']['blocks_left'], (720-10)*20-5)
        self.assertEqual(result['timing']['seconds_left'], ((720-10)*20-5)*6)

    def test_no_active_swap_does_not_fall_back_to_completed_or_hardcoded_tx(self):
        with patch.object(app, 'wallet_queue', return_value=[]), \
             patch.object(app, 'recent_deposits', return_value=[{'tx_hash': '0x'+OLD, 'status': 'success'}]), \
             patch.object(app, 'fetch_thornode') as fetch:
            result = app.get_deposit_tx_info()
        self.assertFalse(result['found'])
        self.assertEqual(result['status'], 'NO_ACTIVE_SWAP')
        fetch.assert_not_called()

    def test_pending_deposit_behind_a_newer_completed_action_is_found(self):
        recent = [{'tx_hash': '0x'+OLD, 'status': 'success'}, {'tx_hash': '0x'+TX, 'status': 'pending'}]
        with patch.object(app, 'wallet_queue', return_value=[]), \
             patch.object(app, 'recent_deposits', return_value=recent), \
             patch.object(app, 'fetch_thornode', return_value=status()):
            self.assertEqual(app.get_deposit_tx_info()['tx_id'], '0x'+TX)

    def test_queue_failure_does_not_report_confirmed_idle(self):
        with patch.object(app, 'wallet_queue', return_value=[]), \
             patch.object(app, 'recent_deposits', return_value=[]), \
             patch.dict(app.queue_cache, error='upstream unavailable'):
            self.assertFalse(app.get_deposit_tx_info()['ok'])

    def test_queue_discovers_wallet_only_and_includes_limit_orders(self):
        def fetch(path, **kwargs):
            if path == 'swaps/streaming':
                return [stream(), {**stream(), 'destination': 'somebody else', 'tx_id': OLD}]
            if path == 'queue/swap':
                return []
            return {'limit_swaps': [{'tx_id': OLD, 'sender': WALLET}]}
        with patch.object(app, 'get_operator_address', return_value=WALLET), \
             patch.object(app, 'fetch_thornode', side_effect=fetch):
            result = app.wallet_queue()
        self.assertEqual({r['tx_id'] for r in result}, {TX.upper(), OLD.upper()})

    def test_recent_records_are_sorted_and_indexed_status_wins(self):
        result = app.merge_deposits(
            [{'tx_hash': '0x'+TX, 'timestamp': 10, 'status': 'broadcast'}, {'tx_hash': '0x'+OLD, 'timestamp': 20}],
            [{'tx_hash': TX.upper(), 'timestamp': 15, 'status': 'success'}])
        self.assertEqual(result[0]['tx_hash'], '0x'+OLD)
        self.assertEqual(result[1]['status'], 'success')

    def test_cli_broadcast_is_found_before_midgard_indexes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'sniper.log'
            log.write_text(f'2026-10-23 12:00:00 [INFO] Deposit transaction broadcasted! Hash: 0x{TX}\n')
            with patch.object(app, 'SNIPER_LOG_PATH', log), patch.object(app, 'recorded_deposits', return_value=[]):
                self.assertEqual(app.local_deposits()[0]['tx_hash'], '0x'+TX)


class MetricCacheTests(unittest.TestCase):
    def test_slow_balance_refresh_returns_immediately_and_is_not_duplicated(self):
        future = Future()
        with patch.dict(app.metric_cache, {}, clear=True), patch.object(app.status_pool, 'submit', return_value=future) as submit:
            first = app.cached_metric('wallet', lambda: None, {'rpc_connected': False})
            second = app.cached_metric('wallet', lambda: None, {'rpc_connected': False})
            self.assertTrue(first['refreshing'])
            self.assertTrue(second['refreshing'])
            submit.assert_called_once()
            future.set_result({'rpc_connected': True, 'usdc_balance': '123'})
            ready = app.cached_metric('wallet', lambda: None, {})
            self.assertEqual(ready['usdc_balance'], '123')
            self.assertFalse(ready['refreshing'])


class DepositTests(unittest.TestCase):
    def inspect(self, data, queue=None):
        with patch.object(app, 'fetch_thornode', side_effect=lambda path, **kw: [{'thorchain': 1205}] if path == 'lastblock' else data), \
             patch.object(app, 'transaction_actions', return_value=[]), \
             patch.object(app, 'block_timestamp', return_value=100):
            return app.get_deposit_tx_info('0x'+TX, queue=queue or [])

    def test_finalized_swap_is_not_delivered_early(self):
        result = self.inspect(status(finalized=True))
        self.assertEqual(result['status'], 'OUTBOUND_PENDING')
        self.assertTrue(result['is_active'])
        self.assertFalse(result['is_completed'])

    def test_confirming_and_pending_are_not_completed(self):
        data = status()
        data['stages']['swap_status']['pending'] = False
        self.assertEqual(self.inspect(data)['status'], 'INBOUND_CONFIRMING')
        data['stages']['inbound_observed']['completed'] = False
        self.assertEqual(self.inspect(data)['status'], 'PENDING')

    def test_small_refund_and_partial_delivery_reconcile(self):
        data = status(True, True)
        data['out_txs'] = [
            {'id': 'c'*64, 'memo': 'OUT:'+TX, 'coins': [{'asset': app.USDP_ASSET, 'amount': '999800000000'}]},
            {'id': 'd'*64, 'memo': 'REFUND:'+TX, 'coins': [{'asset': app.USDC_ASSET, 'amount': '50000000'}]}]
        result = self.inspect(data)
        self.assertEqual(result['status'], 'COMPLETED_PARTIAL')
        self.assertEqual(result['settlement']['refund_usdc'], .5)
        self.assertEqual(result['settlement']['swapped_usdc'], 9999.5)
        self.assertTrue(result['is_completed'])

    def test_rapid_stream_does_not_invent_one_slice_per_block_eta(self):
        rapid = {**stream(), 'interval': 0, 'count': 0}
        result = self.inspect(status(), [rapid])
        self.assertIsNone(result['timing']['seconds_left'])
        self.assertIsNone(result['timing']['estimated_finalize_at'])

    def test_limit_order_ttl_is_not_treated_as_slice_interval(self):
        data = status()
        data['tx']['memo'] = data['tx']['memo'].replace('=:', '=<:', 1)
        result = self.inspect(data, [stream()])
        self.assertEqual(result['status'], 'LIMIT_WAITING')
        self.assertIsNone(result['timing']['estimated_finalize_at'])

    def test_rapid_stream_estimate_uses_observed_attempt_pace(self):
        rapid = {**stream(), 'interval': 0, 'last_height': 1020}
        result = self.inspect(status(), [rapid])
        self.assertEqual(result['streaming']['estimated_finalize_height'], 1020 + 710*2)
        self.assertEqual(result['timing']['estimate_basis'], 'observed_pace')

    def test_finalization_uses_stream_end_block_not_midgard_start_date(self):
        actions = [{'type': 'swap', 'date': '1000000000000', 'height': '1000',
                    'metadata': {'swap': {'streamingSwapMeta': {'lastHeight': '5000', 'count': '10', 'quantity': '12', 'failedSwaps': [1], 'inCoin': {'amount': '10000000000'}}}}}]
        with patch.object(app, 'fetch_thornode', return_value=status(True, True)), \
             patch.object(app, 'transaction_actions', return_value=actions), \
             patch.object(app, 'block_timestamp', return_value=2000) as timestamp:
            result = app.get_deposit_tx_info('0x'+TX, queue=[])
        timestamp.assert_called_once_with(5000)
        self.assertEqual(result['timing']['finalized_at'], 2000)
        self.assertEqual(result['timing']['started_at'], 1000)
        self.assertEqual(result['history_stream']['success_count'], 9)
        self.assertEqual(result['settlement']['swapped_usdc'], 100)
        self.assertFalse(result['settlement']['swapped_is_estimate'])

    def test_invalid_hash_never_hits_network(self):
        with patch.object(app, 'fetch_thornode') as fetch:
            self.assertFalse(app.get_deposit_tx_info('../invalid')['ok'])
        fetch.assert_not_called()


class QuoteApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThorServer(('127.0.0.1', 0), app.ThorDashboardHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def get(self, path):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            conn.request('GET', path)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_reverse_pair_uses_usdp_input_usdc_output_and_correct_fee_units(self):
        def fetch(path, params=None, **kwargs):
            self.assertEqual(path, 'quote/swap')
            self.assertEqual(params['from_asset'], app.USDP_ASSET)
            self.assertEqual(params['to_asset'], app.USDC_ASSET)
            self.assertEqual(params['amount'], '1000000000000')
            return {'expected_amount_out': '1000100000000', 'memo': f'=:ETH.USDC:{WALLET}:0/1/10:symbiosis:1',
                    'fees': {'asset': app.USDC_ASSET, 'outbound': '200000000', 'slippage_bps': 7}}
        with patch.object(app, 'fetch_thornode', side_effect=fetch), \
             patch.object(app, 'pool_rate_to_usdp', return_value=1.001):
            code, result = self.get('/api/quote?source=usdp&amount=10000')
        self.assertEqual(code, 200)
        self.assertEqual((result['source'], result['target']), ('usdp', 'usdc'))
        self.assertFalse(result['can_execute'])
        self.assertEqual(result['expected_output'], 10001)
        self.assertEqual(result['fees']['outbound'], 2)
        self.assertEqual(result['fees']['slippage_bps'], 7)
        self.assertIn('ETH.USDC', result['memo'])

    def test_reverse_market_minimum_uses_net_quote_instead_of_input_parity(self):
        def fetch(path, params=None, **kwargs):
            self.assertEqual(params['liquidity_tolerance_bps'], '50')
            return {'expected_amount_out': '996600000000', 'memo': f'=:ETH.USDC:{WALLET}:0/0/328:symbiosis:1',
                    'fees': {'asset': app.USDC_ASSET}}
        with patch.object(app, 'fetch_thornode', side_effect=fetch), patch.object(app, 'pool_rate_to_usdp', return_value=.998):
            code, result = self.get('/api/quote?source=usdp&amount=10000&floor_mode=market&slippage_bps=50')
        self.assertEqual(code, 200)
        self.assertEqual(result['minimum_output'], 9916.17)
        self.assertEqual(result['minimum_output_atomic'], '991617000000')
        self.assertTrue(result['quote_meets_floor'])
        self.assertIn(':991617000000/0/328:', result['memo'])
        self.assertFalse(result['can_execute'])

    def test_explicit_premium_floor_is_retained_and_flagged_when_unmet(self):
        quote = {'expected_amount_out': '996600000000', 'memo': f'=:ETH.USDC:{WALLET}:0/0/328:symbiosis:1',
                 'fees': {'asset': app.USDC_ASSET}}
        with patch.object(app, 'fetch_thornode', return_value=quote), patch.object(app, 'pool_rate_to_usdp', return_value=.998):
            code, result = self.get('/api/quote?source=usdp&amount=10000&floor_mode=2bps')
        self.assertEqual(code, 200)
        self.assertEqual(result['minimum_output'], 10002)
        self.assertEqual(result['quote_status'], 'FLOOR_NOT_MET')
        self.assertFalse(result['quote_meets_floor'])
        self.assertIn('refund fees', result['floor_warning'])

    def test_reverse_quote_rejects_wrong_output_asset(self):
        quote = {'expected_amount_out': '1000000000000', 'memo': f'=:ETH.USDP:{WALLET}:0/0/10:symbiosis:1',
                 'fees': {'asset': app.USDP_ASSET}}
        with patch.object(app, 'fetch_thornode', return_value=quote), patch.object(app, 'pool_rate_to_usdp', return_value=None):
            code, result = self.get('/api/quote?source=usdp&amount=10000')
        self.assertEqual(code, 400)
        self.assertIn('wrong output asset', result['error'])

    def test_reverse_pair_cannot_use_the_live_execution_endpoint(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            with patch.object(app, 'spawn_sniper') as spawn:
                connection.request('POST', '/api/swap/live',
                                   body=json.dumps({'source': 'usdp', 'confirm': 'EXECUTE_LIVE_SWAP'}),
                                   headers={'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn('quote-only', json.loads(response.read())['error'])
                spawn.assert_not_called()
        finally:
            connection.close()

    def test_bad_amounts_are_rejected_before_network_calls(self):
        for amount in ('0', '-1', 'NaN', 'Infinity', '0.000000001'):
            with self.subTest(amount=amount), patch.object(app, 'fetch_thornode') as fetch:
                code, result = self.get('/api/quote?source=usdp&amount='+amount)
                self.assertEqual(code, 400)
                self.assertFalse(result['ok'])
                fetch.assert_not_called()

    def test_upstream_error_is_not_a_successful_zero_quote(self):
        with patch.object(app, 'fetch_thornode', return_value={'error': 'Pool halted'}), \
             patch.object(app, 'pool_rate_to_usdp', return_value=None):
            code, result = self.get('/api/quote?source=usdp')
        self.assertEqual(code, 400)
        self.assertIn('Pool halted', result['error'])


if __name__ == '__main__':
    unittest.main()
