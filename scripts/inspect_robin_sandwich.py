import urllib.request
import json
import time

url = 'https://rpc.mainnet.chain.robinhood.com'
headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}

def rpc(method, params):
    req = urllib.request.Request(url, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())['result']

def main():
    for b in [72248287, 72248288, 72248289, 72248290, 72248291]:
        blk = rpc('eth_getBlockByNumber', [hex(b), True])
        txs = blk.get('transactions', [])
        ts = int(blk.get('timestamp', '0x0'), 16)
        print(f"=== Block {b} | TS: {ts} | txs: {len(txs)} ===")
        for idx, tx in enumerate(txs):
            h = tx['hash']
            f = tx['from']
            t = tx['to']
            gp = int(tx.get('gasPrice', '0x0'), 16)
            tip = int(tx.get('maxPriorityFeePerGas', '0x0'), 16)
            nonce = int(tx.get('nonce', '0x0'), 16)
            inp = tx.get('input', '')[:10]
            print(f"  [{idx}] nonce={nonce} {h[:14]}.. from={f[:10]}.. to={str(t)[:10]}.. gp={gp/1e9:.3f}gwei tip={tip/1e9:.3f} selector={inp}")

if __name__ == '__main__':
    main()
