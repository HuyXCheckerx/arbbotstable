import urllib.request
import json

# 1. Stable.com live status
stable_url = 'https://api-defi.stable.com/swap/status'
headers = {
    'content-type': 'application/json',
    'origin': 'https://stable.com',
    'referer': 'https://stable.com/',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
}
payload = {'chainFrom':'102','assetFrom':'PYUSD','chainTo':'102','assetTo':'USDG','gasLess':False,'amountFrom':'1000','addressFrom':'G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he','addressTo':'G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he'}
res_stable = json.loads(urllib.request.urlopen(urllib.request.Request(stable_url, data=json.dumps(payload).encode('utf-8'), headers=headers)).read())
asset = res_stable.get('asset', res_stable)

print('=== 1. STABLE.COM LIVE RESERVE CAPACITY ===')
print('Asset:       ', asset.get('asset'))
print('Pool Balance:', f"{float(asset.get('balance', 0)):,.2f} USDG")
print('Min Order:   ', f"{float(asset.get('min', 0)):,.2f} USDG")
print('Max Order:   ', f"{float(asset.get('max', 0)):,.2f} USDG")

# 2. Depth across loan sizes
USDG = '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
PYUSD = '2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo'

sizes = [1000, 5000, 10000, 20000, 30000, 50000, 70000]

print('\n=== 2. DIRECT 2-HOP ARB PROFIT vs SIZING ===')
print(f'| Trade Size | Leg 1 (USDG->USDC) | Leg 2 (USDC->PYUSD) | Net Realized Profit | Net Spread (bps) |')
print(f'| :--- | :--- | :--- | :--- | :--- |')

for s in sizes:
    raw = s * 1000000
    try:
        r1 = json.loads(urllib.request.urlopen(f'https://lite-api.jup.ag/swap/v1/quote?inputMint={USDG}&outputMint={USDC}&amount={raw}&slippageBps=2').read())
        out1 = int(r1['outAmount'])
        r2 = json.loads(urllib.request.urlopen(f'https://lite-api.jup.ag/swap/v1/quote?inputMint={USDC}&outputMint={PYUSD}&amount={out1}&slippageBps=2').read())
        out2 = int(r2['outAmount']) / 1e6
        profit = out2 - s
        bps = (profit / s) * 10000
        print(f'| **${s:,.0f}** | {out1/1e6:,.2f} USDC | {out2:,.2f} PYUSD | **{profit:+,.4f} PYUSD** | **{bps:+.2f} bps** |')
    except Exception as e:
        print(f'| **${s:,.0f}** | Error: {e} |')
