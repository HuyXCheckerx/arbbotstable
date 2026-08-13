import os
import sys
import base64
import time
import requests
import json
import base58
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed

# Logger redirection
class Logger(object):
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

os.makedirs("logs", exist_ok=True)
sys.stdout = Logger("logs/fire_v2_order.log")
sys.stderr = sys.stdout

load_dotenv()

# Config
PRIVATE_KEY = os.environ["SOLANA_PRIVATE_KEY"]
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUP_API_KEY = os.environ.get("JUP_API_KEY", "")

headers = {}
if JUP_API_KEY:
    headers["x-api-key"] = JUP_API_KEY

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
AMOUNT_RAW = 300 * 10**6  # 1000 USDC
DECIMALS = 6

def get_keypair():
    try:
        keypair_data = json.loads(PRIVATE_KEY)
        return Keypair.from_bytes(bytes(keypair_data))
    except Exception:
        return Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))

def format_route_plan(route_plan):
    if not route_plan:
        return "N/A"
    parts = []
    for info in route_plan:
        swap_info = info.get("swapInfo", {})
        label = swap_info.get("label", "Unknown DEX")
        in_amt = int(swap_info.get("inAmount", 0)) / 10**DECIMALS
        out_amt = int(swap_info.get("outAmount", 0)) / 10**DECIMALS
        parts.append(f"{label} ({in_amt:.2f} -> {out_amt:.2f})")
    return " -> ".join(parts)

def main():
    print("=" * 60)
    print("         JUPITER SWAP V2 SCRIPT (1000 USDC -> USDT)")
    print("=" * 60)
    
    keypair = get_keypair()
    user_pubkey = str(keypair.pubkey())
    print(f"[*] Wallet Public Key: {user_pubkey}")
    
    # 1. Fetch Quote from V2 Quote
    quote_url = "https://api.jup.ag/swap/v2/quote"
    quote_params = {
        "inputMint": USDC_MINT,
        "outputMint": USDT_MINT,
        "amount": str(AMOUNT_RAW),
        "slippageBps": "0"
    }
    
    print("[*] Fetching quote from Jupiter Swap V2...")
    try:
        quote_resp = requests.get(quote_url, params=quote_params, headers=headers, timeout=10)
        if quote_resp.status_code != 200:
            print(f"[!] Jupiter Quote API error ({quote_resp.status_code}): {quote_resp.text}")
            return
        quote_data = quote_resp.json()
    except Exception as e:
        print(f"[!] HTTP Quote error: {e}")
        return
        
    out_amount = int(quote_data.get("outAmount", 0)) / 10**DECIMALS
    route_plan = format_route_plan(quote_data.get("routePlan", []))
    price_impact = quote_data.get("priceImpactPct", "0.0")
    
    print("\n" + "=" * 60)
    print("                      QUOTE DETAILS")
    print("=" * 60)
    print(f"  Input Amount:  1000.000000 USDC")
    print(f"  Expected Out:  {out_amount:.6f} USDT")
    print(f"  Price Impact:  {price_impact}%")
    print(f"  Route Hops:    {route_plan}")
    print("=" * 60)
    
    # 2. Build Transaction via V2 Swap POST
    swap_url = "https://api.jup.ag/swap/v2/swap"
    swap_payload = {
        "quoteResponse": quote_data,
        "taker": user_pubkey,
        "wrapAndUnwrapSol": False
    }
    
    print("[*] Building swap transaction from Jupiter Swap V2...")
    try:
        swap_resp = requests.post(swap_url, json=swap_payload, headers=headers, timeout=10)
        if swap_resp.status_code != 200:
            print(f"[!] Jupiter Swap API error ({swap_resp.status_code}): {swap_resp.text}")
            return
        swap_data = swap_resp.json()
    except Exception as e:
        print(f"[!] HTTP Swap error: {e}")
        return
        
    tx_b64 = swap_data.get("swapTransaction", "")
    if not tx_b64:
        print("[!] No transaction base64 returned from Jupiter.")
        return
        
    print(f"  Estimated fees: Prioritization: {swap_data.get('prioritizationFeeLamports', 0)} lamports")
    print(f"                  Compute Unit Price: {swap_data.get('computeUnitPrice', 0)}")
    print("=" * 60)
    
    # Prompt user on stderr/terminal to bypass redirect
    try:
        sys.__stdout__.write("\n[Action Required] Press Enter to sign and submit this swap to Solana (or Ctrl+C to exit)...")
        sys.__stdout__.flush()
        input()
    except KeyboardInterrupt:
        print("\n[!] Swap aborted by user.")
        return
        
    print("[*] Decoding and signing transaction...")
    try:
        raw_tx = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        
        # Sign transaction with our wallet Keypair
        signed_tx = VersionedTransaction(tx.message, [keypair])
    except Exception as e:
        print(f"[!] Signing failed: {e}")
        return
        
    print("[*] Connecting to RPC and submitting transaction...")
    try:
        client = Client(RPC_URL, commitment=Confirmed)
        resp = client.send_transaction(signed_tx)
        tx_sig = resp.value
        print(f"[+] Transaction sent! Signature: {tx_sig}")
        print(f"[*] Explorer link: https://solscan.io/tx/{tx_sig}")
        
        print("[*] Waiting for confirmation (timeout 60s)...")
        confirmed = False
        for i in range(30):
            time.sleep(2)
            try:
                sig_status = client.get_signature_statuses([tx_sig])
                status = sig_status.value[0]
                if status is not None:
                    if status.err is not None:
                        print(f"[!] Transaction execution failed: {status.err}")
                        break
                    if status.confirmation_status in ["confirmed", "finalized"]:
                        print(f"[+] Transaction successfully confirmed!")
                        confirmed = True
                        break
            except Exception as e:
                pass
        
        if not confirmed:
            print("[!] Confirmation timed out or failed. Check explorer link above.")
            
    except Exception as e:
        print(f"[!] RPC submission error: {e}")

if __name__ == "__main__":
    main()
