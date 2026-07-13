import os
import json
import time
import struct
import hashlib
import uuid
import sys
import glob

# Fix for Pterodactyl / Cloud hosts not mapping .local packages correctly
local_paths = glob.glob("/home/container/.local/lib/python*/site-packages")
if local_paths:
    sys.path.extend(local_paths)

import base64
import requests
import asyncio
import websockets
import threading
from dotenv import load_dotenv
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.instruction import Instruction, AccountMeta, CompiledInstruction
from solders.transaction import VersionedTransaction
from solders.message import Message, MessageV0
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from state_store import BotStateStore
from sizing import (
    calculate_refill_aware_min_size,
    calculate_quote_metrics,
    capital_efficiency_key,
    generate_candidate_sizes,
    generate_refinement_sizes,
    is_profitable_candidate,
)

load_dotenv()

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
log_filename = f"logs/{os.path.splitext(os.path.basename(__file__))[0]}.log"
sys.stdout = Logger(log_filename)
sys.stderr = sys.stdout

# ============================================================
# CONFIG
# ============================================================
PRIVATE_KEY = os.environ["SOLANA_PRIVATE_KEY"]
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUP_API_KEY = os.environ.get("JUP_API_KEY", "")
MIN_TRADE_SIZE_USD = float(os.environ.get("MIN_TRADE_SIZE_USD", "1000"))
MIN_NET_PROFIT_USD = float(os.environ.get("MIN_NET_PROFIT_USD", "0.10"))
MIN_NET_RETURN_BPS = float(os.environ.get("MIN_NET_RETURN_BPS", "0"))
USDG_REFILL_THRESHOLD_USD = float(os.environ.get("USDG_REFILL_THRESHOLD_USD", "2000"))
USDG_REFILL_BUFFER_USD = float(os.environ.get("USDG_REFILL_BUFFER_USD", "1"))
DEFAULT_EXECUTION_COST_USD = float(os.environ.get("DEFAULT_EXECUTION_COST_USD", "0.005"))
EXECUTION_COST_SAFETY_MULTIPLIER = float(os.environ.get("EXECUTION_COST_SAFETY_MULTIPLIER", "1.25"))
QUOTE_SAMPLE_DELAY_SECONDS = float(os.environ.get("QUOTE_SAMPLE_DELAY_SECONDS", "0.15"))

api_keys_str = os.environ.get("JUP_API_KEYS", "")
if api_keys_str:
    JUP_API_KEYS = [k.strip() for k in api_keys_str.split(",") if k.strip()]
else:
    JUP_API_KEYS = [JUP_API_KEY] if JUP_API_KEY else []

jup_key_index = 0
def get_jup_headers():
    global jup_key_index
    if not JUP_API_KEYS:
        return {}
    key = JUP_API_KEYS[jup_key_index % len(JUP_API_KEYS)]
    jup_key_index += 1
    return {"x-api-key": key}

PRIORITY_FEE = 10000  # 10k lamports
JITO_TIP = 0          # Disabled to keep total fees strictly < 30k lamports

# ============================================================
# CONSTANTS
# ============================================================
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDG_MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
PYUSD_MINT = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
SOL_MINT = "So11111111111111111111111111111111111111112"

USDC_MINT_PK = Pubkey.from_string(USDC_MINT)
USDG_MINT_PK = Pubkey.from_string(USDG_MINT)
PYUSD_MINT_PK = Pubkey.from_string(PYUSD_MINT)

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

STABLE_PROGRAM_ID = Pubkey.from_string("2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG")
STABLE_CHAIN_ID = 102
STABLE_API = "https://api-defi.stable.com"
JUP_API = "https://api.jup.ag/swap/v2"

MAIN_STATE_SEED = b"main_state"
POOL_SEED = b"pool"
NONCE_SEED = b"nonce"
NATIVE_FEE_SEED = b"native_fee"
SINGLE_CHAIN_SWAP_DISC = hashlib.sha256(b"global:single_chain_swap").digest()[:8]

DECIMALS = 6

# ============================================================
# WEBSOCKET & BALANCE MONITORING HELPERS
# ============================================================
def parse_token_balance(data_b64):
    try:
        data = base64.b64decode(data_b64)
        if len(data) >= 72:
            return struct.unpack("<Q", data[64:72])[0]
    except Exception as e:
        print(f"[!] Error parsing balance: {e}")
    return 0

class BalanceMonitor:
    def __init__(self, rpc_url, accounts_to_sub):
        self.rpc_url = rpc_url
        self.accounts_to_sub = accounts_to_sub
        self.balances = {k: 0 for k in accounts_to_sub.keys()}
        self.update_event = threading.Event()
        self.thread = threading.Thread(target=self._start_loop, daemon=True)
        
    def _start_loop(self):
        asyncio.run(self._ws_loop())
        
    async def _ws_loop(self):
        ws_url = self.rpc_url.replace("https://", "wss://").replace("http://", "ws://")
        while True:
            try:
                async with websockets.connect(ws_url) as websocket:
                    id_to_key = {}
                    sub_to_key = {}
                    id_counter = 1
                    for key, pubkey_str in self.accounts_to_sub.items():
                        req = {
                            "jsonrpc": "2.0",
                            "id": id_counter,
                            "method": "accountSubscribe",
                            "params": [
                                pubkey_str,
                                {
                                    "encoding": "base64",
                                    "commitment": "confirmed"
                                }
                            ]
                        }
                        id_to_key[id_counter] = key
                        print(f"[*] Subscribing to {key}: {pubkey_str} with req_id {id_counter}")
                        await websocket.send(json.dumps(req))
                        id_counter += 1
                    
                    async for message in websocket:
                        data = json.loads(message)
                        if "result" in data and "id" in data:
                            req_id = data["id"]
                            sub_id = data["result"]
                            key = id_to_key.get(req_id)
                            if key:
                                sub_to_key[sub_id] = key
                                print(f"[+] Subscription confirmed: req_id {req_id} -> sub_id {sub_id} ({key})")
                        elif "method" in data and data["method"] == "accountNotification":
                            sub_id = data["params"]["subscription"]
                            key = sub_to_key.get(sub_id)
                            if key:
                                val = data["params"]["result"]["value"]
                                if val is not None:
                                    data_b64 = val["data"][0]
                                    amount_raw = parse_token_balance(data_b64)
                                    print(f"[~] WS Update: {key} -> {amount_raw / 10**DECIMALS:.6f} tokens (raw: {amount_raw})")
                                    self.balances[key] = amount_raw
                                    self.update_event.set()
            except Exception:
                await asyncio.sleep(5)
                
    def start(self):
        self.thread.start()

# ============================================================
# UTILS
# ============================================================
def find_pda(seeds, program_id=STABLE_PROGRAM_ID):
    return Pubkey.find_program_address(seeds, program_id)

def get_ata(owner, mint, token_program=TOKEN_PROGRAM):
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )[0]

def get_token_balance(client, ata):
    resp = client.get_token_account_balance(ata)
    if resp.value is None:
        return 0
    return int(resp.value.amount)

def print_portfolio(session, client, wallet, usdc_ata, usdg_ata, pyusd_ata, label="", prev=None):
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"

    usdc_raw = get_token_balance(client, usdc_ata)
    usdg_raw = get_token_balance(client, usdg_ata)
    pyusd_raw = get_token_balance(client, pyusd_ata)
    usdc_bal = usdc_raw / 10**DECIMALS
    usdg_bal = usdg_raw / 10**DECIMALS
    pyusd_bal = pyusd_raw / 10**DECIMALS
    sol_bal_resp = client.get_balance(wallet, commitment=Confirmed)
    sol_lamports = int(sol_bal_resp.value or 0)
    sol_bal = sol_lamports / 10**9
    
    sol_price = 0.0
    q = get_jup_quote(session, SOL_MINT, USDC_MINT, 10**9)
    if q and "outAmount" in q:
        sol_price = int(q["outAmount"]) / 10**DECIMALS
        
    total_usd = usdc_bal + usdg_bal + pyusd_bal + (sol_bal * sol_price)
    
    def format_diff(curr, old, decimals=2):
        if old is None:
            return ""
        diff = curr - old
        if abs(diff) < (10 ** -decimals):
            return ""
        color = GREEN if diff > 0 else RED
        sign = "+" if diff > 0 else ""
        return f"  {color}({sign}{diff:.{decimals}f}){RESET}"

    u_diff = format_diff(usdc_bal, prev.get("usdc") if prev else None)
    g_diff = format_diff(usdg_bal, prev.get("usdg") if prev else None)
    p_diff = format_diff(pyusd_bal, prev.get("pyusd") if prev else None)
    s_diff = format_diff(sol_bal, prev.get("sol") if prev else None, 6)
    t_diff = format_diff(total_usd, prev.get("total") if prev else None)

    print(f"\n{label}")
    print(f"  USDC: {usdc_bal:.2f}{u_diff}")
    print(f"  USDG: {usdg_bal:.2f}{g_diff}")
    print(f"  PYUSD: {pyusd_bal:.2f}{p_diff}")
    print(f"  SOL:  {sol_bal:.6f}{s_diff} (@ ${sol_price:.2f})")
    print(f"  Total Value: ${total_usd:.2f}{t_diff}")
    print("-" * 30)
    
    return {
        "usdc": usdc_bal,
        "usdg": usdg_bal,
        "pyusd": pyusd_bal,
        "sol": sol_bal,
        "usdc_raw": usdc_raw,
        "usdg_raw": usdg_raw,
        "pyusd_raw": pyusd_raw,
        "sol_lamports": sol_lamports,
        "sol_price": sol_price,
        "stablecoin_total": usdc_bal + usdg_bal + pyusd_bal,
        "total": total_usd
    }

# ============================================================
# SWAP EXECUTION logic
# ============================================================
def get_jup_quote(session, input_mint, output_mint, amount_raw, taker=None):
    # Slippage is 0 per presets
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": "0",
    }
    if taker:
        params["taker"] = taker
        params["dynamicComputeUnitLimit"] = "true"
        params["maxLamports"] = str(PRIORITY_FEE)
        
    resp = session.get(f"{JUP_API}/order", params=params, headers=get_jup_headers(), timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        if "outAmount" in data:
            if taker and not data.get("transaction"):
                pass # fallback to quote if no transaction generated
            else:
                return data

    resp = session.get(f"{JUP_API}/quote", params={
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": "0",
    }, headers=get_jup_headers(), timeout=10)
    if resp.status_code == 200:
        return resp.json()
    print(f"[!] Jup quote failed: {resp.text}")
    return None

def cap_jup_priority_fee(tx_bytes: bytes, max_fee_lamports: int = 30000) -> bytes:
    try:
        tx = VersionedTransaction.from_bytes(tx_bytes)
        msg = tx.message
        
        cb_index = -1
        for i, pk in enumerate(msg.account_keys):
            if str(pk) == "ComputeBudget111111111111111111111111111111":
                cb_index = i
                break
                
        if cb_index == -1:
            return tx_bytes
            
        cu_limit = None
        cu_price_idx = -1
        
        for i, ix in enumerate(msg.instructions):
            if ix.program_id_index == cb_index:
                if len(ix.data) == 5 and ix.data[0] == 2:
                    cu_limit = struct.unpack("<I", ix.data[1:5])[0]
                elif len(ix.data) == 9 and ix.data[0] == 3:
                    cu_price_idx = i
                    
        if cu_limit is not None and cu_price_idx != -1:
            old_price = struct.unpack("<Q", msg.instructions[cu_price_idx].data[1:9])[0]
            current_fee = (cu_limit * old_price) // 10**6
            print(f"[*] Jupiter generated priority fee: {current_fee} lamports (limit={cu_limit}, price={old_price})")
            
            # Always strip old price instructions and enforce our strict cap
            new_price = (max_fee_lamports * 10**6) // cu_limit
            new_data = b'\x03' + struct.pack("<Q", new_price)
            print(f"[*] Jupiter fee strictly capped down to {max_fee_lamports} lamports.")
            
            # Filter out all existing set_compute_unit_price instructions
            new_ixs = []
            for ix in msg.instructions:
                if ix.program_id_index == cb_index and len(ix.data) == 9 and ix.data[0] == 3:
                    continue # Skip old price ix
                new_ixs.append(ix)
            
            # Append our new price ix at the beginning (after limit)
            new_ixs.insert(1, CompiledInstruction(cb_index, new_data, b''))
            
            new_msg = MessageV0(msg.header, msg.account_keys, msg.recent_blockhash, new_ixs, msg.address_table_lookups)
            new_tx = VersionedTransaction.populate(new_msg, tx.signatures)
            return bytes(new_tx)
    except Exception as e:
        print(f"[!] Error capping Jup fee: {e}")
        
    return tx_bytes

def execute_jup_swap(session, client, keypair, quote):
    tx_b64 = quote.get("transaction")
    request_id = quote.get("requestId")
    
    if tx_b64:
        tx_bytes = base64.b64decode(tx_b64)
        tx_bytes = cap_jup_priority_fee(tx_bytes, PRIORITY_FEE)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        signed_tx_b64 = base64.b64encode(bytes(signed_tx)).decode('utf-8')
        
        if request_id:
            resp = session.post(f"{JUP_API}/execute", json={
                "requestId": request_id,
                "signedTransaction": signed_tx_b64
            }, headers=get_jup_headers(), timeout=15)
            
            if resp.status_code == 200:
                print(f"[+] Jup Swap Executed via API: {resp.json().get('txid', 'Unknown TX')}")
                return True
            else:
                print(f"[!] Jup execute API error: {resp.text}")
                return False
        else:
            try:
                result = client.send_transaction(signed_tx)
                if result.value:
                    print(f"[+] Jup Swap Sent: {result.value}")
                    client.confirm_transaction(result.value, commitment=Confirmed)
                    return True
            except Exception as e:
                print(f"[!] RPC Error during Jup swap: {e}")
            return False

    resp = session.post(f"{JUP_API}/swap", json={
        "quoteResponse": quote,
        "taker": str(keypair.pubkey()),
        "wrapAndUnwrapSol": False,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": PRIORITY_FEE,
                "priorityLevel": "medium",
            }
        },
        "jitoTipLamports": JITO_TIP,
    }, headers=get_jup_headers(), timeout=15)
    
    if resp.status_code != 200:
        print(f"[!] Jup swap error: {resp.text}")
        return False
        
    tx_b64 = resp.json().get("swapTransaction", "")
    if not tx_b64:
        return False
        
    tx_bytes = base64.b64decode(tx_b64)
    tx_bytes = cap_jup_priority_fee(tx_bytes, PRIORITY_FEE)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    
    recent_blockhash = client.get_latest_blockhash().value.blockhash
    signed_tx = VersionedTransaction(tx.message, [keypair])
    
    try:
        result = client.send_transaction(signed_tx)
        if result.value:
            print(f"[+] Jup Swap Sent: {result.value}")
            client.confirm_transaction(result.value, commitment=Confirmed)
            return True
    except Exception as e:
        print(f"[!] RPC Error during Jup swap: {e}")
    return False

def execute_stable_swap(session, client, keypair, asset_from, asset_to, amount_human):
    wallet = keypair.pubkey()
    amount_str = str(amount_human)
    
    resp = session.post(f"{STABLE_API}/swap/create/singleChain", json={
        "assetFrom": asset_from,
        "assetTo": asset_to,
        "chainFrom": str(STABLE_CHAIN_ID),
        "chainTo": str(STABLE_CHAIN_ID),
        "amountFrom": amount_str,
        "amountTo": amount_str,
        "addressFrom": str(wallet),
        "addressTo": str(wallet),
        "device": str(uuid.uuid4()),
        "gasLess": False,
    }, timeout=15)
    
    if resp.status_code != 200:
        print(f"[!] Stable create order error: {resp.text}")
        return False
        
    order = resp.json().get("data", resp.json())
    sig_hex = order.get("maintainerSignature", "")
    if not sig_hex:
        print("[!] No maintainer signature")
        return False
        
    sig_raw = bytes.fromhex(sig_hex.replace("0x", ""))
    if len(sig_raw) == 65:
        maintainer_sig = sig_raw[:64]
        recovery_id = sig_raw[64]
    elif len(sig_raw) == 64:
        maintainer_sig = sig_raw
        recovery_id = int(order.get("recoveryId", 0))
    else:
        print("[!] Bad sig length")
        return False

    nonce = int(order.get("nonce", 0))
    deadline = int(order.get("deadline", 0))
    native_fee = int(order.get("executionFeeNative", order.get("nativeFee", "0")))
    amount_raw = amount_human * 10**DECIMALS
    
    if native_fee > 0:
        print(f"[*] Stable.com is charging a native fee of {native_fee} lamports!")

    # Identify Mint/Program for stable swap logic
    # Note: USDG and PYUSD are Token2022
    def get_mint_info(asset):
        if asset == "USDC": return USDC_MINT_PK, TOKEN_PROGRAM
        if asset == "USDG": return USDG_MINT_PK, TOKEN_2022_PROGRAM
        if asset == "PYUSD": return PYUSD_MINT_PK, TOKEN_2022_PROGRAM
        return None, None

    mint_in, tp_in = get_mint_info(asset_from)
    mint_out, tp_out = get_mint_info(asset_to)

    main_state_pda, _ = find_pda([MAIN_STATE_SEED])
    nonce_pda, _ = find_pda([NONCE_SEED, bytes(wallet)])
    native_fee_pda, _ = find_pda([NATIVE_FEE_SEED])
    pool_in_pda, _ = find_pda([POOL_SEED, bytes(mint_in)])
    pool_out_pda, _ = find_pda([POOL_SEED, bytes(mint_out)])

    user_ata_in = get_ata(wallet, mint_in, tp_in)
    pool_ata_in = get_ata(pool_in_pda, mint_in, tp_in)
    user_ata_out = get_ata(wallet, mint_out, tp_out)
    pool_ata_out = get_ata(pool_out_pda, mint_out, tp_out)

    accounts = [
        AccountMeta(pubkey=wallet, is_signer=True, is_writable=True),
        AccountMeta(pubkey=wallet, is_signer=True, is_writable=True),
        AccountMeta(pubkey=nonce_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=main_state_pda, is_signer=False, is_writable=False),
        AccountMeta(pubkey=native_fee_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_in_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=mint_in, is_signer=False, is_writable=False),
        AccountMeta(pubkey=pool_ata_in, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_ata_in, is_signer=False, is_writable=True),
        AccountMeta(pubkey=pool_out_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=mint_out, is_signer=False, is_writable=False),
        AccountMeta(pubkey=pool_ata_out, is_signer=False, is_writable=True),
        AccountMeta(pubkey=wallet, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_ata_out, is_signer=False, is_writable=True),
        AccountMeta(pubkey=tp_in, is_signer=False, is_writable=False),
        AccountMeta(pubkey=tp_out, is_signer=False, is_writable=False),
        AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]

    data = bytearray()
    data.extend(SINGLE_CHAIN_SWAP_DISC)
    data.extend(struct.pack("<Q", amount_raw))
    data.extend(struct.pack("<Q", native_fee))
    data.extend(maintainer_sig)
    data.extend(struct.pack("<Q", nonce))
    data.extend(struct.pack("<q", deadline))
    data.extend(struct.pack("<B", recovery_id))

    ix = Instruction(program_id=STABLE_PROGRAM_ID, data=bytes(data), accounts=accounts)
    
    cu_limit_ix = set_compute_unit_limit(400_000)
    cu_price_ix = set_compute_unit_price(PRIORITY_FEE)

    recent_blockhash = client.get_latest_blockhash().value.blockhash
    msg = Message.new_with_blockhash([cu_limit_ix, cu_price_ix, ix], wallet, recent_blockhash)
    
    from solders.transaction import Transaction
    tx = Transaction.new_unsigned(msg)
    tx.sign([keypair], recent_blockhash)
    
    result = client.send_transaction(tx)
    if result.value:
        print(f"[+] Stable Swap Sent: {result.value}")
        client.confirm_transaction(result.value, commitment=Confirmed)
        return True
    return False

# ============================================================
# MAIN
# ============================================================
def main():
    client = Client(RPC_URL, commitment=Confirmed)
    session = requests.Session()
    headers = {"Content-Type": "application/json"}
    if JUP_API_KEY:
        headers["x-api-key"] = JUP_API_KEY
    session.headers.update(headers)

    try:
        keypair = Keypair.from_base58_string(PRIVATE_KEY)
    except Exception:
        keypair = Keypair.from_bytes(bytes(json.loads(PRIVATE_KEY)))

    wallet = keypair.pubkey()
    print(f"[*] Wallet: {wallet}")
    state_store = BotStateStore()
    state_store.start_session(wallet)

    usdc_ata = get_ata(wallet, USDC_MINT_PK, TOKEN_PROGRAM)
    usdg_ata = get_ata(wallet, USDG_MINT_PK, TOKEN_2022_PROGRAM)
    pyusd_ata = get_ata(wallet, PYUSD_MINT_PK, TOKEN_2022_PROGRAM)
    
    pool_usdc_pda, _ = find_pda([POOL_SEED, bytes(USDC_MINT_PK)])
    pool_usdc_ata = get_ata(pool_usdc_pda, USDC_MINT_PK, TOKEN_PROGRAM)
    
    pool_usdg_pda, _ = find_pda([POOL_SEED, bytes(USDG_MINT_PK)])
    pool_usdg_ata = get_ata(pool_usdg_pda, USDG_MINT_PK, TOKEN_2022_PROGRAM)

    pool_pyusd_pda, _ = find_pda([POOL_SEED, bytes(PYUSD_MINT_PK)])
    pool_pyusd_ata = get_ata(pool_pyusd_pda, PYUSD_MINT_PK, TOKEN_2022_PROGRAM)

    accounts_to_sub = {
        "user_usdc": str(usdc_ata),
        "user_usdg": str(usdg_ata),
        "user_pyusd": str(pyusd_ata),
        "pool_usdc": str(pool_usdc_ata),
        "pool_usdg": str(pool_usdg_ata),
        "pool_pyusd": str(pool_pyusd_ata),
    }
    monitor = BalanceMonitor(RPC_URL, accounts_to_sub)
    
    print("[*] Fetching initial balances...")
    monitor.balances["user_usdc"] = get_token_balance(client, usdc_ata)
    monitor.balances["user_usdg"] = get_token_balance(client, usdg_ata)
    monitor.balances["user_pyusd"] = get_token_balance(client, pyusd_ata)
    monitor.balances["pool_usdc"] = get_token_balance(client, pool_usdc_ata)
    monitor.balances["pool_usdg"] = get_token_balance(client, pool_usdg_ata)
    monitor.balances["pool_pyusd"] = get_token_balance(client, pool_pyusd_ata)
    
    print("[*] Starting WebSocket Balance Monitor...")
    monitor.start()
    
    prev_port = print_portfolio(session, client, wallet, usdc_ata, usdg_ata, pyusd_ata, "[*] BEFORE ARB")
    state_store.update_snapshot(
        {
            "USDC": prev_port["usdc_raw"],
            "USDG": prev_port["usdg_raw"],
            "PYUSD": prev_port["pyusd_raw"],
        },
        {
            "USDC": monitor.balances["pool_usdc"],
            "USDG": monitor.balances["pool_usdg"],
            "PYUSD": monitor.balances["pool_pyusd"],
        },
        prev_port["sol_lamports"],
        prev_port["sol_price"],
    )
    state_store.set_status("scanning", "Scanning markets")
    print("[*] Starting Arb Bot loop...")
    while True:
        try:
            # Fetch user and pool balances directly from RPC to avoid WebSocket cache lag
            try:
                usdc_raw = get_token_balance(client, usdc_ata)
            except Exception:
                usdc_raw = monitor.balances["user_usdc"]

            try:
                usdg_raw = get_token_balance(client, usdg_ata)
            except Exception:
                usdg_raw = monitor.balances["user_usdg"]

            try:
                pyusd_raw = get_token_balance(client, pyusd_ata)
            except Exception:
                pyusd_raw = monitor.balances["user_pyusd"]

            try:
                pool_usdc_raw = get_token_balance(client, pool_usdc_ata)
            except Exception:
                pool_usdc_raw = monitor.balances["pool_usdc"]

            try:
                pool_usdg_raw = get_token_balance(client, pool_usdg_ata)
            except Exception:
                pool_usdg_raw = monitor.balances["pool_usdg"]

            try:
                pool_pyusd_raw = get_token_balance(client, pool_pyusd_ata)
            except Exception:
                pool_pyusd_raw = monitor.balances["pool_pyusd"]

            usdc_bal = usdc_raw // 10**DECIMALS
            usdg_bal = usdg_raw // 10**DECIMALS
            pyusd_bal = pyusd_raw // 10**DECIMALS
            pool_usdc = pool_usdc_raw // 10**DECIMALS
            pool_usdg = pool_usdg_raw // 10**DECIMALS
            pool_pyusd = pool_pyusd_raw // 10**DECIMALS

            try:
                current_sol_lamports = int(client.get_balance(wallet, commitment=Confirmed).value or 0)
            except Exception:
                current_sol_lamports = prev_port["sol_lamports"]

            state_store.update_snapshot(
                {"USDC": usdc_raw, "USDG": usdg_raw, "PYUSD": pyusd_raw},
                {"USDC": pool_usdc_raw, "USDG": pool_usdg_raw, "PYUSD": pool_pyusd_raw},
                current_sol_lamports,
                prev_port["sol_price"],
            )
            state_store.set_status("scanning", "Scanning markets")

            permutations = [
                ("USDC", "USDG", usdc_bal, pool_usdg, USDC_MINT, USDG_MINT),
                ("USDG", "USDC", usdg_bal, pool_usdc, USDG_MINT, USDC_MINT),
                ("USDC", "PYUSD", usdc_bal, pool_pyusd, USDC_MINT, PYUSD_MINT),
                ("PYUSD", "USDC", pyusd_bal, pool_usdc, PYUSD_MINT, USDC_MINT),
                ("USDG", "PYUSD", usdg_bal, pool_pyusd, USDG_MINT, PYUSD_MINT),
                ("PYUSD", "USDG", pyusd_bal, pool_usdg, PYUSD_MINT, USDG_MINT),
            ]
            
            estimated_execution_cost_usd = (
                state_store.estimated_execution_cost_usd(DEFAULT_EXECUTION_COST_USD)
                * EXECUTION_COST_SAFETY_MULTIPLIER
            )
            best_selection_score = None
            best_route = None
            rescue_asset = None
            
            for (asset_from, asset_to, bal_from, pool_to, mint_from, mint_to) in permutations:
                simulated_bal = bal_from
                potential_rescue = None
                
                if asset_from == "USDC" and bal_from < MIN_TRADE_SIZE_USD:
                    max_rescue_pyusd = min(pyusd_bal, pool_usdc - 1) if pool_usdc > 0 else 0
                    max_rescue_usdg = min(usdg_bal, pool_usdc - 1) if pool_usdc > 0 else 0
                    
                    if max_rescue_pyusd >= MIN_TRADE_SIZE_USD and asset_to != "PYUSD":
                        simulated_bal = max_rescue_pyusd
                        potential_rescue = "PYUSD"
                    elif max_rescue_usdg >= MIN_TRADE_SIZE_USD and asset_to != "USDG":
                        simulated_bal = max_rescue_usdg
                        potential_rescue = "USDG"

                effective_min_trade_size = MIN_TRADE_SIZE_USD
                if asset_to == "USDG":
                    effective_min_trade_size = calculate_refill_aware_min_size(
                        pool_to,
                        MIN_TRADE_SIZE_USD,
                        USDG_REFILL_THRESHOLD_USD,
                        USDG_REFILL_BUFFER_USD,
                    )

                if simulated_bal >= effective_min_trade_size and pool_to >= effective_min_trade_size + 1:
                    max_feasible_size = int(min(simulated_bal, pool_to - 1))
                    coarse_sizes = generate_candidate_sizes(max_feasible_size, effective_min_trade_size)
                    if not coarse_sizes:
                        continue

                    # A rescue path contains an additional Stable.com transaction,
                    # so reserve another half-cycle of observed execution cost.
                    route_cost_estimate = estimated_execution_cost_usd * (1.5 if potential_rescue else 1.0)
                    evaluated = {}
                    print(
                        f"[*] Dynamic sizing {asset_to} -> {asset_from}: "
                        f"{coarse_sizes[0]}-{coarse_sizes[-1]} tokens | "
                        f"estimated cost ${route_cost_estimate:.6f} | "
                        f"minimum {effective_min_trade_size}"
                    )

                    def evaluate_size(size):
                        if size in evaluated:
                            return evaluated[size]
                        probe_amount_raw = int(size) * 10**DECIMALS
                        quote = get_jup_quote(session, mint_to, mint_from, probe_amount_raw)
                        if not quote:
                            evaluated[size] = None
                            return None
                        out_human = int(quote.get("outAmount", 0)) / 10**DECIMALS
                        metrics = calculate_quote_metrics(size, out_human, route_cost_estimate)
                        eligible = is_profitable_candidate(
                            metrics,
                            MIN_NET_PROFIT_USD,
                            MIN_NET_RETURN_BPS,
                        )
                        marker = "eligible" if eligible else "skip"
                        print(
                            f"    {size:>8} {asset_to} -> {out_human:.6f} {asset_from} | "
                            f"gross ${metrics['gross_profit_usd']:+.6f} | "
                            f"net ${metrics['net_profit_usd']:+.6f} "
                            f"({metrics['net_return_bps']:+.4f} bps) [{marker}]"
                        )
                        evaluated[size] = (quote, metrics)
                        if QUOTE_SAMPLE_DELAY_SECONDS > 0:
                            time.sleep(QUOTE_SAMPLE_DELAY_SECONDS)
                        return evaluated[size]

                    for size in coarse_sizes:
                        evaluate_size(size)

                    quoted_coarse = {
                        size: result
                        for size, result in evaluated.items()
                        if result is not None
                    }
                    if not quoted_coarse:
                        continue

                    eligible_coarse = {
                        size: result
                        for size, result in quoted_coarse.items()
                        if is_profitable_candidate(
                            result[1],
                            MIN_NET_PROFIT_USD,
                            MIN_NET_RETURN_BPS,
                        )
                    }
                    if eligible_coarse:
                        coarse_best_size = max(
                            eligible_coarse,
                            key=lambda size: capital_efficiency_key(
                                size,
                                eligible_coarse[size][1],
                            ),
                        )
                    else:
                        # No coarse point passed yet; refine around the closest
                        # point by absolute net dollars in case a midpoint does.
                        coarse_best_size = max(
                            quoted_coarse,
                            key=lambda size: quoted_coarse[size][1]["net_profit_usd"],
                        )
                    refinement_sizes = generate_refinement_sizes(
                        coarse_best_size,
                        coarse_sizes,
                        effective_min_trade_size,
                        max_feasible_size,
                    )
                    for size in refinement_sizes:
                        evaluate_size(size)

                    eligible_results = [
                        (size, quote, metrics)
                        for size, result in evaluated.items()
                        if result is not None
                        for quote, metrics in [result]
                        if is_profitable_candidate(
                            metrics,
                            MIN_NET_PROFIT_USD,
                            MIN_NET_RETURN_BPS,
                        )
                    ]
                    if not eligible_results:
                        continue

                    local_best = max(
                        eligible_results,
                        key=lambda item: capital_efficiency_key(item[0], item[2]),
                    )
                    swap_size, quote, metrics = local_best
                    selection_score = capital_efficiency_key(swap_size, metrics)
                    if best_selection_score is None or selection_score > best_selection_score:
                        best_selection_score = selection_score
                        best_route = (
                            asset_from,
                            asset_to,
                            swap_size,
                            mint_from,
                            mint_to,
                            metrics,
                            route_cost_estimate,
                        )
                        rescue_asset = potential_rescue

            if not best_route:
                print(f"[!] No profitable arb output found on Jup right now. Waiting for updates...")
                monitor.update_event.wait(timeout=5)
                monitor.update_event.clear()
                continue

            (
                asset_from,
                asset_to,
                swap_size,
                mint_from,
                mint_to,
                selected_metrics,
                selected_cost_estimate,
            ) = best_route
            print(
                f"\n[*] Best dynamic route: {asset_from} -> {asset_to} | "
                f"size {swap_size} | estimated net ${selected_metrics['net_profit_usd']:.6f} | "
                f"efficiency {selected_metrics['net_return_bps']:.4f} bps"
            )
            route_label = f"{asset_from} -> {asset_to} -> {asset_from}"

            if swap_size < 1:
                print(f"[!] Swap size too small ({swap_size})")
                monitor.update_event.wait(timeout=5)
                monitor.update_event.clear()
                continue

            print("[*] Revalidating selected size twice before taking first-leg exposure...")
            verify_failed = False
            verification_metrics = [selected_metrics]
            probe_amount_raw = int(swap_size) * 10**DECIMALS
            for i in range(2):
                time.sleep(0.5)
                v_quote = get_jup_quote(session, mint_to, mint_from, probe_amount_raw)
                if not v_quote:
                    print(f"    Verify {i+1}/2: quote unavailable")
                    verify_failed = True
                    break
                out_human = int(v_quote.get("outAmount", 0)) / 10**DECIMALS
                metrics = calculate_quote_metrics(swap_size, out_human, selected_cost_estimate)
                verification_metrics.append(metrics)
                if is_profitable_candidate(metrics, MIN_NET_PROFIT_USD, MIN_NET_RETURN_BPS):
                    print(
                        f"    Verify {i+1}/2: net ${metrics['net_profit_usd']:.6f} "
                        f"({metrics['net_return_bps']:.4f} bps)"
                    )
                else:
                    print(
                        f"    Verify {i+1}/2: below threshold | "
                        f"net ${metrics['net_profit_usd']:.6f}"
                    )
                    verify_failed = True
                    break

            if verify_failed:
                print("[!] Selected size failed net-profit revalidation. Skipping...")
                time.sleep(1)
                continue

            # Record the least favorable verified quote as the expected result.
            selected_metrics = min(
                verification_metrics,
                key=lambda metrics: metrics["net_profit_usd"],
            )

            attempt_before = print_portfolio(
                session,
                client,
                wallet,
                usdc_ata,
                usdg_ata,
                pyusd_ata,
                "[*] TRADE START",
                prev=prev_port,
            )
            state_store.set_status("executing_stable", "Executing Stable.com leg", route=route_label)

            if rescue_asset:
                print(f"[*] Stuck {rescue_asset} detected. Executing {rescue_asset} -> USDC rescue swap on stable.com first...")
                state_store.set_status("recovering", f"Recovering {rescue_asset}", route=route_label)
                if not execute_stable_swap(session, client, keypair, rescue_asset, "USDC", swap_size):
                    print(f"[!] Rescue swap failed. Aborting.")
                    failed_port = print_portfolio(session, client, wallet, usdc_ata, usdg_ata, pyusd_ata, "[*] AFTER FAILED ATTEMPT", prev=attempt_before)
                    state_store.record_attempt(
                        route_label,
                        swap_size,
                        selected_metrics["gross_profit_usd"],
                        attempt_before,
                        failed_port,
                        False,
                        "Stable.com rescue swap failed",
                    )
                    state_store.update_snapshot(
                        {"USDC": failed_port["usdc_raw"], "USDG": failed_port["usdg_raw"], "PYUSD": failed_port["pyusd_raw"]},
                        {"USDC": pool_usdc_raw, "USDG": pool_usdg_raw, "PYUSD": pool_pyusd_raw},
                        failed_port["sol_lamports"],
                        failed_port["sol_price"],
                    )
                    prev_port = failed_port
                    state_store.set_status("error", "Rescue failed", route=route_label, error="Stable.com rescue swap failed")
                    monitor.update_event.wait(timeout=5)
                    monitor.update_event.clear()
                    continue
                print(f"[+] Rescue swap success! Proceeding with main arb...")
                state_store.set_status("executing_stable", "Executing Stable.com leg", route=route_label)

            print(f"[*] Swapping {swap_size} {asset_from} -> {asset_to} on stable.com...")
            if not execute_stable_swap(session, client, keypair, asset_from, asset_to, swap_size):
                print("[!] Stable.com swap failed. Aborting.")
                failed_port = print_portfolio(session, client, wallet, usdc_ata, usdg_ata, pyusd_ata, "[*] AFTER FAILED ATTEMPT", prev=attempt_before)
                state_store.record_attempt(
                    route_label,
                    swap_size,
                    selected_metrics["gross_profit_usd"],
                    attempt_before,
                    failed_port,
                    False,
                    "Stable.com arbitrage leg failed",
                )
                state_store.update_snapshot(
                    {"USDC": failed_port["usdc_raw"], "USDG": failed_port["usdg_raw"], "PYUSD": failed_port["pyusd_raw"]},
                    {"USDC": pool_usdc_raw, "USDG": pool_usdg_raw, "PYUSD": pool_pyusd_raw},
                    failed_port["sol_lamports"],
                    failed_port["sol_price"],
                )
                prev_port = failed_port
                state_store.set_status("error", "Stable.com leg failed", route=route_label, error="Stable.com arbitrage leg failed")
                monitor.update_event.wait(timeout=5)
                monitor.update_event.clear()
                continue
            
            print(f"[+] Stable.com swap success!")

            # 3. Swap all to Jup
            print(f"\n[*] Swapping {asset_to} back to {asset_from} on Jupiter (0% slippage)...")
            state_store.set_status("exposed", f"Holding {asset_to}; preparing exit", route=route_label)

            temp_asset_key = f"user_{asset_to.lower()}"
            retries = 0
            while retries < 5:
                new_bal = monitor.balances[temp_asset_key]
                if new_bal > 0:
                    break
                print(f"[!] Balance not showing yet... waiting 1s (Attempt {retries+1}/5)")
                time.sleep(1)
                retries += 1
            
            print(f"[*] Current {asset_to} balance to swap back: {new_bal / 10**DECIMALS:.6f}")
            
            balance_key = f"user_{asset_to.lower()}"
            if asset_to == "USDC":
                ata_to_check = usdc_ata
            elif asset_to == "USDG":
                ata_to_check = usdg_ata
            else:
                ata_to_check = pyusd_ata

            attempts = 0
            arb_successful = False
            while True:
                attempts += 1
                
                final_quote = get_jup_quote(session, mint_to, mint_from, new_bal, taker=str(wallet))
                if not final_quote:
                    print("[!] Failed to get Jupiter quote. Retrying...")
                    time.sleep(2)
                    if monitor.balances[balance_key] < 100000:
                        print("[+] Balance is now 0. A previous swap must have landed!")
                        arb_successful = True
                        break
                    continue
                
                expected_out = int(final_quote["outAmount"]) / 10**DECIMALS
                in_human = new_bal / 10**DECIMALS
                if expected_out <= in_human:
                    if attempts > 10:
                        print(f"[!] Attempts exceeded 10. Discarding loop and restarting...")
                        break
                    print(f"[-] Attempt {attempts}: Quote unprofitable ({in_human} -> {expected_out:.6f}). Waiting for price to improve...")
                    time.sleep(3)
                    continue

                print(f"[*] Attempt {attempts}: Firing Jup swap for {expected_out:.6f} {asset_from}...")
                state_store.set_status("executing_jupiter", "Executing Jupiter exit", route=route_label)
            
                if execute_jup_swap(session, client, keypair, final_quote):
                    print("[+] Jupiter swap submitted! Waiting to verify balance clearance...")
                    cleared = False
                    for check_i in range(15):
                        time.sleep(2)
                        try:
                            check_bal = get_token_balance(client, ata_to_check)
                        except Exception:
                            check_bal = monitor.balances[balance_key]
                        ws_check = monitor.balances[balance_key]
                        print(f"    Verify check {check_i+1}/5: RPC={check_bal}, WS={ws_check}")
                        if check_bal < 100000 and ws_check < 100000:
                            cleared = True
                            break
                    if cleared:
                        print("[+] Balance confirmed cleared!")
                        arb_successful = True
                        break
                    else:
                        print("[!] Balance not cleared yet. Retrying swap...")
                else:
                    print("[!] Transaction execution/preflight failed. Checking if balance cleared...")
                    if monitor.balances[balance_key] < 100000:
                        print("[+] Balance is 0! Previous swap landed.")
                        arb_successful = True
                        break
                    print("Retrying immediately...")

            # Out of the Jupiter swap loop (swap succeeded and balance is verified to be 0)
            new_port = print_portfolio(session, client, wallet, usdc_ata, usdg_ata, pyusd_ata, "[*] AFTER ARB", prev=prev_port)
            note = "" if arb_successful else f"{asset_to} exit was not confirmed; intermediate balance may remain"
            record = state_store.record_attempt(
                route_label,
                swap_size,
                selected_metrics["gross_profit_usd"],
                attempt_before,
                new_port,
                arb_successful,
                note,
            )
            state_store.update_snapshot(
                {"USDC": new_port["usdc_raw"], "USDG": new_port["usdg_raw"], "PYUSD": new_port["pyusd_raw"]},
                {"USDC": pool_usdc_raw, "USDG": pool_usdg_raw, "PYUSD": pool_pyusd_raw},
                new_port["sol_lamports"],
                new_port["sol_price"],
            )
            if arb_successful:
                state_store.set_status("scanning", "Scanning markets")
            else:
                state_store.set_status("exposed", f"Unresolved {asset_to} position", route=route_label, error=note)
            print(
                f"\n[+] Accounting updated | Net PnL: ${record['realized_pnl_usd']:.6f} | "
                f"SOL consumed: {record['sol_consumed']:.9f} "
                f"(~${record['sol_cost_usd']:.6f})\n"
            )
            
            prev_port = new_port
        except Exception as e:
            print(f"[!] Loop error: {e}")
            state_store.set_status("error", "Loop error", error=e)
            time.sleep(5)

if __name__ == "__main__":
    main()
