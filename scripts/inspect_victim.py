import urllib.request
import json

url = 'https://rpc.mainnet.chain.robinhood.com'
headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}

def rpc(method, params):
    req = urllib.request.Request(url, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())['result']

tx_hashes = [
    '0x5ab579129af3ea898d46bcf44ab5fb2ef83bd8a6d8fde999bf42c3849ce58c44',
    '0x5cd499ff21162a818f25a713848bc77e9e21528d28adad2af0dbc29952e18b08',
    '0x774c898464548276b06b5027106e66ebe2d15d18d4d53c3856237d7ae44fa4f3'
]

for h in tx_hashes:
    tx = rpc('eth_getTransactionByHash', [h])
    rec = rpc('eth_getTransactionReceipt', [h])
    print(f"\n=== TX {h} ===")
    print("From:", tx['from'], "To:", tx['to'], "Value:", int(tx['value'], 16), "GasPrice:", int(tx['gasPrice'], 16)/1e9)
    print("Input:", tx['input'][:138])
    for log in rec.get('logs', []):
        addr = log['address']
        topics = log.get('topics', [])
        if topics and topics[0] == '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef':
            from_a = '0x' + topics[1][-40:]
            to_a = '0x' + topics[2][-40:]
            val = int(log['data'], 16)
            print(f"  Transfer on {addr}: {from_a} -> {to_a} val={val}")
        elif topics and topics[0] == '0xc42079f94a6350d7e6235f29174924f9d5fb2ce00241a9c81be738d4506636c4':
            print(f"  Swap on {addr}: {topics}")
