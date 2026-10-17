from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.engines.multichain_engine import MultichainEngine, CHAINS
import traceback

engine = MultichainEngine()
for key in ("bsc", "polygon"):
    config = CHAINS[key]
    print(f"Testing {config.name}...")
    try:
        dq = engine.dex.get_paraswap_quote(config, config.usdc, config.usdt, 50000 * 10**config.decimals, 2, "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C")
        print("  DEX quote buyAmount:", dq.buy_amount if dq else None)
    except Exception as e:
        print("  DEX error:")
        traceback.print_exc()

    # Direct test of api.paraswap.io on VPS
    try:
        import urllib.request, urllib.parse, json
        params = {
            "srcToken": config.usdc.lower(), "destToken": config.usdt.lower(),
            "amount": str(50000 * 10**config.decimals), "srcDecimals": config.decimals,
            "destDecimals": config.decimals, "side": "SELL", "network": config.chain_id,
        }
        url = "https://api.paraswap.io/prices?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "arbbotstable/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            print("  Direct ParaSwap destAmount:", data["priceRoute"]["destAmount"])
    except Exception as e:
        print("  Direct ParaSwap error:", e)
