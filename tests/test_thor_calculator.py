from decimal import Decimal as D
from http.server import ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.web.thor_calculator import (
    CalculatorHandler, assumptions_from, calculate_report, cycle, _cache,
)
from scripts.quote_thorchain_usdp import SCALE, USDC, USDP


def fixture():
    fees = {'liquidity': '6.02114415', 'affiliate': '1.00293359',
            'outbound': '2.41302692', 'total': '9.43710466'}
    raw = {'expected_amount_out': '1002646640848',
           'fees': {k: str(int(D(v) * SCALE)) for k, v in fees.items()},
           'expiry': int(time.time()) + 600, 'max_streaming_quantity': 1637}
    row = {'profile': 'symbiosis/auto', 'expected_received_usdp': '10026.46640848',
           'sender_gas_cost_usdc': '0.4', 'sender_gas_model': {},
           'fees_usdp': fees, 'expiry_unix': int(time.time()) + 600,
           'estimated_seconds': 3300, 'max_streaming_quantity': 1637,
           'request': {'affiliate_bps': 1}, 'raw_quote': raw, 'warnings': []}
    report = {'input_usdc': '10000', 'routes': [row], 'generated_at_utc': '2026-10-21T10:45:00+00:00',
              'errors': [], 'eth_price_usdc_pool_implied': '2455', 'rpc_gas_price_gwei': '2'}
    pools = [
        {'asset': USDC, 'balance_asset': str(1_000_000 * SCALE), 'balance_rune': str(1_000_000 * SCALE)},
        {'asset': USDP, 'balance_asset': str(60_300 * SCALE), 'balance_rune': str(60_000 * SCALE)}]
    return report, pools


class CalculationTests(unittest.TestCase):
    def test_complete_historical_example_does_not_deduct_embedded_fees_twice(self):
        report, pools = fixture()
        result = calculate_report(report, pools, {'other_cost': '2.6', 'exit_rate': '.9998'})
        r = result['routes'][0]
        self.assertEqual(r['expected']['profit_usdc'], D('21.461115198304'))
        self.assertEqual(r['expected']['costs_usdc'], D(3))
        self.assertEqual(r['expected']['delivered_usdp'], D('10026.46640848'))
        self.assertEqual(r['expected']['break_even_exit'], D(10003) / D('10026.46640848'))
        self.assertEqual(r['target_required_usdp'], D(10013) / D('.9998'))

    def test_partial_fill_charges_full_outbound_and_refund_once(self):
        report, pools = fixture()
        result = calculate_report(report, pools, {'fill_pct': '10', 'other_cost': '2.6'})
        r = result['routes'][0]
        scenario = next(s for s in r['scenarios'] if s['fill_pct'] == 10)
        delivered = (D('10026.46640848') + D('2.41302692')) / 10 - D('2.41302692')
        self.assertEqual(scenario['delivered_usdp'], delivered)
        self.assertEqual(scenario['refund_usdc'], D('8999.75'))
        self.assertEqual(scenario['profit_usdc'], delivered + D('8999.75') - 10003)
        full = next(s for s in r['scenarios'] if s['fill_pct'] == 100)
        self.assertEqual(full['refund_usdc'], 0)
        self.assertEqual(full['profit_usdc'], r['expected']['profit_usdc'])
        zero = next(s for s in r['scenarios'] if s['fill_pct'] == 0)
        self.assertEqual(zero['delivered_usdp'], 0)
        self.assertEqual(zero['profit_usdc'], D('-3.25'))

    def test_missing_gas_never_reports_positive_net_or_selects_a_best_route(self):
        report, pools = fixture()
        report['routes'][0]['sender_gas_cost_usdc'] = None
        result = calculate_report(report, pools, {})
        self.assertIsNone(result['routes'][0]['expected']['profit_usdc'])
        self.assertIsNone(result['best_profile'])
        self.assertEqual(result['verdict'], 'unknown')
        overridden = calculate_report(report, pools, {'gas_override': '1'})
        self.assertIsNotNone(overridden['routes'][0]['expected']['profit_usdc'])

    def test_fee_and_cost_changes_have_correct_units(self):
        report, pools = fixture()
        a = calculate_report(report, pools, {})['routes'][0]
        b = calculate_report(report, pools, {'service_cost': '50'})['routes'][0]
        self.assertEqual(a['expected']['profit_usdc'] - b['expected']['profit_usdc'], D(50))
        self.assertEqual(a['expected']['delivered_usdp'], b['expected']['delivered_usdp'])

    def test_validation_rejects_nonfinite_negative_and_out_of_range_assumptions(self):
        for key, value in [('exit_rate', 'NaN'), ('exit_rate', 'Infinity'), ('conversion_rate', '0'),
                           ('other_cost', '-1'), ('fill_pct', '101'), ('haircut_bps', '10000'),
                           ('gas_override', 'abc')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                assumptions_from({key: value})

    def test_expired_route_is_never_ranked_as_best(self):
        report, pools = fixture()
        report['routes'][0]['expiry_unix'] = int(time.time()) - 1
        result = calculate_report(report, pools, {})
        self.assertIsNone(result['best_profile'])
        self.assertFalse(result['routes'][0]['valid'])

    def test_pool_model_limits_fill_and_reconciles_cashflows(self):
        report, pools = fixture()
        result = calculate_report(report, pools, {})
        r = result['routes'][0]
        p = r['pool_model']
        self.assertIsNotNone(p)
        self.assertLess(p['filled_usdc'], D(10000))
        self.assertGreater(p['filled_usdc'], 0)
        self.assertEqual(p['profit_usdc'], p['sale_usdc'] + p['refund_usdc'] - 10000 - p['costs_usdc'])
        self.assertEqual(p['memo_limit_1e8'], '1000200000000')

    def test_haircut_and_nonparity_conversion_are_applied_once(self):
        report, pools = fixture()
        r = calculate_report(report, pools, {'haircut_bps': '10', 'conversion_rate': '.999', 'exit_rate': '1.002'})['routes'][0]
        expected_q = D('10016.43994207')
        self.assertEqual(r['stress']['delivered_usdp'], expected_q)
        self.assertEqual(r['stress']['sale_usdc'], expected_q * D('.999') * D('1.002'))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), CalculatorHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_readonly_surface_and_static_assets(self):
        for path in ['/', '/calculator', '/static/thor_calculator.js', '/static/thor_calculator.css']:
            with urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + '/api/swap', data=b'{}', headers={'Content-Type': 'application/json'}))
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_quote_and_recalculation_endpoints_with_snapshot(self):
        report, pools = fixture()
        entry = {'report': report, 'pools': pools, 'at': time.time(), 'amount': D(10000)}
        _cache['test-snapshot'] = entry
        try:
            with patch('src.web.thor_calculator.fetch_quotes', return_value=('test-snapshot', entry)):
                with urlopen(self.base + '/api/calculator/quotes?amount=10000') as response:
                    first = json.load(response)
                self.assertTrue(first['ok'])
                self.assertEqual(first['quote_id'], 'test-snapshot')
            payload = {'quote_id': first['quote_id'], 'assumptions': {'exit_rate': '.9998', 'other_cost': '2.6'}}
            with urlopen(Request(self.base + '/api/calculator/calculate', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})) as response:
                second = json.load(response)
            self.assertEqual(second['routes'][0]['expected']['profit_usdc'], '21.461115198304')
        finally:
            _cache.pop('test-snapshot', None)

    def test_invalid_amount_and_unknown_snapshot_are_actionable_errors(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + '/api/calculator/quotes?amount=NaN')
        self.assertEqual(error.exception.code, 400)
        self.assertIn('finite', json.load(error.exception)['error'])
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + '/api/calculator/calculate', data=b'{"quote_id":"missing"}'))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == '__main__':
    unittest.main()
