import urllib.request
import json

url = 'https://rpc.mainnet.chain.robinhood.com'
headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}

def rpc(method, params):
    req = urllib.request.Request(url, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())['result']

def main():
    target = '0x68a04a63fd1d8eabf167ef48ed0a0ef06c2374d9'
    code = rpc('eth_getCode', [target, 'latest'])
    print(f"Contract {target} code length: {len(code)} chars ({(len(code)-2)//2} bytes)")
    
    # Check if there is source code on RobinScan API or web
    # Let's inspect owner or storage slots of this contract
    for slot in range(5):
        val = rpc('eth_getStorageAt', [target, hex(slot), 'latest'])
        print(f"  Slot {slot}: {val}")

if __name__ == '__main__':
    main()
