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
from dataclasses import dataclass
from dotenv import load_dotenv
from solders.commitment_config import CommitmentLevel
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.instruction import Instruction, AccountMeta, CompiledInstruction
from solders.transaction import VersionedTransaction
from solders.message import Message, MessageV0
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.rpc.config import RpcContextConfig
from solders.rpc.requests import IsBlockhashValid
from solders.rpc.responses import IsBlockhashValidResp
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from state_store import BotStateStore
from recovery_store import RecoveryStore
from balance_tracker import (
    BalanceTracker,
    coherent_ws_match,
    confirm_balances_ws_first,
)
from sizing import (
    absolute_profit_key,
    acquired_balance_delta,
    acquired_delta_is_cleared,
    adjusted_drain_minimum_raw,
    calculate_quote_metrics,
    drain_candidate_is_valid,
    generate_candidate_sizes,
    generate_drain_candidate_amounts_raw,
    generate_refinement_sizes,
    is_profitable_candidate,
    maximum_safe_stable_input_raw,
    normalize_drain_window_raw,
    parse_stable_liquidity_constraint,
    parse_stable_reserve_constraint,
    stable_pool_can_settle,
    usdc_strategy_directions,
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
log_name = os.environ.get("BOT_LOG_NAME", os.path.splitext(os.path.basename(__file__))[0])
log_filename = f"logs/{log_name}.log"
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
# Stable's reported USDG constraint is 1.8 tokens; keep a default 0.1-token
# race buffer while remaining below the 2.0-token refill trigger.
USDG_PROTOCOL_RESERVE_FLOOR_RAW = 1_800_000
USDG_REFILL_TRIGGER_RAW = 2_000_000
USDG_DEFAULT_MAX_REMAINDER_RAW = 1_990_000
USDG_DRAIN_SAFETY_BUFFER_RAW = max(
    0,
    int(round(float(os.environ.get("USDG_DRAIN_SAFETY_BUFFER_USD", "0.10")) * 1_000_000)),
)
_requested_usdg_min_remainder_raw = max(
    0,
    int(round(float(os.environ.get("USDG_DRAIN_MIN_REMAINDER_USD", "1.80")) * 1_000_000)),
    int(os.environ.get("USDG_DRAIN_DUST_RAW", "1")),
)
_requested_usdg_max_remainder_raw = max(
    0,
    int(round(float(os.environ.get("USDG_MAX_REMAINDER_USD", "1.99")) * 1_000_000)),
)
if _requested_usdg_max_remainder_raw < USDG_PROTOCOL_RESERVE_FLOOR_RAW:
    # Migrate the former $1 maximum to the new safe window automatically.
    _requested_usdg_max_remainder_raw = USDG_DEFAULT_MAX_REMAINDER_RAW
USDG_DRAIN_DUST_RAW, USDG_MAX_REMAINDER_RAW = normalize_drain_window_raw(
    _requested_usdg_min_remainder_raw,
    _requested_usdg_max_remainder_raw,
    protocol_floor_raw=USDG_PROTOCOL_RESERVE_FLOOR_RAW,
    safety_buffer_raw=USDG_DRAIN_SAFETY_BUFFER_RAW,
    refill_trigger_raw=USDG_REFILL_TRIGGER_RAW,
)
USDG_DRAIN_MIN_REMAINDER_RAW = USDG_DRAIN_DUST_RAW
USDG_DRAIN_MIN_REMAINDER_USD = USDG_DRAIN_MIN_REMAINDER_RAW / 1_000_000
USDG_MAX_REMAINDER_USD = USDG_MAX_REMAINDER_RAW / 1_000_000
DEFAULT_EXECUTION_COST_USD = float(os.environ.get("DEFAULT_EXECUTION_COST_USD", "0.005"))
EXECUTION_COST_SAFETY_MULTIPLIER = float(os.environ.get("EXECUTION_COST_SAFETY_MULTIPLIER", "1.25"))
QUOTE_SAMPLE_DELAY_SECONDS = float(os.environ.get("QUOTE_SAMPLE_DELAY_SECONDS", "0.15"))
WS_CONFIRM_TIMEOUT_SECONDS = float(os.environ.get("WS_CONFIRM_TIMEOUT_SECONDS", "12"))
RECOVERY_MIN_NET_PROFIT_USD = float(
    os.environ.get("RECOVERY_MIN_NET_PROFIT_USD", "0.10")
)
SIGNATURE_CHECK_INTERVAL_SECONDS = max(
    1.0,
    float(os.environ.get("SIGNATURE_CHECK_INTERVAL_SECONDS", "5")),
)
STABLE_POOL_REFILL_SYNC_SECONDS = float(
    os.environ.get("STABLE_POOL_REFILL_SYNC_SECONDS", "15")
)
STABLE_BACKEND_LAG_RETRY_SECONDS = float(
    os.environ.get("STABLE_BACKEND_LAG_RETRY_SECONDS", "5")
)

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
POSITION_TOLERANCE_RAW = 100_000
SUBMIT_ONLY_OPTS = TxOpts(
    skip_confirmation=True,
    preflight_commitment=Confirmed,
)

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
    return None

class BalanceMonitor(BalanceTracker):
    def __init__(self, rpc_url, accounts_to_sub):
        super().__init__(accounts_to_sub.keys())
        self.rpc_url = rpc_url
        self.accounts_to_sub = accounts_to_sub
        self.ready_event = threading.Event()
        self.thread = threading.Thread(target=self._start_loop, daemon=True)
        
    def _start_loop(self):
        asyncio.run(self._ws_loop())
        
    async def _ws_loop(self):
        ws_url = self.rpc_url.replace("https://", "wss://").replace("http://", "ws://")
        while True:
            try:
                self.ready_event.clear()
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
                                if len(sub_to_key) == len(self.accounts_to_sub):
                                    self.ready_event.set()
                        elif "method" in data and data["method"] == "accountNotification":
                            sub_id = data["params"]["subscription"]
                            key = sub_to_key.get(sub_id)
                            if key:
                                val = data["params"]["result"]["value"]
                                if val is not None:
                                    data_b64 = val["data"][0]
                                    amount_raw = parse_token_balance(data_b64)
                                    if amount_raw is None:
                                        print(f"[!] Ignoring malformed WS token data for {key}")
                                        continue
                                    slot = data["params"]["result"].get("context", {}).get("slot")
                                    print(f"[~] WS Update: {key} -> {amount_raw / 10**DECIMALS:.6f} tokens (raw: {amount_raw})")
                                    self.update(
                                        key,
                                        amount_raw,
                                        slot=int(slot) if slot is not None else None,
                                        timestamp=time.time(),
                                    )
            except Exception:
                self.ready_event.clear()
                await asyncio.sleep(5)
                
    def start(self):
        self.thread.start()

    def is_ready(self):
        return self.ready_event.is_set()

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
    last_error = None
    for attempt in range(3):
        try:
            resp = client.get_token_account_balance(ata)
            if resp.value is not None and resp.value.amount is not None:
                return int(resp.value.amount)
            last_error = RuntimeError(f"RPC returned no token balance for {ata}")
        except Exception as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"Unable to read token balance for {ata} after 3 attempts") from last_error


@dataclass(frozen=True)
class SwapSubmissionResult:
    submitted: bool
    signature: str = ""
    blockhash: str = ""
    ambiguous: bool = False
    error: str = ""

    @property
    def may_have_landed(self):
        return bool(self.submitted or self.ambiguous)

    def __bool__(self):
        return bool(self.submitted)


def get_submission_signature_status(client, signature_text):
    """Return ``not_found``, ``processed``, ``confirmed``, etc."""

    try:
        response = client.get_signature_statuses(
            [Signature.from_string(signature_text)],
            search_transaction_history=True,
        )
        status = response.value[0]
    except Exception as exc:
        return "unknown", str(exc)

    if status is None:
        return "not_found", ""
    if status.err is not None:
        return "failed", str(status.err)

    confirmation = str(status.confirmation_status or "").lower()
    if (
        "confirmed" in confirmation
        or "finalized" in confirmation
        or getattr(status, "confirmations", 0) is None
    ):
        return "confirmed", ""
    return "processed", ""


def is_submission_blockhash_valid(client, blockhash_text):
    """Return whether a submitted transaction can still land, or ``None``."""

    try:
        body = IsBlockhashValid(
            Hash.from_string(blockhash_text),
            RpcContextConfig(CommitmentLevel.Confirmed),
        )
        response = client._provider.make_request(body, IsBlockhashValidResp)
        return bool(response.value)
    except Exception:
        return None


def reconcile_persisted_submission(client, pending_store):
    """Resolve a pre-restart submission lock before scanning can resume."""

    pending = pending_store.get_pending_submission()
    if pending is None:
        return

    signature = str(pending.get("signature", ""))
    blockhash = str(pending.get("blockhash", ""))
    label = str(pending.get("label", "pending transaction"))
    if not signature or not blockhash:
        raise RuntimeError(
            "Pending submission record is incomplete; refusing to trade until repaired"
        )

    pending_store.set_status(
        "reconciling",
        f"Reconciling {label} after restart",
    )
    print(
        f"[~] Found persisted {label} submission {signature}; "
        "scanning is locked until its chain state is conclusive."
    )
    last_log_at = 0.0
    seen_on_chain = False
    while True:
        signature_state, signature_error = get_submission_signature_status(
            client,
            signature,
        )
        if signature_state in {"processed", "confirmed", "failed"}:
            seen_on_chain = True
        elif seen_on_chain and signature_state in {"not_found", "unknown"}:
            signature_state = "processed"
        if signature_state == "failed":
            print(f"[!] Persisted {label} failed on-chain: {signature_error}")
            pending_store.clear_pending_submission(signature)
            return
        if signature_state == "confirmed":
            print(
                f"[+] Persisted {label} is confirmed; refreshing wallet balances "
                "before any new entry."
            )
            pending_store.clear_pending_submission(signature)
            return
        if signature_state == "not_found":
            blockhash_valid = is_submission_blockhash_valid(client, blockhash)
            if blockhash_valid is False:
                repeat_state, repeat_error = get_submission_signature_status(
                    client,
                    signature,
                )
                if repeat_state == "failed":
                    print(f"[!] Persisted {label} failed on-chain: {repeat_error}")
                    pending_store.clear_pending_submission(signature)
                    return
                if repeat_state == "confirmed":
                    print(
                        f"[+] Persisted {label} is confirmed; refreshing wallet "
                        "balances before any new entry."
                    )
                    pending_store.clear_pending_submission(signature)
                    return
                if repeat_state == "processed":
                    seen_on_chain = True
                    continue
                if repeat_state == "unknown":
                    continue
                if repeat_state == "not_found":
                    print(
                        f"[+] Persisted {label} was never recorded and its blockhash expired."
                    )
                    pending_store.clear_pending_submission(signature)
                    return

        now = time.monotonic()
        if now - last_log_at >= 30:
            detail = f" ({signature_error})" if signature_error else ""
            print(
                f"[~] Startup reconciliation waiting: signature={signature_state}{detail}."
            )
            last_log_at = now
        time.sleep(SIGNATURE_CHECK_INTERVAL_SECONDS)


def confirm_transfer_ws_first(
    client,
    monitor,
    watched_accounts,
    after_revisions,
    label,
    timeout_seconds=WS_CONFIRM_TIMEOUT_SECONDS,
    submission=None,
    pending_store=None,
):
    """Confirm a transfer from fresh WS revisions, then one RPC snapshot.

    ``watched_accounts`` maps monitor keys to ``(token_account, predicate)``.
    Every watched key must receive a post-submission WebSocket revision and all
    predicates must match.  This prevents a stale cached zero from falsely
    confirming an exit.  RPC is a one-shot fallback, never a polling loop.
    """

    watched_keys = tuple(watched_accounts)
    ws_ready_at_start = monitor.is_ready()
    predicates = {
        key: predicate
        for key, (_, predicate) in watched_accounts.items()
    }
    result = confirm_balances_ws_first(
        monitor,
        predicates,
        {key: after_revisions[key] for key in watched_keys},
        timeout_seconds,
        rpc_reader=lambda key: get_token_balance(
            client,
            watched_accounts[key][0],
        ),
    )
    values = dict(result.balances)
    rendered = ", ".join(f"{key}={value}" for key, value in values.items())
    if result.source == "ws":
        print(f"[+] WS confirmed {label}: {rendered}")
    elif result.source == "rpc_error":
        print(f"[!] RPC confirmation snapshot failed for {label}: {result.error}")
    else:
        reason = (
            f"no matching fresh WS state within {float(timeout_seconds):.1f}s"
            if ws_ready_at_start
            else "WebSocket monitor was reconnecting during the bounded wait"
        )
        print(f"[~] {reason}; took one RPC snapshot for {label}.")
        print(
            f"[{'+' if result.confirmed else '!'}] RPC confirmation snapshot for "
            f"{label}: {rendered}"
        )
    if result.confirmed:
        if pending_store is not None and submission is not None:
            pending_store.clear_pending_submission(submission.signature)
        return True, values, result.source

    if submission is None or not submission.may_have_landed:
        return False, values, result.source

    if not submission.signature or not submission.blockhash:
        raise RuntimeError(
            f"Cannot safely reconcile ambiguous {label}: missing local signature/blockhash"
        )

    print(
        f"[~] {label} submission {submission.signature} is unresolved; "
        "blocking all new entries while WS/signature state is reconciled."
    )
    last_log_at = 0.0
    seen_on_chain = False
    confirmed_observed = False
    while True:
        if confirmed_observed:
            signature_state, signature_error = "confirmed", ""
        else:
            signature_state, signature_error = get_submission_signature_status(
                client,
                submission.signature,
            )
            if signature_state in {"processed", "confirmed", "failed"}:
                seen_on_chain = True
            elif seen_on_chain and signature_state in {"not_found", "unknown"}:
                # RPC nodes behind a load balancer can disagree temporarily.
                # Once any node has seen the signature, it can never be
                # treated as an unrecorded/expirable transaction again.
                signature_state = "processed"
        if signature_state == "failed":
            print(
                f"[!] {label} transaction failed on-chain: {signature_error or 'unknown error'}"
            )
            if pending_store is not None:
                pending_store.clear_pending_submission(submission.signature)
            return False, values, "signature_error"

        if signature_state == "confirmed":
            confirmed_result = confirm_balances_ws_first(
                monitor,
                predicates,
                {key: after_revisions[key] for key in watched_keys},
                0,
                rpc_reader=lambda key: get_token_balance(
                    client,
                    watched_accounts[key][0],
                ),
            )
            values = dict(confirmed_result.balances)
            if confirmed_result.confirmed:
                rendered = ", ".join(
                    f"{key}={value}" for key, value in values.items()
                )
                print(f"[+] Confirmed {label} after signature reconciliation: {rendered}")
                if pending_store is not None:
                    pending_store.clear_pending_submission(submission.signature)
                return True, values, confirmed_result.source
            if confirmed_result.source != "rpc_error":
                print(
                    f"[halt] {label} signature is confirmed, but its expected token/USDC "
                    "state does not match. Keeping the submission lock and waiting for "
                    "operator review or a coherent WS update."
                )
                if pending_store is not None and hasattr(pending_store, "set_status"):
                    pending_store.set_status(
                        "exposed",
                        "Confirmed transaction has unexpected balances",
                        error=f"Manual review required for {label}",
                    )
                while True:
                    mismatch_snapshot = monitor.wait_for(
                        lambda current: coherent_ws_match(current, predicates),
                        {key: after_revisions[key] for key in watched_keys},
                        timeout=30,
                    )
                    if mismatch_snapshot is not None:
                        values = {
                            key: mismatch_snapshot[key] for key in watched_keys
                        }
                        if pending_store is not None:
                            pending_store.clear_pending_submission(
                                submission.signature
                            )
                        print(f"[+] WS resolved the balance mismatch for {label}.")
                        return True, values, "ws_reconciled"
                    print(
                        f"[halt] Still waiting on the confirmed {label} balance mismatch; "
                        "no balance polling or new submission is being performed."
                    )

        if signature_state == "not_found":
            blockhash_valid = is_submission_blockhash_valid(
                client,
                submission.blockhash,
            )
            if blockhash_valid is False:
                # Close the race where the transaction was recorded between
                # the first status lookup and the blockhash-expiry check.
                repeat_state, repeat_error = get_submission_signature_status(
                    client,
                    submission.signature,
                )
                if repeat_state == "failed":
                    print(
                        f"[!] {label} transaction failed on-chain: "
                        f"{repeat_error or 'unknown error'}"
                    )
                    if pending_store is not None:
                        pending_store.clear_pending_submission(
                            submission.signature
                        )
                    return False, values, "signature_error"
                if repeat_state == "confirmed":
                    seen_on_chain = True
                    confirmed_observed = True
                    continue
                if repeat_state == "processed":
                    seen_on_chain = True
                    continue
                if repeat_state == "unknown":
                    continue
                final_result = confirm_balances_ws_first(
                    monitor,
                    predicates,
                    {key: after_revisions[key] for key in watched_keys},
                    0,
                    rpc_reader=lambda key: get_token_balance(
                        client,
                        watched_accounts[key][0],
                    ),
                )
                values = dict(final_result.balances)
                if final_result.confirmed:
                    print(f"[+] Final RPC snapshot confirmed {label}.")
                    if pending_store is not None:
                        pending_store.clear_pending_submission(submission.signature)
                    return True, values, final_result.source
                if final_result.source == "rpc_error":
                    print(
                        f"[!] Could not take the final balance snapshot for {label}; "
                        "keeping the submission lock despite blockhash expiry."
                    )
                    continue
                print(
                    f"[!] {label} never landed and its blockhash has expired; "
                    "the submission is now safe to abandon."
                )
                if pending_store is not None:
                    pending_store.clear_pending_submission(submission.signature)
                return False, values, "expired"

        fresh_snapshot = monitor.wait_for(
            lambda current: coherent_ws_match(current, predicates),
            {key: after_revisions[key] for key in watched_keys},
            timeout=SIGNATURE_CHECK_INTERVAL_SECONDS,
        )
        if fresh_snapshot is not None:
            values = {key: fresh_snapshot[key] for key in watched_keys}
            rendered = ", ".join(
                f"{key}={value}" for key, value in values.items()
            )
            print(f"[+] WS eventually confirmed {label}: {rendered}")
            if pending_store is not None:
                pending_store.clear_pending_submission(submission.signature)
            return True, values, "ws_reconciled"

        now = time.monotonic()
        if now - last_log_at >= 30:
            detail = f" ({signature_error})" if signature_error else ""
            print(
                f"[~] Still reconciling {label}: signature={signature_state}{detail}; "
                "no additional transaction will be submitted."
            )
            last_log_at = now

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
def get_jup_quote(session, input_mint, output_mint, amount_raw, taker=None, slippage_bps=0):
    slippage_bps = max(0, int(slippage_bps))
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(slippage_bps),
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
        "slippageBps": str(slippage_bps),
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

def execute_jup_swap(
    session,
    client,
    keypair,
    quote,
    pending_store=None,
    submission_label="Jupiter swap",
):
    tx_b64 = quote.get("transaction")
    request_id = quote.get("requestId")
    
    if tx_b64:
        tx_bytes = base64.b64decode(tx_b64)
        tx_bytes = cap_jup_priority_fee(tx_bytes, PRIORITY_FEE)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        signed_tx_b64 = base64.b64encode(bytes(signed_tx)).decode('utf-8')
        local_signature = str(signed_tx.signatures[0])
        blockhash = str(signed_tx.message.recent_blockhash)
        
        if request_id:
            if pending_store is not None:
                pending_store.set_pending_submission(
                    local_signature,
                    blockhash,
                    submission_label,
                )
            try:
                resp = session.post(f"{JUP_API}/execute", json={
                    "requestId": request_id,
                    "signedTransaction": signed_tx_b64
                }, headers=get_jup_headers(), timeout=15)
            except requests.RequestException as exc:
                print(
                    "[!] Jupiter execute response was lost after submission; "
                    f"reconciling local signature {local_signature}: {exc}"
                )
                return SwapSubmissionResult(
                    False,
                    local_signature,
                    blockhash,
                    ambiguous=True,
                    error=str(exc),
                )
            
            if resp.status_code == 200:
                try:
                    txid = resp.json().get("txid", local_signature)
                except ValueError:
                    txid = local_signature
                print(f"[+] Jup Swap Executed via API: {txid}")
                return SwapSubmissionResult(True, local_signature, blockhash)

            print(f"[!] Jup execute API error: {resp.text}")
            if resp.status_code >= 500:
                return SwapSubmissionResult(
                    False,
                    local_signature,
                    blockhash,
                    ambiguous=True,
                    error=resp.text,
                )
            if pending_store is not None:
                pending_store.clear_pending_submission(local_signature)
            return SwapSubmissionResult(False, error=resp.text)
        else:
            if pending_store is not None:
                pending_store.set_pending_submission(
                    local_signature,
                    blockhash,
                    submission_label,
                )
            try:
                result = client.send_transaction(signed_tx, opts=SUBMIT_ONLY_OPTS)
                if result.value:
                    print(f"[+] Jup Swap Sent: {result.value}")
                    return SwapSubmissionResult(
                        True,
                        str(result.value),
                        blockhash,
                    )
            except Exception as e:
                print(
                    "[!] RPC response was ambiguous during Jup swap; "
                    f"reconciling local signature {local_signature}: {e}"
                )
                return SwapSubmissionResult(
                    False,
                    local_signature,
                    blockhash,
                    ambiguous=True,
                    error=str(e),
                )
            return SwapSubmissionResult(
                False,
                local_signature,
                blockhash,
                ambiguous=True,
                error="RPC returned no transaction signature",
            )

    try:
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
    except requests.RequestException as exc:
        print(f"[!] Jupiter swap transaction request failed before submission: {exc}")
        return SwapSubmissionResult(False, error=str(exc))
    
    if resp.status_code != 200:
        print(f"[!] Jup swap error: {resp.text}")
        return SwapSubmissionResult(False, error=resp.text)
        
    tx_b64 = resp.json().get("swapTransaction", "")
    if not tx_b64:
        return SwapSubmissionResult(False, error="missing swapTransaction")
        
    tx_bytes = base64.b64decode(tx_b64)
    tx_bytes = cap_jup_priority_fee(tx_bytes, PRIORITY_FEE)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    
    signed_tx = VersionedTransaction(tx.message, [keypair])
    local_signature = str(signed_tx.signatures[0])
    blockhash = str(signed_tx.message.recent_blockhash)
    if pending_store is not None:
        pending_store.set_pending_submission(
            local_signature,
            blockhash,
            submission_label,
        )
    
    try:
        result = client.send_transaction(signed_tx, opts=SUBMIT_ONLY_OPTS)
        if result.value:
            print(f"[+] Jup Swap Sent: {result.value}")
            return SwapSubmissionResult(True, str(result.value), blockhash)
    except Exception as e:
        print(
            "[!] RPC response was ambiguous during Jup swap; "
            f"reconciling local signature {local_signature}: {e}"
        )
        return SwapSubmissionResult(
            False,
            local_signature,
            blockhash,
            ambiguous=True,
            error=str(e),
        )
    return SwapSubmissionResult(
        False,
        local_signature,
        blockhash,
        ambiguous=True,
        error="RPC returned no transaction signature",
    )

class StableSwapResult:
    def __init__(
        self,
        ok,
        reserve_constraint=None,
        liquidity_constraint=None,
        submission=None,
    ):
        self.ok = bool(ok)
        self.reserve_constraint = reserve_constraint
        self.liquidity_constraint = liquidity_constraint
        self.submission = (
            submission if submission is not None else SwapSubmissionResult(False)
        )

    @property
    def may_have_landed(self):
        return self.submission.may_have_landed

    def __bool__(self):
        return self.ok


def execute_stable_swap(
    session,
    client,
    keypair,
    asset_from,
    asset_to,
    amount_human,
    pending_store=None,
    submission_label="Stable.com swap",
):
    wallet = keypair.pubkey()
    amount_raw = int(round(float(amount_human) * 10**DECIMALS))
    if amount_raw <= 0:
        print("[!] Stable swap amount must be positive")
        return StableSwapResult(False)
    amount_human = amount_raw / 10**DECIMALS
    amount_str = f"{amount_human:.{DECIMALS}f}".rstrip("0").rstrip(".")
    
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
        reserve_constraint = None
        liquidity_constraint = None
        try:
            error_payload = resp.json()
            reserve_constraint = parse_stable_reserve_constraint(error_payload)
            liquidity_constraint = parse_stable_liquidity_constraint(error_payload)
            if reserve_constraint:
                required_raw = reserve_constraint["required_raw"]
                remaining_raw = reserve_constraint["remaining_raw"]
                remaining_text = (
                    f"{remaining_raw / 10**DECIMALS:.6f}"
                    if remaining_raw is not None
                    else "unknown"
                )
                print(
                    "[!] Stable.com rejected the pool remainder: "
                    f"would leave {remaining_text} {asset_to}, requires at least "
                    f"{required_raw / 10**DECIMALS:.6f}. Rescanning without retrying."
                )
            elif liquidity_constraint:
                print(
                    "[!] Stable.com order service is behind the chain: "
                    f"requested {liquidity_constraint['amount_raw'] / 10**DECIMALS:.6f}, "
                    f"backend still reports "
                    f"{liquidity_constraint['available_raw'] / 10**DECIMALS:.6f} available."
                )
        except (ValueError, TypeError):
            pass
        print(f"[!] Stable create order error: {resp.text}")
        return StableSwapResult(
            False,
            reserve_constraint=reserve_constraint,
            liquidity_constraint=liquidity_constraint,
        )
        
    order = resp.json().get("data", resp.json())
    sig_hex = order.get("maintainerSignature", "")
    if not sig_hex:
        print("[!] No maintainer signature")
        return StableSwapResult(False)
        
    sig_raw = bytes.fromhex(sig_hex.replace("0x", ""))
    if len(sig_raw) == 65:
        maintainer_sig = sig_raw[:64]
        recovery_id = sig_raw[64]
    elif len(sig_raw) == 64:
        maintainer_sig = sig_raw
        recovery_id = int(order.get("recoveryId", 0))
    else:
        print("[!] Bad sig length")
        return StableSwapResult(False)

    nonce = int(order.get("nonce", 0))
    deadline = int(order.get("deadline", 0))
    native_fee = int(order.get("executionFeeNative", order.get("nativeFee", "0")))
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
    local_signature = str(tx.signatures[0])
    blockhash = str(recent_blockhash)
    if pending_store is not None:
        pending_store.set_pending_submission(
            local_signature,
            blockhash,
            submission_label,
        )

    try:
        result = client.send_transaction(tx, opts=SUBMIT_ONLY_OPTS)
    except Exception as exc:
        print(
            "[!] Stable RPC response was ambiguous after submission; "
            f"reconciling local signature {local_signature}: {exc}"
        )
        submission = SwapSubmissionResult(
            False,
            local_signature,
            blockhash,
            ambiguous=True,
            error=str(exc),
        )
        return StableSwapResult(False, submission=submission)
    if result.value:
        print(f"[+] Stable Swap Sent: {result.value}")
        submission = SwapSubmissionResult(True, str(result.value), blockhash)
        return StableSwapResult(True, submission=submission)
    submission = SwapSubmissionResult(
        False,
        local_signature,
        blockhash,
        ambiguous=True,
        error="RPC returned no transaction signature",
    )
    return StableSwapResult(False, submission=submission)

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
    recovery_store = RecoveryStore()
    state_store.start_session(wallet)
    reconcile_persisted_submission(client, state_store)

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
    monitor.seed("user_usdc", get_token_balance(client, usdc_ata))
    monitor.seed("user_usdg", get_token_balance(client, usdg_ata))
    monitor.seed("user_pyusd", get_token_balance(client, pyusd_ata))
    monitor.seed("pool_usdc", get_token_balance(client, pool_usdc_ata))
    monitor.seed("pool_usdg", get_token_balance(client, pool_usdg_ata))
    monitor.seed("pool_pyusd", get_token_balance(client, pool_pyusd_ata))
    
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
            "USDC": monitor.get("pool_usdc"),
            "USDG": monitor.get("pool_usdg"),
            "PYUSD": monitor.get("pool_pyusd"),
        },
        prev_port["sol_lamports"],
        prev_port["sol_price"],
    )
    state_store.set_status("scanning", "Scanning markets")
    print("[*] Starting Arb Bot loop...")
    usdg_drain_min_remainder_raw = USDG_DRAIN_DUST_RAW
    stable_reserve_failure_counts = {}
    stable_reserve_backoff_until = {}
    last_unresolved_position = None
    while True:
        try:
            reconcile_persisted_submission(client, state_store)
            # Fetch user and pool balances directly from RPC to avoid WebSocket cache lag
            try:
                usdc_raw = get_token_balance(client, usdc_ata)
            except Exception:
                usdc_raw = monitor.get("user_usdc")

            try:
                usdg_raw = get_token_balance(client, usdg_ata)
            except Exception:
                usdg_raw = monitor.get("user_usdg")

            try:
                pyusd_raw = get_token_balance(client, pyusd_ata)
            except Exception:
                pyusd_raw = monitor.get("user_pyusd")

            try:
                pool_usdc_raw = get_token_balance(client, pool_usdc_ata)
            except Exception:
                pool_usdc_raw = monitor.get("pool_usdc")

            try:
                pool_usdg_raw = get_token_balance(client, pool_usdg_ata)
                monitor.observe_balance("pool_usdg", pool_usdg_raw)
            except Exception:
                pool_usdg_raw = monitor.get("pool_usdg")

            try:
                pool_pyusd_raw = get_token_balance(client, pool_pyusd_ata)
            except Exception:
                pool_pyusd_raw = monitor.get("pool_pyusd")

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
            unresolved_positions = tuple(
                (symbol, raw)
                for symbol, raw in (("USDG", usdg_raw), ("PYUSD", pyusd_raw))
                if raw > POSITION_TOLERANCE_RAW
            )
            if unresolved_positions:
                symbol, raw = unresolved_positions[0]
                active_recovery, refreshed_recovery = recovery_store.sync_detected_position(
                    symbol,
                    raw,
                    RECOVERY_MIN_NET_PROFIT_USD,
                    f"Jupiter {symbol}->USDC recovery",
                )
                if refreshed_recovery:
                    print(
                        f"[recovery] Scheduled exact {raw / 10**DECIMALS:.6f} {symbol} "
                        f"return when net profit reaches ${RECOVERY_MIN_NET_PROFIT_USD:.2f}."
                    )
                if unresolved_positions != last_unresolved_position:
                    rendered_positions = ", ".join(
                        f"{raw / 10**DECIMALS:.6f} {symbol}"
                        for symbol, raw in unresolved_positions
                    )
                    print(
                        f"[halt] Unresolved intermediate position: {rendered_positions}. "
                        "No new first leg will be submitted until it is cleared."
                    )
                    last_unresolved_position = unresolved_positions
                state_store.set_status(
                    "recovering",
                    "Recovery worker is monitoring the exact intermediate position",
                    error=(
                        f"Waiting for ${active_recovery['min_net_profit_usd']:.2f} net "
                        "return threshold before the Jupiter exit"
                    ),
                )
                monitor.update_event.wait(timeout=5)
                monitor.update_event.clear()
                continue
            last_unresolved_position = None
            state_store.set_status("scanning", "Scanning markets")

            token_configs = {
                "USDG": {
                    "mint": USDG_MINT,
                    "stable_pool": pool_usdg_raw / 10**DECIMALS,
                    "token_ata": usdg_ata,
                    "balance_key": "user_usdg",
                },
                "PYUSD": {
                    "mint": PYUSD_MINT,
                    "stable_pool": pool_pyusd_raw / 10**DECIMALS,
                    "token_ata": pyusd_ata,
                    "balance_key": "user_pyusd",
                },
            }
            strategies = []
            for token, venue_order in usdc_strategy_directions():
                config = token_configs[token]
                stable_first = venue_order == "stable_first"
                strategies.append(
                    {
                        "token": token,
                        "venue_order": venue_order,
                        "jup_input_mint": config["mint"] if stable_first else USDC_MINT,
                        "jup_output_mint": USDC_MINT if stable_first else config["mint"],
                        "stable_destination_pool": (
                            config["stable_pool"]
                            if stable_first
                            else pool_usdc_raw / 10**DECIMALS
                        ),
                        "token_ata": config["token_ata"],
                        "balance_key": config["balance_key"],
                    }
                )
            
            estimated_execution_cost_usd = (
                state_store.estimated_execution_cost_usd(DEFAULT_EXECUTION_COST_USD)
                * EXECUTION_COST_SAFETY_MULTIPLIER
            )
            best_route = None
            
            for strategy in strategies:
                token = strategy["token"]
                venue_order = strategy["venue_order"]
                route_key = (token, venue_order)
                if time.monotonic() < stable_reserve_backoff_until.get(route_key, 0):
                    continue
                pool_to = strategy["stable_destination_pool"]
                usdg_sizing_mode = (
                    "drain" if venue_order == "stable_first" and token == "USDG" else None
                )
                usdg_drain_mode = usdg_sizing_mode == "drain"
                if usdg_drain_mode:
                    sync_remaining = monitor.seconds_until_increase_settled(
                        "pool_usdg",
                        STABLE_POOL_REFILL_SYNC_SECONDS,
                    )
                    if sync_remaining > 0:
                        print(
                            "[wait] USDG refill is confirmed on-chain, but Stable.com's "
                            f"order service may still be syncing; retrying this route in "
                            f"{sync_remaining:.1f}s"
                        )
                        continue
                drain_candidate_raws = []
                if usdg_drain_mode:
                    min_trade_raw = int(round(MIN_TRADE_SIZE_USD * 10**DECIMALS))
                    drain_candidate_raws = generate_drain_candidate_amounts_raw(
                        pool_usdg_raw,
                        usdc_raw,
                        min_trade_raw,
                        dust_raw=usdg_drain_min_remainder_raw,
                        max_remainder_raw=USDG_MAX_REMAINDER_RAW,
                    )
                    if not drain_candidate_raws:
                        pool_human = pool_usdg_raw / 10**DECIMALS
                        safe_capacity_raw = maximum_safe_stable_input_raw(
                            usdc_raw,
                            pool_usdg_raw,
                            usdg_drain_min_remainder_raw,
                        )
                        if safe_capacity_raw < min_trade_raw:
                            print(
                                f"[skip] USDC/USDG (Stable->Jupiter): pool has "
                                f"${pool_human:.6f}; only "
                                f"${safe_capacity_raw / 10**DECIMALS:.6f} can be traded "
                                f"while protecting the reserve; minimum is "
                                f"${MIN_TRADE_SIZE_USD:.6f}"
                            )
                            continue
                        usdg_sizing_mode = "partial"
                        usdg_drain_mode = False
                        max_feasible_size = safe_capacity_raw / 10**DECIMALS
                        coarse_sizes = generate_candidate_sizes(
                            max_feasible_size,
                            MIN_TRADE_SIZE_USD,
                        )
                        # Include the wallet/pool-bound exact maximum, including
                        # fractional token units, without also quoting the
                        # nearly identical rounded-down maximum.
                        whole_maximum = int(max_feasible_size)
                        if max_feasible_size != whole_maximum:
                            coarse_sizes = [
                                size for size in coarse_sizes if size != whole_maximum
                            ]
                        coarse_sizes = sorted(set(coarse_sizes + [max_feasible_size]))
                        print(
                            f"[fallback] USDG cannot reach the refill window; evaluating "
                            f"normal sizes up to the exact safe maximum "
                            f"${max_feasible_size:.6f}."
                        )
                    else:
                        coarse_sizes = [raw / 10**DECIMALS for raw in drain_candidate_raws]
                        max_feasible_size = coarse_sizes[-1]
                    effective_min_trade_size = MIN_TRADE_SIZE_USD
                else:
                    effective_min_trade_size = MIN_TRADE_SIZE_USD
                    protected_reserve_usd = (
                        usdg_drain_min_remainder_raw / 10**DECIMALS
                    )
                    if (
                        usdc_bal < effective_min_trade_size
                        or pool_to < effective_min_trade_size + protected_reserve_usd
                    ):
                        continue
                    max_feasible_size = int(
                        min(usdc_bal, pool_to - protected_reserve_usd)
                    )
                    coarse_sizes = generate_candidate_sizes(max_feasible_size, effective_min_trade_size)
                    if not coarse_sizes:
                        continue

                route_cost_estimate = estimated_execution_cost_usd
                evaluated = {}
                venue_label = "Stable->Jupiter" if venue_order == "stable_first" else "Jupiter->Stable"
                if usdg_drain_mode:
                    print(
                        f"[*] USDG drain sizing USDC/{token} ({venue_label}): "
                        f"{coarse_sizes[0]:.6f}-{coarse_sizes[-1]:.6f} tokens | "
                        f"pool remainder <= ${USDG_MAX_REMAINDER_USD:.6f} | "
                        f"estimated cost ${route_cost_estimate:.6f}"
                    )
                else:
                    print(
                        f"[*] Dynamic sizing USDC/{token} ({venue_label}): "
                        f"{coarse_sizes[0]}-{coarse_sizes[-1]} tokens | "
                        f"estimated cost ${route_cost_estimate:.6f} | "
                        f"minimum {effective_min_trade_size}"
                    )

                def evaluate_size(size):
                    if size in evaluated:
                        return evaluated[size]
                    probe_amount_raw = (
                        int(round(size * 10**DECIMALS))
                        if usdg_sizing_mode in {"drain", "partial"}
                        else int(size) * 10**DECIMALS
                    )
                    input_human = probe_amount_raw / 10**DECIMALS
                    # Use the same wallet-specific executable order request as
                    # the eventual submission. Generic quotes were repeatedly
                    # optimistic relative to the final taker-bound order.
                    quote = get_jup_quote(
                        session,
                        strategy["jup_input_mint"],
                        strategy["jup_output_mint"],
                        probe_amount_raw,
                        taker=str(wallet),
                    )
                    if not quote:
                        evaluated[size] = None
                        return None
                    out_human = int(quote.get("outAmount", 0)) / 10**DECIMALS
                    metrics = calculate_quote_metrics(input_human, out_human, route_cost_estimate)
                    pool_can_settle = stable_pool_can_settle(
                        venue_order,
                        input_human,
                        out_human,
                        pool_to,
                        reserve=(
                            0
                            if usdg_drain_mode
                            else usdg_drain_min_remainder_raw / 10**DECIMALS
                        ),
                    )
                    eligible = pool_can_settle and is_profitable_candidate(
                        metrics,
                        MIN_NET_PROFIT_USD,
                        MIN_NET_RETURN_BPS,
                    )
                    if not pool_can_settle:
                        marker = "skip: Stable pool capacity"
                    else:
                        marker = "eligible" if eligible else "skip"
                    jup_pair = f"{token}->USDC" if venue_order == "stable_first" else f"USDC->{token}"
                    print(
                        f"    {input_human:>12.6f} {jup_pair} -> {out_human:.6f} | "
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
                    ) and stable_pool_can_settle(
                        venue_order,
                        result[1]["input_amount"],
                        result[1]["output_amount"],
                        pool_to,
                        reserve=(
                            0
                            if usdg_drain_mode
                            else usdg_drain_min_remainder_raw / 10**DECIMALS
                        ),
                    )
                }
                if eligible_coarse:
                    coarse_best_size = max(
                        eligible_coarse,
                        key=lambda size: absolute_profit_key(
                            size,
                            eligible_coarse[size][1],
                        ),
                    )
                else:
                    # No coarse point passed yet; refine around the closest
                    # point by absolute net dollars in case a midpoint does.
                    coarse_best_size = max(
                        quoted_coarse,
                        key=lambda size: absolute_profit_key(
                            size,
                            quoted_coarse[size][1],
                        ),
                    )
                refinement_sizes = [] if usdg_drain_mode else generate_refinement_sizes(
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
                    ) and stable_pool_can_settle(
                        venue_order,
                        metrics["input_amount"],
                        metrics["output_amount"],
                        pool_to,
                        reserve=(
                            0
                            if usdg_drain_mode
                            else usdg_drain_min_remainder_raw / 10**DECIMALS
                        ),
                    )
                ]
                if not eligible_results:
                    continue

                local_best = max(
                    eligible_results,
                    key=lambda item: absolute_profit_key(item[0], item[2]),
                )
                swap_size, quote, metrics = local_best
                best_route = (
                    strategy,
                    swap_size,
                    metrics,
                    route_cost_estimate,
                    usdg_sizing_mode,
                )
                # This route's full coarse/refinement sweep is complete and
                # local_best is its highest absolute-net-profit candidate.
                # Execute it now instead of aging the quote on later routes.
                break

            if not best_route:
                print(f"[!] No profitable arb output found on Jup right now. Waiting for updates...")
                monitor.update_event.wait(timeout=5)
                monitor.update_event.clear()
                continue

            (
                selected_strategy,
                swap_size,
                selected_metrics,
                selected_cost_estimate,
                selected_usdg_sizing_mode,
            ) = best_route
            token = selected_strategy["token"]
            venue_order = selected_strategy["venue_order"]
            venue_label = "Stable->Jupiter" if venue_order == "stable_first" else "Jupiter->Stable"
            print(
                f"\n[*] Selected USDC route: USDC/{token} ({venue_label}) | "
                f"size {swap_size} | estimated net ${selected_metrics['net_profit_usd']:.6f} | "
                f"return {selected_metrics['net_return_bps']:.4f} bps"
            )
            if venue_order == "stable_first":
                route_label = f"USDC -> {token} (Stable) -> USDC (Jupiter)"
            else:
                route_label = f"USDC -> {token} (Jupiter) -> USDC (Stable)"

            if swap_size < 1:
                print(f"[!] Swap size too small ({swap_size})")
                monitor.update_event.wait(timeout=5)
                monitor.update_event.clear()
                continue

            print("[*] Revalidating selected size twice before taking first-leg exposure...")
            verify_failed = False
            verified_entry_quote = None
            verification_metrics = [selected_metrics]
            selected_usdg_drain_mode = selected_usdg_sizing_mode == "drain"
            selected_usdg_raw_sizing = selected_usdg_sizing_mode in {"drain", "partial"}
            probe_amount_raw = (
                int(round(swap_size * 10**DECIMALS))
                if selected_usdg_raw_sizing
                else int(swap_size) * 10**DECIMALS
            )
            for i in range(2):
                time.sleep(0.5)
                # Keep validation on the same executable quote path used for
                # the first-leg transaction; otherwise a generic quote can
                # pass and the taker-bound order can immediately fail.
                v_quote = get_jup_quote(
                    session,
                    selected_strategy["jup_input_mint"],
                    selected_strategy["jup_output_mint"],
                    probe_amount_raw,
                    taker=str(wallet),
                )
                if not v_quote:
                    print(f"    Verify {i+1}/2: quote unavailable")
                    verify_failed = True
                    break
                out_human = int(v_quote.get("outAmount", 0)) / 10**DECIMALS
                metrics = calculate_quote_metrics(swap_size, out_human, selected_cost_estimate)
                verification_metrics.append(metrics)
                pool_can_settle = stable_pool_can_settle(
                    venue_order,
                    swap_size,
                    out_human,
                    selected_strategy["stable_destination_pool"],
                    reserve=(
                        0
                        if selected_usdg_drain_mode
                        else usdg_drain_min_remainder_raw / 10**DECIMALS
                    ),
                )
                if pool_can_settle and is_profitable_candidate(
                    metrics,
                    MIN_NET_PROFIT_USD,
                    MIN_NET_RETURN_BPS,
                ):
                    # The second successful value replaces the first and can
                    # be submitted directly for a Jupiter-first route.
                    verified_entry_quote = v_quote
                    print(
                        f"    Verify {i+1}/2: net ${metrics['net_profit_usd']:.6f} "
                        f"({metrics['net_return_bps']:.4f} bps)"
                    )
                else:
                    reason = "Stable pool capacity" if not pool_can_settle else "profit threshold"
                    print(
                        f"    Verify {i+1}/2: rejected by {reason} | "
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

            if not monitor.is_ready():
                print(
                    "[wait] WebSocket account subscriptions are not ready; "
                    "not taking first-leg exposure."
                )
                monitor.ready_event.wait(timeout=1)
                if not monitor.is_ready():
                    continue

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
            token_ata = selected_strategy["token_ata"]
            balance_key = selected_strategy["balance_key"]
            try:
                intermediate_baseline_raw = get_token_balance(client, token_ata)
            except Exception:
                intermediate_baseline_raw = monitor.get(balance_key)

            arb_successful = False
            failure_note = ""
            tolerance_raw = POSITION_TOLERANCE_RAW

            if venue_order == "stable_first":
                state_store.set_status("executing_stable", "Executing Stable.com entry", route=route_label)
                print(f"[*] Stable.com entry: {swap_size} USDC -> {token}")
                if selected_usdg_drain_mode:
                    sync_remaining = monitor.seconds_until_increase_settled(
                        "pool_usdg",
                        STABLE_POOL_REFILL_SYNC_SECONDS,
                    )
                    if sync_remaining > 0:
                        print(
                            "[!] USDG refilled during quote validation; allowing Stable.com's "
                            f"order service another {sync_remaining:.1f}s to synchronize, then "
                            "rescanning with a fresh quote."
                        )
                        state_store.set_status("scanning", "Waiting for Stable.com pool sync")
                        continue
                    try:
                        live_usdg_pool_raw = get_token_balance(client, pool_usdg_ata)
                    except Exception as exc:
                        print(
                            "[!] Cannot fetch a fresh USDG pool balance immediately before entry; "
                            f"skipping the tightly bounded drain order: {exc}"
                        )
                        state_store.set_status("scanning", "Scanning markets")
                        continue
                    monitor.observe_balance("pool_usdg", live_usdg_pool_raw)
                    sync_remaining = monitor.seconds_until_increase_settled(
                        "pool_usdg",
                        STABLE_POOL_REFILL_SYNC_SECONDS,
                    )
                    if sync_remaining > 0:
                        print(
                            "[!] A fresh RPC balance exposed a USDG refill before entry; "
                            f"waiting {sync_remaining:.1f}s for Stable.com's order service."
                        )
                        state_store.set_status("scanning", "Waiting for Stable.com pool sync")
                        continue
                    selected_swap_raw = int(round(swap_size * 10**DECIMALS))
                    checked_usdg_remainder_raw = live_usdg_pool_raw - selected_swap_raw
                    if not drain_candidate_is_valid(
                        live_usdg_pool_raw,
                        selected_swap_raw,
                        dust_raw=usdg_drain_min_remainder_raw,
                        max_remainder_raw=USDG_MAX_REMAINDER_RAW,
                    ):
                        print(
                            "[!] USDG pool changed immediately before entry; order would leave "
                            f"{checked_usdg_remainder_raw / 10**DECIMALS:.6f} USDG, outside the safe "
                            f"{usdg_drain_min_remainder_raw / 10**DECIMALS:.6f}-"
                            f"{USDG_MAX_REMAINDER_USD:.6f} window. Skipping and rescanning."
                        )
                        state_store.set_status("scanning", "Scanning markets")
                        continue
                elif selected_usdg_sizing_mode == "partial":
                    try:
                        live_usdg_pool_raw = get_token_balance(client, pool_usdg_ata)
                    except Exception as exc:
                        print(
                            "[!] Cannot fetch a fresh USDG pool balance before the "
                            f"partial entry; skipping: {exc}"
                        )
                        state_store.set_status("scanning", "Scanning markets")
                        continue
                    live_safe_capacity_raw = max(
                        0,
                        live_usdg_pool_raw - usdg_drain_min_remainder_raw,
                    )
                    if probe_amount_raw > live_safe_capacity_raw:
                        print(
                            "[!] USDG pool decreased before entry; exact input "
                            f"{probe_amount_raw / 10**DECIMALS:.6f} exceeds fresh safe "
                            f"capacity {live_safe_capacity_raw / 10**DECIMALS:.6f}. "
                            "Skipping and resizing."
                        )
                        state_store.set_status("scanning", "Scanning markets")
                        continue
                elif token == "PYUSD":
                    try:
                        live_pyusd_pool_raw = get_token_balance(client, pool_pyusd_ata)
                    except Exception as exc:
                        print(
                            "[!] Cannot fetch a fresh PYUSD pool balance before entry; "
                            f"skipping: {exc}"
                        )
                        state_store.set_status("scanning", "Scanning markets")
                        continue
                    live_safe_capacity_raw = max(
                        0,
                        live_pyusd_pool_raw - usdg_drain_min_remainder_raw,
                    )
                    if probe_amount_raw > live_safe_capacity_raw:
                        print(
                            "[!] PYUSD pool decreased before entry; exact input "
                            f"{probe_amount_raw / 10**DECIMALS:.6f} exceeds fresh safe "
                            f"capacity {live_safe_capacity_raw / 10**DECIMALS:.6f}. "
                            "Skipping and resizing."
                        )
                        state_store.set_status("scanning", "Scanning markets")
                        continue
                entry_cursor = monitor.snapshot([balance_key, "user_usdc"])
                stable_result = execute_stable_swap(
                    session,
                    client,
                    keypair,
                    "USDC",
                    token,
                    swap_size,
                    pending_store=state_store,
                    submission_label=f"Stable.com USDC->{token} entry",
                )
                selected_route_key = (token, venue_order)
                if not stable_result.may_have_landed:
                    failure_note = "Stable.com entry failed"
                    reserve_constraint = stable_result.reserve_constraint
                    liquidity_constraint = stable_result.liquidity_constraint
                    if selected_usdg_drain_mode and reserve_constraint:
                        adjusted_min_raw = adjusted_drain_minimum_raw(
                            usdg_drain_min_remainder_raw,
                            USDG_MAX_REMAINDER_RAW,
                            reserve_constraint,
                            safety_buffer_raw=USDG_DRAIN_SAFETY_BUFFER_RAW,
                            checked_remainder_raw=checked_usdg_remainder_raw,
                        )
                        if adjusted_min_raw is not None:
                            usdg_drain_min_remainder_raw = adjusted_min_raw

                        failure_count = stable_reserve_failure_counts.get(selected_route_key, 0) + 1
                        stable_reserve_failure_counts[selected_route_key] = failure_count
                        backoff_seconds = min(
                            300,
                            30 * (2 ** min(failure_count - 1, 4)),
                        )
                        if adjusted_min_raw is None:
                            backoff_seconds = 300
                        stable_reserve_backoff_until[selected_route_key] = (
                            time.monotonic() + backoff_seconds
                        )

                        required_raw = reserve_constraint["required_raw"]
                        if adjusted_min_raw is None:
                            adjustment_text = "no safe remainder remains below the refill trigger"
                        else:
                            adjustment_text = (
                                "runtime minimum is now "
                                f"{adjusted_min_raw / 10**DECIMALS:.6f} USDG"
                            )
                        failure_note = (
                            "Stable.com USDG reserve rejected the entry; "
                            f"requires {required_raw / 10**DECIMALS:.6f} USDG, "
                            f"{adjustment_text}; backing off {backoff_seconds}s"
                        )
                        print(f"[!] {failure_note}")
                    elif selected_usdg_drain_mode and liquidity_constraint:
                        failure_count = stable_reserve_failure_counts.get(selected_route_key, 0) + 1
                        stable_reserve_failure_counts[selected_route_key] = failure_count
                        backoff_seconds = min(
                            30.0,
                            max(1.0, STABLE_BACKEND_LAG_RETRY_SECONDS)
                            * (2 ** min(failure_count - 1, 3)),
                        )
                        stable_reserve_backoff_until[selected_route_key] = (
                            time.monotonic() + backoff_seconds
                        )
                        chain_available_raw = max(
                            0,
                            live_usdg_pool_raw - usdg_drain_min_remainder_raw,
                        )
                        backend_available_raw = liquidity_constraint["available_raw"]
                        if (
                            backend_available_raw + USDG_DRAIN_SAFETY_BUFFER_RAW
                            < chain_available_raw
                        ):
                            availability_reason = (
                                "order service trails the confirmed on-chain pool"
                            )
                        else:
                            availability_reason = (
                                "order service reports liquidity as unavailable or reserved"
                            )
                        failure_note = (
                            f"Stable.com {availability_reason} (backend available "
                            f"{backend_available_raw / 10**DECIMALS:.6f}, on-chain capacity "
                            f"{chain_available_raw / 10**DECIMALS:.6f}); "
                            f"backing off {backoff_seconds:.0f}s without resubmitting"
                        )
                        print(f"[!] {failure_note}")
                    elif reserve_constraint:
                        failure_count = stable_reserve_failure_counts.get(
                            selected_route_key, 0
                        ) + 1
                        stable_reserve_failure_counts[selected_route_key] = failure_count
                        backoff_seconds = min(
                            300,
                            30 * (2 ** min(failure_count - 1, 4)),
                        )
                        stable_reserve_backoff_until[selected_route_key] = (
                            time.monotonic() + backoff_seconds
                        )
                        required_raw = reserve_constraint["required_raw"]
                        failure_note = (
                            f"Stable.com {token} reserve rejected the entry; requires "
                            f"{required_raw / 10**DECIMALS:.6f} remaining; backing off "
                            f"{backoff_seconds}s before resizing"
                        )
                        print(f"[!] {failure_note}")
                    else:
                        stable_reserve_failure_counts.pop(selected_route_key, None)
                        stable_reserve_backoff_until.pop(selected_route_key, None)
                else:
                    stable_reserve_failure_counts.pop(selected_route_key, None)
                    stable_reserve_backoff_until.pop(selected_route_key, None)
                    state_store.set_status("exposed", f"Holding {token}; preparing Jupiter exit", route=route_label)
                    entry_confirmed, entry_balances, _ = confirm_transfer_ws_first(
                        client,
                        monitor,
                        {
                            balance_key: (
                                token_ata,
                                lambda balance: balance > intermediate_baseline_raw,
                            ),
                            "user_usdc": (
                                usdc_ata,
                                lambda balance: balance < attempt_before["usdc_raw"],
                            ),
                        },
                        entry_cursor.revisions,
                        f"Stable.com {token} entry",
                        submission=stable_result.submission,
                        pending_store=state_store,
                    )
                    intermediate_after_raw = entry_balances.get(
                        balance_key,
                        intermediate_baseline_raw,
                    )
                    received_raw = acquired_balance_delta(intermediate_after_raw, intermediate_baseline_raw)
                    if not entry_confirmed or received_raw <= 0:
                        failure_note = f"{token} entry balance delta was not observed"
                    else:
                        entry_usdc_after_raw = entry_balances["user_usdc"]
                        print(f"[+] Stable.com produced {received_raw / 10**DECIMALS:.6f} {token}")
                        remaining_raw = received_raw
                        for exit_attempt in range(1, 11):
                            if remaining_raw <= tolerance_raw:
                                arb_successful = True
                                break

                            final_quote = get_jup_quote(
                                session,
                                selected_strategy["jup_input_mint"],
                                selected_strategy["jup_output_mint"],
                                remaining_raw,
                                taker=str(wallet),
                            )
                            if not final_quote:
                                print(f"[!] Jupiter exit quote unavailable ({exit_attempt}/10)")
                                time.sleep(2)
                                continue

                            expected_out = int(final_quote["outAmount"]) / 10**DECIMALS
                            in_human = remaining_raw / 10**DECIMALS
                            if expected_out <= in_human:
                                print(
                                    f"[-] Jupiter exit below parity ({exit_attempt}/10): "
                                    f"{in_human:.6f} -> {expected_out:.6f}; waiting"
                                )
                                time.sleep(3)
                                continue

                            state_store.set_status("executing_jupiter", "Executing Jupiter exit", route=route_label)
                            print(f"[*] Jupiter exit attempt {exit_attempt}: {in_human:.6f} {token} -> USDC")
                            exit_cursor = monitor.snapshot([balance_key, "user_usdc"])
                            # The confirmed post-entry balance is the correct
                            # credit baseline even if the monitor missed or is
                            # still delivering an entry notification.
                            exit_usdc_before_raw = entry_usdc_after_raw
                            submission = execute_jup_swap(
                                session,
                                client,
                                keypair,
                                final_quote,
                                pending_store=state_store,
                                submission_label=f"Jupiter {token}->USDC exit",
                            )
                            if not submission.may_have_landed:
                                print(f"[!] Jupiter exit submission failed ({exit_attempt}/10)")
                                time.sleep(2)
                                continue

                            exit_confirmed, _, exit_confirmation_source = confirm_transfer_ws_first(
                                client,
                                monitor,
                                {
                                    balance_key: (
                                        token_ata,
                                        lambda balance: acquired_delta_is_cleared(
                                            balance,
                                            intermediate_baseline_raw,
                                            tolerance_raw,
                                        ),
                                    ),
                                    "user_usdc": (
                                        usdc_ata,
                                        lambda balance: balance > exit_usdc_before_raw,
                                    ),
                                },
                                exit_cursor.revisions,
                                f"Jupiter {token}->USDC exit",
                                submission=submission,
                                pending_store=state_store,
                            )
                            if exit_confirmed:
                                arb_successful = True
                                break
                            if exit_confirmation_source in {"signature_error", "expired"}:
                                print(
                                    "[!] Jupiter exit transaction definitively failed or expired; "
                                    "it is safe to request a fresh quote without duplicating it."
                                )
                                time.sleep(1)
                                continue
                            failure_note = (
                                f"Jupiter exit was submitted, but fresh {token} clearance "
                                "and USDC credit were not observed; not resubmitting blindly"
                            )
                            break

                        if not arb_successful:
                            failure_note = failure_note or (
                                f"Jupiter exit did not clear the acquired {token} delta"
                            )

            else:
                state_store.set_status("executing_jupiter", "Executing Jupiter entry", route=route_label)
                final_quote = verified_entry_quote
                if not final_quote:
                    failure_note = "Verified Jupiter entry order unavailable"
                    print(
                        f"[skip] Jupiter entry {swap_size} USDC->{token}: "
                        "second verified executable order was unavailable"
                    )
                else:
                    print("[*] Reusing verification 2/2 executable order for Jupiter entry.")
                    entry_out_raw = int(final_quote.get("outAmount", 0))
                    try:
                        live_pool_usdc_raw = get_token_balance(client, pool_usdc_ata)
                    except Exception:
                        live_pool_usdc_raw = monitor.get("pool_usdc")
                    stable_capacity_raw = max(0, live_pool_usdc_raw - 10**DECIMALS)
                    live_metrics = calculate_quote_metrics(
                        swap_size,
                        entry_out_raw / 10**DECIMALS,
                        selected_cost_estimate,
                    )
                    if not is_profitable_candidate(live_metrics, MIN_NET_PROFIT_USD, MIN_NET_RETURN_BPS):
                        failure_note = (
                            "Jupiter entry fell below the net-profit threshold "
                            f"(net ${live_metrics['net_profit_usd']:.6f}, "
                            f"return {live_metrics['net_return_bps']:.4f} bps)"
                        )
                        print(
                            f"[skip] Jupiter entry {swap_size} USDC->{token}: {failure_note}; "
                            f"requires net >= ${MIN_NET_PROFIT_USD:.6f} and return >= "
                            f"{MIN_NET_RETURN_BPS:.4f} bps"
                        )
                    elif entry_out_raw > stable_capacity_raw:
                        failure_note = (
                            "Stable.com USDC pool cannot settle the Jupiter output "
                            f"({entry_out_raw / 10**DECIMALS:.6f} {token} required, "
                            f"{stable_capacity_raw / 10**DECIMALS:.6f} available)"
                        )
                        print(f"[skip] Jupiter entry {swap_size} USDC->{token}: {failure_note}")
                    else:
                        print(f"[*] Jupiter entry: {swap_size} USDC -> {token}")
                        entry_cursor = monitor.snapshot([balance_key, "user_usdc"])
                        submission = execute_jup_swap(
                            session,
                            client,
                            keypair,
                            final_quote,
                            pending_store=state_store,
                            submission_label=f"Jupiter USDC->{token} entry",
                        )
                        if not submission.may_have_landed:
                            failure_note = "Jupiter entry submission failed"
                        else:
                            entry_confirmed, entry_balances, _ = confirm_transfer_ws_first(
                                client,
                                monitor,
                                {
                                    balance_key: (
                                        token_ata,
                                        lambda balance: balance > intermediate_baseline_raw,
                                    ),
                                    "user_usdc": (
                                        usdc_ata,
                                        lambda balance: balance < attempt_before["usdc_raw"],
                                    ),
                                },
                                entry_cursor.revisions,
                                f"Jupiter USDC->{token} entry",
                                submission=submission,
                                pending_store=state_store,
                            )
                            intermediate_after_raw = entry_balances.get(
                                balance_key,
                                intermediate_baseline_raw,
                            )
                            received_raw = acquired_balance_delta(
                                intermediate_after_raw,
                                intermediate_baseline_raw,
                            )
                            if not entry_confirmed or received_raw <= 0:
                                failure_note = f"Jupiter entry did not produce a {token} balance delta"
                            else:
                                try:
                                    live_pool_usdc_raw = get_token_balance(client, pool_usdc_ata)
                                except Exception:
                                    live_pool_usdc_raw = monitor.get("pool_usdc")
                                stable_capacity_raw = max(0, live_pool_usdc_raw - 10**DECIMALS)
                                if received_raw > stable_capacity_raw:
                                    failure_note = "Stable.com USDC pool changed and cannot settle the received amount"
                                else:
                                    state_store.set_status("executing_stable", "Executing Stable.com exit", route=route_label)
                                    received_human = received_raw / 10**DECIMALS
                                    print(f"[*] Stable.com exit: {received_human:.6f} {token} -> USDC")
                                    exit_cursor = monitor.snapshot([balance_key, "user_usdc"])
                                    # Use the confirmed entry result, not a
                                    # potentially lagging WS cache, as the
                                    # starting point for the USDC credit.
                                    exit_usdc_before_raw = entry_balances["user_usdc"]
                                    stable_exit_result = execute_stable_swap(
                                        session,
                                        client,
                                        keypair,
                                        token,
                                        "USDC",
                                        received_human,
                                        pending_store=state_store,
                                        submission_label=f"Stable.com {token}->USDC exit",
                                    )
                                    if not stable_exit_result.may_have_landed:
                                        failure_note = "Stable.com exit failed after Jupiter entry"
                                    else:
                                        exit_confirmed, _, _ = confirm_transfer_ws_first(
                                            client,
                                            monitor,
                                            {
                                                balance_key: (
                                                    token_ata,
                                                    lambda balance: acquired_delta_is_cleared(
                                                        balance,
                                                        intermediate_baseline_raw,
                                                        tolerance_raw,
                                                    ),
                                                ),
                                                "user_usdc": (
                                                    usdc_ata,
                                                    lambda balance: balance > exit_usdc_before_raw,
                                                ),
                                            },
                                            exit_cursor.revisions,
                                            f"Stable.com {token}->USDC exit",
                                            submission=stable_exit_result.submission,
                                            pending_store=state_store,
                                        )
                                        if exit_confirmed:
                                            arb_successful = True
                                        else:
                                            failure_note = (
                                                "Stable.com exit was submitted, but fresh token "
                                                "clearance and USDC credit were not observed"
                                            )

            new_port = print_portfolio(session, client, wallet, usdc_ata, usdg_ata, pyusd_ata, "[*] AFTER ARB", prev=prev_port)
            note = "" if arb_successful else (failure_note or f"{token} cycle was not confirmed")
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
                try:
                    unresolved_raw = acquired_balance_delta(
                        get_token_balance(client, token_ata),
                        intermediate_baseline_raw,
                    )
                except Exception:
                    unresolved_raw = acquired_balance_delta(
                        monitor.get(balance_key),
                        intermediate_baseline_raw,
                    )
                if unresolved_raw > tolerance_raw:
                    plan = recovery_store.schedule(
                        token,
                        unresolved_raw,
                        RECOVERY_MIN_NET_PROFIT_USD,
                        f"Jupiter {token}->USDC recovery",
                        note,
                    )
                    print(
                        f"[recovery] Scheduled exact {unresolved_raw / 10**DECIMALS:.6f} {token} "
                        f"return when net profit reaches ${plan['min_net_profit_usd']:.2f}."
                    )
                    state_store.set_status(
                        "recovering",
                        f"Recovery worker is monitoring {token}",
                        route=route_label,
                        error=note,
                    )
                else:
                    state_store.set_status("error", "Arbitrage attempt failed", route=route_label, error=note)
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
