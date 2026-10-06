#!/usr/bin/env python3
"""Test DirectAggregatorClient with proxy on VPS."""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass

import requests
from src.engines.direct_aggregators import DirectAggregatorClient
from src.engines.eth_flash_arb_pyusd_usdc import (
    ArbError, MatchaQuote, ProviderRateLimitedError, RetryableArbError,
    is_address, is_hex_data, parse_integer
)

proxy_url = os.getenv("MATCHA_PROXY", "http://COlBMQ:bCYTai@14.224.225.135:45376").strip()
session = requests.Session()
session.trust_env = False
if proxy_url:
    session.proxies = {"http": proxy_url, "https": proxy_url}
session.headers.update({"accept": "application/json", "user-agent": "Mozilla/5.0"})

client = DirectAggregatorClient(
    executor="0x50da32e628b45abb1335924086ca0013b9d4ec1c",
    operator=None,
    session=session,
    api_modules={
        "ArbError": ArbError,
        "MatchaQuote": MatchaQuote,
        "ProviderRateLimitedError": ProviderRateLimitedError,
        "RetryableArbError": RetryableArbError,
        "is_address": is_address,
        "is_hex_data": is_hex_data,
        "parse_integer": parse_integer,
    }
)

pyusd = "0x6c3ea9036406852006290770bedfcaba0e23a0e8"
usdg = "0xe343167631d89b6ffc58b88d6b7fb0228795491d"
amt = 100_000_000  # 100 PYUSD

print(f"Fetching quotes for 100 PYUSD -> USDG via DirectAggregatorClient...")
try:
    quotes = client.quotes(amt, 50, sell_token_address=pyusd, buy_token_address=usdg)
    print(f"[SUCCESS] Got {len(quotes)} quote(s):")
    for q in quotes:
        print(f"  Aggregator: {q.aggregator} | Buy Amount: {q.buy_amount / 1e6:.4f} USDG | Gas: {q.gas}")
        print(f"  Target: {q.target} | Calldata len: {len(q.data) if q.data else 0}")
except Exception as e:
    print(f"[FAIL] Error: {e}")
    if hasattr(client, "last_errors"):
        print("Last errors:", client.last_errors)
