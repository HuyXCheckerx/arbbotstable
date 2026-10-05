import os
import sys
import json
import ssl
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 1. Test Ethereum Direct Quoting (Velora / Paraswap)
print("=== [1/2] Testing Ethereum Direct Quote (Velora) ===")
try:
    from src.engines.eth_flash_arb_pyusd_usdc import MatchaClient, HttpJsonClient, PYUSD, USDG
    client = HttpJsonClient(timeout=10.0, user_agent="Mozilla/5.0")
    mc = MatchaClient(client, quote_provider="direct")
    executor = "0x50da32e628b45abb1335924086ca0013b9d4ec1c"
    sell_amt = 100_000_000 # 100 tokens (6 decimals)
    quotes = mc.quotes(executor, sell_amt, 50, ["velora"], PYUSD, USDG)
    for agg, q in quotes:
        print(f"  [SUCCESS] {agg.upper()}: PYUSD -> USDG | in={sell_amt/1e6:.2f}, out={q.buy_amount/1e6:.4f}, target={q.target}")
except Exception as e:
    print(f"  [ERROR] Ethereum direct quote failed: {e}")

# 2. Test Solana DFlow Quoting (using certifi or SSL context)
print("\n=== [2/2] Testing Solana DFlow Quote ===")
try:
    import urllib.request
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl._create_unverified_context()

    PYUSD_MINT = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
    USDG_MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    
    url = f"https://dev-quote-api.dflow.net/quote?inputMint={PYUSD_MINT}&outputMint={USDG_MINT}&amount=100000000&slippageBps=0"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8, context=ssl_ctx) as resp:
        data = json.loads(resp.read().decode())
        out_amt = int(data.get("outAmount", 0))
        steps = len(data.get("routePlan", []))
        print(f"  [SUCCESS] DFLOW: PYUSD -> USDG | in=100.00, out={out_amt/1e6:.4f}, route_steps={steps}")

    url2 = f"https://dev-quote-api.dflow.net/quote?inputMint={USDC_MINT}&outputMint={USDG_MINT}&amount=100000000&slippageBps=0"
    req2 = urllib.request.Request(url2, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req2, timeout=8, context=ssl_ctx) as resp:
        data2 = json.loads(resp.read().decode())
        out_amt2 = int(data2.get("outAmount", 0))
        steps2 = len(data2.get("routePlan", []))
        print(f"  [SUCCESS] DFLOW: USDC -> USDG | in=100.00, out={out_amt2/1e6:.4f}, route_steps={steps2}")
except Exception as e:
    print(f"  [ERROR] Solana DFlow quote failed: {e}")
