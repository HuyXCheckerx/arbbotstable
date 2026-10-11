import time
import hmac
import base64
import hashlib
import requests
from urllib.parse import urlencode

# ===== OKX API credentials =====
API_KEY = "afbd1cbb-fb82-4c24-9b50-c28823cd8001"
SECRET_KEY = "E41ADC9119A47896A8ABE0E752983B31"
PASSPHRASE = "Huypro159!"
PROJECT_ID = "4506f56deeba9843699dd86ea3253f5a"
# ===== Quote settings =====
BASE_URL = "https://www.okx.com"
REQUEST_PATH = "/api/v6/dex/aggregator/quote"
METHOD = "GET"

# Solana settings: chainIndex = "501"
params = {
    "chainIndex": "501",  # 501 for Solana (1 for Ethereum)
    "fromTokenAddress": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT on Solana
    "toTokenAddress": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",    # USDC on Solana
    "amount": "100000000",  # 100 USDT, 6 decimals
    "slippage": "0"
}

def get_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

def sign(timestamp, method, request_path, query_string, body=""):
    message = timestamp + method.upper() + request_path
    if query_string:
        message += "?" + query_string
    message += body

    mac = hmac.new(
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        digestmod=hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

query_string = urlencode(params)
timestamp = get_timestamp()
signature = sign(timestamp, METHOD, REQUEST_PATH, query_string)

headers = {
    "OK-ACCESS-KEY": API_KEY,
    "OK-ACCESS-SIGN": signature,
    "OK-ACCESS-TIMESTAMP": timestamp,
    "OK-ACCESS-PASSPHRASE": PASSPHRASE,
    "OK-ACCESS-PROJECT": PROJECT_ID,
    "Content-Type": "application/json"
}

url = f"{BASE_URL}{REQUEST_PATH}?{query_string}"

response = requests.get(url, headers=headers, timeout=20)
data = response.json()

print("Input:")
print("  From token: USDT")
print("  To token: USDC")
print("  Amount: 100 USDT")
print("  Slippage: 0")

print("\nRaw response:")
print(data)

try:
    quote = data["data"][0]
    output_amount = quote.get("toTokenAmount")

    print("\nOutput:")
    print("  Estimated USDC amount (base units):", output_amount)
    if output_amount:
        print(f"  Estimated USDC amount: {int(output_amount) / 1e6:.6f} USDC")
except (KeyError, IndexError, TypeError):
    print("\nOutput:")
    print("  Could not parse output amount from response.")