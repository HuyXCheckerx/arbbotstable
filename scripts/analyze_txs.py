import urllib.request
import json

url = 'https://rpc.mainnet.chain.robinhood.com'
headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}

def rpc(method, params):
    req = urllib.request.Request(url, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())['result']

def analyze_receipt(tx_hash, name):
    rec = rpc('eth_getTransactionReceipt', [tx_hash])
    print(f"\n==================== {name}: {tx_hash} ====================")
    print(f"Status: {rec.get('status')} | Block: {int(rec['blockNumber'], 16)} | TxIndex: {int(rec['transactionIndex'], 16)} | GasUsed: {int(rec['gasUsed'], 16)}")
    logs = rec.get('logs', [])
    print(f"Total logs: {len(logs)}")
    for i, log in enumerate(logs):
        topics = log.get('topics', [])
        addr = log.get('address')
        data = log.get('data')
        # Check Transfer event: keccak256("Transfer(address,address,uint256)")
        if topics and topics[0] == '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef':
            from_a = '0x' + topics[1][-40:]
            to_a = '0x' + topics[2][-40:]
            val = int(data, 16)
            print(f"  [Log {i}] Transfer on {addr}: from={from_a} to={to_a} val={val}")
        # Swap event check: Uniswap V2 Swap(address,uint256,uint256,uint256,uint256,address) or V3
        elif topics and topics[0] == '0xd78ad11e23be8074c8629c65b9205f53a282d84ab6aab90bb770dceaa7a49410':
            print(f"  [Log {i}] Uniswap V2 Swap on {addr}: topics={topics}")
        elif topics and topics[0] == '0xc42079f94a6350d7e6235f29174924f9d5fb2ce00241a9c81be738d4506636c4':
            print(f"  [Log {i}] Uniswap V3 Swap on {addr}: topics={topics}")
        else:
            print(f"  [Log {i}] Event on {addr}: topic0={topics[0] if topics else 'None'}")

def main():
    analyze_receipt('0x76dc71f8bcfa07853a8a09fdc9edab67dd5f79e2277bd55332040c97ddbb7a89', 'FRONTRUN')
    analyze_receipt('0x62fc9a56a0927220f00e665d468c6833b74b6b363c82dc79a03c2ae48c26d657', 'BACKRUN')

if __name__ == '__main__':
    main()
