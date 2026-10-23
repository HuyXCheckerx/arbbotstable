"""Replay saved public data only. No RPC connections, processes, or signing."""
from dataclasses import asdict
from decimal import Decimal as D
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.engines import thorchain_usdp_sniper as e


def replay(folder):
    folder = Path(folder)
    def data(name):
        return json.loads((folder / (name+'.json')).read_text())['data']
    client = e.ThorClient()
    pools_file = 'pools' if (folder / 'pools.json').exists() and folder.name == 'latest' else 'pinned-pools'
    with patch.object(client, 'get', return_value=data(pools_file)):
        pools = e.fetch_pool_spot_rate(client)
    fees = data('outbound-fees' if folder.name == 'latest' else 'outbound_fees')
    refund = D(next(row['outbound_fee'] for row in fees if row['asset'] == e.USDC_ASSET))/10**8
    dynamic = data('dynamic-floors' if folder.name == 'latest' else 'dynamic_l1_fees-symbiosis')
    by_asset = {asset: int(row['dynamic_bps']) for row in dynamic['pairs']
                for asset in row['pair'].split('|') if asset != 'THOR.RUNE'}
    floors = (by_asset[e.USDC_ASSET], by_asset[e.USDP_ASSET])
    result = {'pools': asdict(pools), 'fee_floors': floors, 'refund_fee': refund,
              'gas_assumption_usd': '.38', 'amount_usdc': '99964', 'modes': {}}
    for mode in ('rapid', 'resting'):
        record = json.loads((folder / ('quote-'+mode+'.json')).read_text())
        quote = e.parse_quote(record['data'], D(99964))
        quote.fee_floors = floors
        quantity = e.memo_stream_fields(quote.memo)[2]
        plan_args = (pools, quote, D(99964), D('1.0002'), '2bps', 2, 1, refund, D('.38'))
        exact, best = e.plan_for_quote(*plan_args, exact=True), e.plan_for_quote(*plan_args)
        capacity = None
        best_net = None
        deposit, target = 9996400000000, 9998399280000
        outbound = int(quote.raw['fees']['outbound'])
        for count in range(1, int(quote.raw['max_streaming_quantity'])+1):
            filled, emit = e.simulate_streaming_fill(pools, deposit, count, target, floors, 1, outbound)
            net = (D(emit-outbound-filled)/10**8-refund-D('.38')) if filled else -refund-D('.38')
            candidate = {'quantity': count, 'filled_usdc': D(filled)/10**8,
                         'delivered_usdp': max(D(0), D(emit-outbound)/10**8) if filled else D(0),
                         'net_profit_usd': net}
            if capacity is None or candidate['filled_usdc'] > capacity['filled_usdc']:
                capacity = candidate
            if filled and (best_net is None or net > best_net['net_profit_usd']):
                best_net = candidate
        result['modes'][mode] = {
            'height': record['headers'].get('Grpc-Metadata-X-Cosmos-Block-Height'),
            'fetched_at': record.get('fetched_at'), 'quote_expected_usdp': quote.expected_usdp,
            'quantity': quantity, 'outbound_fee_usdp': quote.fees_usdp['outbound'],
            'requested_deposit_plan': asdict(exact), 'best_tested_deposit': asdict(best),
            'maximum_fill_across_allowed_quantities': capacity, 'best_net_quantity': best_net,
            'first_slice_required_spot': e.slice_fill_rate(pools, quote, D('1.0002'), '2bps', 2, 1)}
    serialized = json.dumps(result, indent=2, default=str)
    (folder / 'simulation-corrected.json').write_text(serialized+'\n')
    print(serialized)


if __name__ == '__main__':
    replay(Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).parent / 'latest')
