import os
import time
import base64
import requests
import json
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed

load_dotenv()
PRIVATE_KEY = os.environ["SOLANA_PRIVATE_KEY"]
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUP_API = "https://lite-api.jup.ag/swap/v1"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDG_MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"

TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
PRIORITY_FEE = 10000
JITO_TIP = 10000
DECIMALS = 6

def get_ata(owner, mint, token_program):
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )[0]

def main():
    client = Client(RPC_URL, commitment=Confirmed)
    session = requests.Session()
    
    try:
        keypair = Keypair.from_base58_string(PRIVATE_KEY)
    except:
        keypair = Keypair.from_bytes(bytes(json.loads(PRIVATE_KEY)))

    wallet = keypair.pubkey()
    usdg_ata = get_ata(wallet, Pubkey.from_string(USDG_MINT), TOKEN_2022_PROGRAM)

    attempts = 0
    while True:
        attempts += 1
        resp = client.get_token_account_balance(usdg_ata)
        if not resp.value:
            print("[+] USDG balance is 0. Done.")
            break
            
        usdg_bal_raw = int(resp.value.amount)
        if usdg_bal_raw == 0:
            print("[+] USDG balance is 0. Done.")
            break
            
        usdg_human = usdg_bal_raw / 10**DECIMALS
        
        # Quote
        q_resp = session.get(f"{JUP_API}/quote", params={
            "inputMint": USDG_MINT,
            "outputMint": USDC_MINT,
            "amount": str(usdg_bal_raw),
            "slippageBps": "0"
        })
        if q_resp.status_code != 200:
            print("[!] Quote failed, retrying...")
            time.sleep(1)
            continue
            
        quote = q_resp.json()
        out_raw = int(quote["outAmount"])
        out_human = out_raw / 10**DECIMALS
        
        if out_human <= usdg_human:
            if attempts > 10:
                print("[!] Attempts exceeded 10. Discarding loop and restarting...")
                attempts = 0
                time.sleep(2)
                continue
            print(f"[-] Attempt {attempts}: Unprofitable ({usdg_human:.6f} -> {out_human:.6f}). Waiting...")
            time.sleep(2)
            continue
            
        print(f"[*] Attempt {attempts}: Firing Jup swap for {out_human:.6f} USDC (0% slippage)...")
        
        s_resp = session.post(f"{JUP_API}/swap", json={
            "quoteResponse": quote,
            "userPublicKey": str(wallet),
            "dynamicComputeUnitLimit": True,
            "dynamicSlippage": False,
            "wrapAndUnwrapSol": False,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": PRIORITY_FEE,
                    "priorityLevel": "medium"
                }
            },
            "jitoTipLamports": JITO_TIP
        })
        
        if s_resp.status_code == 200:
            tx_b64 = s_resp.json().get("swapTransaction", "")
            if tx_b64:
                tx_bytes = base64.b64decode(tx_b64)
                tx = VersionedTransaction.from_bytes(tx_bytes)
                recent_blockhash = client.get_latest_blockhash().value.blockhash
                signed_tx = VersionedTransaction(tx.message, [keypair])
                
                res = client.send_transaction(signed_tx)
                if res.value:
                    print(f"[+] Sent: {res.value}")
                    client.confirm_transaction(res.value, commitment=Confirmed)
                    print("[+] Confirmed! Successfully rescued USDG back to USDC.")
                    break
        
        print("[!] Swap failed (likely slippage). Retrying immediately...")
        time.sleep(0.5)

if __name__ == "__main__":
    main()
