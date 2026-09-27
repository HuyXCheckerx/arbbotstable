import urllib.request
import json

url = 'https://rpc.mainnet.chain.robinhood.com'
headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}

def rpc(method, params):
    req = urllib.request.Request(url, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())['result']

def main():
    target_token = '0xa67a3eebf0ae8a935848bb47993b9a6d68751a23'.lower()
    for b in [72248288, 72248289, 72248290]:
        blk = rpc('eth_getBlockByNumber', [hex(b), True])
        for idx, tx in enumerate(blk['transactions']):
            h = tx['hash']
            rec = rpc('eth_getTransactionReceipt', [h])
            for log in rec.get('logs', []):
                if log.get('address', '').lower() == target_token:
                    f = tx['from']
                    t = tx['to']
                    print(f"Block {b} Tx [{idx}] {h} from={f} to={t}")
                    for top in log.get('topics', []):
                        pass
                    print(f"  Receipt status: {rec.get('status')} gasUsed={int(rec.get('gasUsed','0x0'), 16)}")

if __name__ == '__main__':
    main()
