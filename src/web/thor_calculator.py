"""Read-only THORChain quotes and Decimal full-cycle arbitrage calculations."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from scripts.quote_thorchain_usdp import (
    QuoteError, ThorClient, USDC, USDP, SCALE, amount_raw, collect, parser,
)

D = Decimal
ROOT = Path(__file__).resolve().parent
_cache = {}
_cache_lock = threading.Lock()
_quote_lock = threading.Lock()


def decimal(value, label, minimum=D(0), maximum=D('1000000000')):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{label} must be a finite number')
    if not result.is_finite():
        raise ValueError(f'{label} must be a finite number')
    if result < minimum or result > maximum:
        raise ValueError(f'{label} must be between {minimum} and {maximum}')
    return result


def assumptions_from(values):
    if not isinstance(values, dict):
        raise ValueError('assumptions must be an object')
    fields = {
        'conversion_rate': ('Conversion ratio', '1', D('0.000001'), D(10)),
        'exit_rate': ('Exit rate', '1', D('0.000001'), D(10)),
        'other_cost': ('Conversion and exit costs', '3', D(0), D('1000000')),
        'service_cost': ('Extra service fee budget', '0', D(0), D('1000000')),
        'refund_fee': ('USDC refund fee', '0.25', D(0), D('1000000')),
        'target_profit': ('Target profit', '10', D(0), D('1000000')),
        'haircut_bps': ('Output haircut', '1', D(0), D(9999)),
        'floor_bps': ('Memo floor above parity', '2', D(0), D(10000)),
        'fill_pct': ('Partial fill percentage', '10', D(0), D(100)),
    }
    result = {k: decimal(values.get(k, default), label, lo, hi)
              for k, (label, default, lo, hi) in fields.items()}
    override = values.get('gas_override')
    result['gas_override'] = None if override in (None, '') else decimal(override, 'Sender gas budget', maximum=D('1000000'))
    return result


def cycle(amount, delivered, conversion, exit_rate, costs, refund=D(0)):
    """All token deductions are already in delivered; costs are separate USDC."""
    converted = delivered * conversion
    returned = converted * exit_rate
    profit = None if costs is None else returned + refund - amount - costs
    break_even = None if costs is None or converted == 0 else max(D(0), (amount + costs - refund) / converted)
    return {
        'delivered_usdp': delivered, 'converted_tokens': converted,
        'sale_usdc': returned, 'refund_usdc': refund,
        'returned_usdc': returned + refund, 'costs_usdc': costs,
        'profit_usdc': profit,
        'roi_bps': profit / amount * 10000 if profit is not None else None,
        'break_even_exit': break_even,
    }


def pool_model(row, pools, amount, a, costs):
    """Reuse the bot's snapshot replay; never invoke execution or wallet code."""
    from src.engines.thorchain_usdp_sniper import PoolSpot, parse_quote, quote_fee_tier_bps, simulate_streaming_fill
    by_asset = {p['asset']: p for p in pools}
    u, v = by_asset[USDC], by_asset[USDP]
    ua, ur, va, vr = [int(x) for x in (u['balance_asset'], u['balance_rune'], v['balance_asset'], v['balance_rune'])]
    snapshot = PoolSpot(D(ur) * va / (D(ua) * vr), D(ua) / SCALE, D(va) / SCALE, ur, ua, vr, va)
    q = parse_quote(row['raw_quote'], amount)
    floor = quote_fee_tier_bps(q)
    quantity = max(1, int(row['max_streaming_quantity']))
    target = int(amount * (1 + a['floor_bps'] / 10000) * SCALE)
    outbound = D(row['fees_usdp']['outbound'])
    filled_raw, emitted_raw = simulate_streaming_fill(
        snapshot, int(amount * SCALE), quantity, target, floor,
        int(row['request'].get('affiliate_bps', 0)), int(outbound * SCALE),
    )
    filled = D(filled_raw) / SCALE
    delivered = max(D(0), D(emitted_raw) / SCALE - outbound) if filled else D(0)
    refund = max(D(0), amount - filled - a['refund_fee']) if filled < amount else D(0)
    result = cycle(amount, delivered, a['conversion_rate'], a['exit_rate'], costs, refund)
    result.update(filled_usdc=filled, fill_pct=filled / amount * 100, inferred_leg_floor_bps=floor,
                  memo_limit_1e8=str(target), quantity=quantity)
    return result


def calculate_report(report, pools, values):
    a = assumptions_from(values)
    amount = D(report['input_usdc'])
    results = []
    for row in report['routes']:
        q = D(row['expected_received_usdp'])
        outbound = D(row['fees_usdp']['outbound'])
        gas = a['gas_override']
        if gas is None and row['sender_gas_cost_usdc'] is not None:
            gas = D(row['sender_gas_cost_usdc'])
        costs = None if gas is None else gas + a['other_cost'] + a['service_cost']
        expected = cycle(amount, q, a['conversion_rate'], a['exit_rate'], costs)
        stress_q = (q * (1 - a['haircut_bps'] / 10000) * SCALE).to_integral_value(rounding='ROUND_FLOOR') / SCALE
        stress = cycle(amount, stress_q, a['conversion_rate'], a['exit_rate'], costs)
        required = None if costs is None else (amount + costs + a['target_profit']) / (a['conversion_rate'] * a['exit_rate'])
        scenarios = []
        for pct in sorted({D(0), D(1), D(5), D(25), D(50), D(100), a['fill_pct']}):
            f = pct / 100
            # Proportional quote-rate scenario, NOT a prediction of partial fills.
            delivered = max(D(0), (q + outbound) * f - outbound) if f else D(0)
            refund = max(D(0), amount * (1 - f) - a['refund_fee']) if f < 1 else D(0)
            scenario = cycle(amount, delivered, a['conversion_rate'], a['exit_rate'], costs, refund)
            scenario.update(fill_pct=pct, filled_usdc=amount * f)
            scenarios.append(scenario)
        valid = int(row['expiry_unix']) > time.time()
        result = {
            'profile': row['profile'], 'valid': valid, 'expiry_unix': row['expiry_unix'],
            'estimated_seconds': row['estimated_seconds'], 'quantity': row['max_streaming_quantity'],
            'fees_usdp': row['fees_usdp'], 'sender_gas_usdc': gas,
            'gas_model': row['sender_gas_model'], 'expected': expected, 'stress': stress,
            'target_required_usdp': required,
            'target_required_usdp_1e8': str((required * SCALE).to_integral_value(rounding=ROUND_CEILING)) if required is not None else None,
            'target_exit_rate': (amount + costs + a['target_profit']) / (q * a['conversion_rate']) if costs is not None else None,
            'scenarios': scenarios, 'warnings': row['warnings'], 'pool_model': None,
        }
        try:
            result['pool_model'] = pool_model(row, pools, amount, a, costs)
        except Exception as exc:
            result['pool_model_error'] = f'Pool model unavailable: {exc}'
        results.append(result)
    eligible = [r for r in results if r['valid'] and r['stress']['profit_usdc'] is not None]
    best = max(eligible, key=lambda r: r['stress']['profit_usdc'], default=None)
    target = a['target_profit']
    verdict = 'unknown' if best is None else ('meets_target' if best['stress']['profit_usdc'] >= target else ('positive' if best['stress']['profit_usdc'] > 0 else 'negative'))
    by_asset = {p['asset']: p for p in pools}
    market = {}
    if USDC in by_asset and USDP in by_asset:
        u, v = by_asset[USDC], by_asset[USDP]
        market = {'usdc_depth': D(u['balance_asset']) / SCALE, 'usdp_depth': D(v['balance_asset']) / SCALE,
                  'spot_usdp_per_usdc': D(u['balance_rune']) * D(v['balance_asset']) / (D(u['balance_asset']) * D(v['balance_rune']))}
    return {'ok': True, 'amount_usdc': amount, 'observed_at_utc': report['generated_at_utc'],
            'assumptions': a, 'routes': results, 'best_profile': best['profile'] if best else None,
            'verdict': verdict, 'market': market, 'errors': report['errors'],
            'eth_price_usdc': report['eth_price_usdc_pool_implied'],
            'gas_price_gwei': report['rpc_gas_price_gwei']}


class SnapshotClient(ThorClient):
    pools = None

    def get(self, path, params=None):
        data = super().get(path, params)
        if path == 'pools':
            self.pools = data
        return data


def fetch_quotes(amount_text):
    amount = decimal(amount_text, 'USDC amount', D('0.000001'), D('100000000'))
    amount_raw(amount)
    with _cache_lock:
        for quote_id, entry in _cache.items():
            if entry['amount'] == amount and time.time() - entry['at'] < 20:
                return quote_id, entry
    if not _quote_lock.acquire(blocking=False):
        raise BlockingIOError('A live quote refresh is in progress. Try again shortly.')
    try:
        args = parser().parse_args(['--amount', str(amount), '--all-routes', '--no-fast-comparison', '--slippage-bps', '1', '--timeout', '8'])
        client = SnapshotClient(args.base_url, args.timeout)
        report = collect(args, client)
        if not report['routes']:
            raise QuoteError('; '.join(e['error'] for e in report['errors']) or 'No USDP quote available for this amount')
        entry = {'amount': amount, 'at': time.time(), 'report': report, 'pools': client.pools}
        quote_id = uuid4().hex
        with _cache_lock:
            for key in list(_cache):
                if time.time() - _cache[key]['at'] > 600:
                    del _cache[key]
            while len(_cache) >= 32:
                del _cache[next(iter(_cache))]
            _cache[quote_id] = entry
        return quote_id, entry
    finally:
        _quote_lock.release()


def serve_calculator(handler):
    """Shared routes for both the existing dashboard and standalone preview."""
    path = urlparse(handler.path).path
    assets = {'/calculator': ('templates/thor_calculator.html', 'text/html; charset=utf-8'),
              '/calculator/': ('templates/thor_calculator.html', 'text/html; charset=utf-8'),
              '/static/thor_calculator.css': ('static/thor_calculator.css', 'text/css'),
              '/static/thor_calculator.js': ('static/thor_calculator.js', 'text/javascript')}
    if handler.command == 'GET' and path in assets:
        file, content_type = assets[path]
        handler._send(200, content_type, (ROOT / file).read_bytes())
        return True
    if path not in ('/api/calculator/quotes', '/api/calculator/calculate'):
        return False
    try:
        if handler.command == 'GET' and path.endswith('/quotes'):
            params = parse_qs(urlparse(handler.path).query)
            quote_id, entry = fetch_quotes(params.get('amount', ['10000'])[0])
            result = calculate_report(entry['report'], entry['pools'], {})
            result['quote_id'] = quote_id
        elif handler.command == 'POST' and path.endswith('/calculate'):
            length = int(handler.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('Expected a JSON request smaller than 8 KB')
            body = json.loads(handler.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            with _cache_lock:
                entry = _cache.get(body.get('quote_id'))
            if entry is None or time.time() - entry['at'] > 600:
                raise ValueError('Quote expired or server restarted. Refresh live quotes.')
            result = calculate_report(entry['report'], entry['pools'], body.get('assumptions', {}))
            result['quote_id'] = body['quote_id']
        else:
            handler._json(405, {'ok': False, 'error': 'Method not allowed'})
            return True
        handler._json(200, result)
    except BlockingIOError as exc:
        handler._json(429, {'ok': False, 'error': str(exc)})
    except (ValueError, InvalidOperation, QuoteError, TypeError) as exc:
        handler._json(400, {'ok': False, 'error': str(exc)})
    except Exception:
        handler._json(502, {'ok': False, 'error': 'Live market data is unavailable. Try refreshing again.'})
    return True


class CalculatorHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, data):
        self._send(status, 'application/json', json.dumps(data, default=str, allow_nan=False).encode())

    def do_GET(self):
        if self.path == '/':
            self.path = '/calculator'
        if not serve_calculator(self):
            self._json(404, {'ok': False, 'error': 'Not found'})

    def do_POST(self):
        if not serve_calculator(self):
            self._json(404, {'ok': False, 'error': 'Not found'})


def run(host='127.0.0.1', port=8081):
    print(f'USDP arbitrage calculator: http://{host}:{port}/calculator', flush=True)
    ThreadingHTTPServer((host, port), CalculatorHandler).serve_forever()
