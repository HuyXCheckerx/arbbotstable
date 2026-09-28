import sys
import json
sys.path.insert(0, 'src/engines')
from matcha_browser_bridge import fetch_bridge_quotes

def test_taker(name, taker):
    p = {
        'chainId': 1399811149,
        'sellTokenAddress': '2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo',
        'buyTokenAddress': '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH',
        'sellAmount': '100000000000',
        'sellTokenDecimals': 6,
        'buyTokenDecimals': 6,
        'slippageBps': 0,
        'taker': taker,
    }
    try:
        q = fetch_bridge_quotes('solana', p, ['0x', 'DFlow', 'Jupiter', 'OKX'])
        print(f"{name} ({taker[:8]}...): SUCCESS!")
        for k, v in q.items():
            if isinstance(v, dict):
                tx = v.get("transaction", "")
                print(f"   {k}: outAmount={v.get('outAmount')} txLen={len(tx) if tx else 0} error={v.get('error')}")
            else:
                print(f"   {k}: {v}")
    except Exception as e:
        print(f"{name} ({taker[:8]}...): FAILED: {e}")

if __name__ == '__main__':
    print("Testing dummy_ones...")
    test_taker("dummy_ones", "11111111111111111111111111111111")
    print("\nTesting user_wallet...")
    test_taker("user_wallet", "G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he")
