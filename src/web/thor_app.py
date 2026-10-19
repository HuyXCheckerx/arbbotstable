from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=True)

# Add paths
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

WEB_DIR = PROJECT_ROOT / "src" / "web"
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_PATH = WEB_DIR / "templates" / "thor_dashboard.html"
FAVICON_PATH = WEB_DIR / "favicon.svg"
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

SNIPER_LOG_PATH = LOGS_DIR / "thor_sniper.log"
SNIPER_PID_PATH = LOGS_DIR / "thor_sniper.pid"
TRANSACTIONS_PATH = LOGS_DIR / "thor_transactions.json"

from src.engines.thorchain_usdp_sniper import (
    RESTING_INTERVAL,
    RESTING_QUANTITY,
    ThorClient,
    fetch_pool_spot_rate,
    memo_stream_fields,
    parse_quote,
    slice_fill_rate,
    DEFAULT_THORNODE_BASE,
    THORCHAIN_SCALE,
    USDC_ASSET,
    USDP_ASSET,
    USDC_CONTRACT,
    USDP_CONTRACT,
)

ROUTER_V4 = "0xD37BbE5744D730a1d98d8DC97c42F0Ca46aD7146"
KNOWN_ACTIVE_TX = "0x3dfaf115a61e469fccb987a7977d32326e32edc5683f26b16775c6da826f0610"

# A one-off swap quotes, may wait up to 120s for an approval receipt, then up to
# 180s for the deposit receipt. The process is never killed when this runs out.
SWAP_WAIT_SECONDS = 420
DEPOSIT_HASH_RE = re.compile(r"Deposit transaction broadcasted! Hash: (0x[0-9a-fA-F]{64})")
PROTECTED_MEMO_RE = re.compile(r"Protected Memo: (\S+)")
# The sniper may send less than requested: it sizes deposits to what the pool can absorb.
DEPOSIT_AMOUNT_RE = re.compile(r"Amount: \d+ atomic units \(([\d.]+) USDC\)")

thor_client = ThorClient(base_url=DEFAULT_THORNODE_BASE, timeout=8.0)
status_pool = ThreadPoolExecutor(max_workers=6)

# Sniper processes spawned by this server, kept so exited ones get reaped
# instead of lingering as zombies that still answer os.kill(pid, 0).
children: dict[int, subprocess.Popen] = {}
swap_lock = threading.Lock()
swap_proc: subprocess.Popen | None = None
# Where swap_proc's output starts in the sniper log, and which mode it runs.
swap_meta: dict[str, Any] = {}
# Sniper log lines a resting order writes while it waits, and once it starts placing.
RESTING_WAITING_MARK = "Resting order waiting:"
RESTING_PLACING_MARK = "Placing resting order"


def get_web3_instance():
    try:
        from web3 import Web3
        rpc_url = os.getenv("ETH_RPC_URL", "https://ethereum.publicnode.com").strip()
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 6.0}))
        return w3
    except Exception:
        return None


def fetch_thornode(path: str, params: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
    return thor_client.get(path, params=params)


def get_operator_address() -> str:
    # Same key the sniper signs with, so balances shown match the executing wallet.
    key = os.getenv("ETH_OPERATOR_PRIVATE_KEY") or os.getenv("ETH_PRIVATE_KEY") or os.getenv("OPERATOR_PRIVATE_KEY")
    if key:
        try:
            from eth_account import Account
            return Account.from_key(key).address
        except Exception:
            pass
    return "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"


def read_wallet_balances() -> dict[str, Any]:
    operator = get_operator_address()
    result = {
        "operator": operator,
        "eth_balance": "0.0000",
        "usdc_balance": "0.00",
        "usdp_balance": "0.00",
        "usdc_allowance": "0.00",
        "router": ROUTER_V4,
        "rpc_connected": False,
    }
    w3 = get_web3_instance()
    if not w3 or not w3.is_connected():
        return result

    erc20_abi = [
        {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
        {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"},
    ]
    try:
        eth_bal = w3.eth.get_balance(operator) / 1e18
        result["eth_balance"] = f"{eth_bal:.4f}"
        result["rpc_connected"] = True

        usdc_contract = w3.eth.contract(address=w3.to_checksum_address(USDC_CONTRACT), abi=erc20_abi)
        usdp_contract = w3.eth.contract(address=w3.to_checksum_address(USDP_CONTRACT), abi=erc20_abi)

        usdc_bal = usdc_contract.functions.balanceOf(operator).call() / 1e6
        usdp_bal = usdp_contract.functions.balanceOf(operator).call() / 1e18
        allowance = usdc_contract.functions.allowance(operator, w3.to_checksum_address(ROUTER_V4)).call() / 1e6

        result["usdc_balance"] = f"{usdc_bal:.2f}"
        result["usdp_balance"] = f"{usdp_bal:.4f}"
        result["usdc_allowance"] = f"{allowance:.2f}"
    except Exception as exc:
        result["error"] = str(exc)
    return result


def get_deposit_tx_info(tx_hash: str | None = None) -> dict[str, Any]:
    """Retrieve complete, institutional-grade information for any THORChain deposit transaction.
    
    Parses both active streaming swaps from /swaps/streaming and finalized/refunded
    outbounds from /tx/status/{hash}.
    """
    search_hash = (tx_hash or KNOWN_ACTIVE_TX).strip()
    if not search_hash:
        return {"ok": False, "found": False, "error": "Empty transaction hash provided"}

    clean_hash = search_hash[2:].upper() if (search_hash.startswith("0x") or search_hash.startswith("0X")) else search_hash.upper()
    formatted_hash = "0x" + clean_hash.lower()

    # 1. Check swaps/streaming
    match = None
    try:
        streaming_list = fetch_thornode("swaps/streaming", timeout=6.0)
        if isinstance(streaming_list, list):
            for item in streaming_list:
                item_tx = str(item.get("tx_id", "")).upper()
                if item_tx == clean_hash or clean_hash in item_tx:
                    match = item
                    break
    except Exception:
        pass

    # 2. Check tx/status
    status_data = {}
    try:
        status_data = fetch_thornode(f"tx/status/{clean_hash}", timeout=6.0)
        if not isinstance(status_data, dict):
            status_data = {}
    except Exception as exc:
        status_data = {"error": str(exc)}

    has_tx = bool(status_data.get("tx"))
    has_stages = bool(status_data.get("stages"))
    if not match and not has_tx and not has_stages:
        return {
            "ok": False,
            "found": False,
            "tx_id": formatted_hash,
            "status": "NOT_FOUND",
            "message": f"Transaction {formatted_hash} not yet indexed on THORNode. If recently submitted, allow 1-2 Ethereum blocks.",
        }

    # Inbound details
    tx_in = status_data.get("tx", {})
    in_coins = tx_in.get("coins", [])
    in_amount = 0.0
    in_asset = "ETH.USDC"
    if in_coins:
        c = in_coins[0]
        in_asset = c.get("asset", "ETH.USDC")
        in_amount = int(c.get("amount", 0)) / THORCHAIN_SCALE

    # Outbound details
    out_usdp = 0.0
    outbound_tx_hash = None
    refund_usdc = 0.0
    refund_tx_hash = None
    affiliate_rune = 0.0
    affiliate_collector = None
    outbound_list = []

    for out in status_data.get("out_txs", []):
        memo = out.get("memo", "")
        coins = out.get("coins", [])
        if not coins:
            continue
        c = coins[0]
        asset = c.get("asset", "")
        raw_amt = int(c.get("amount", 0))
        tx_id = out.get("id", "")
        tx_id_fmt = ("0x" + tx_id.lower()) if ("0000000000" not in tx_id and tx_id) else tx_id
        parsed_out = {
            "asset": asset,
            "amount": raw_amt / THORCHAIN_SCALE,
            "tx_id": tx_id_fmt,
            "to_address": out.get("to_address"),
            "memo": memo,
        }
        outbound_list.append(parsed_out)

        if "USDP" in asset:
            out_usdp += raw_amt / THORCHAIN_SCALE
            if not outbound_tx_hash and "0000000000" not in tx_id:
                outbound_tx_hash = tx_id_fmt
        elif "USDC" in asset and ("REFUND" in memo or raw_amt > 100000000):
            refund_usdc += raw_amt / THORCHAIN_SCALE
            if not refund_tx_hash and "0000000000" not in tx_id:
                refund_tx_hash = tx_id_fmt
        elif "RUNE" in asset:
            affiliate_rune += raw_amt / THORCHAIN_SCALE
            affiliate_collector = out.get("to_address")

    stages = status_data.get("stages", {})
    swap_finalised = stages.get("swap_finalised", {}).get("completed", False)
    outbound_signed = stages.get("outbound_signed", {}).get("completed", False)

    is_active = (match is not None) or (stages.get("swap_status", {}).get("pending", False) and not swap_finalised)

    # Status classification
    if is_active:
        status_label = "STREAMING"
    elif swap_finalised or outbound_signed:
        if refund_usdc > 0 and out_usdp > 0:
            status_label = "COMPLETED_PARTIAL"
        elif refund_usdc > 0 and out_usdp == 0:
            status_label = "REFUNDED"
        else:
            status_label = "COMPLETED"
    elif stages.get("inbound_observed", {}).get("completed", False):
        status_label = "INBOUND_CONFIRMING"
    else:
        status_label = "PENDING"

    # Streaming stats
    streaming_info = None
    if match:
        quantity = int(match.get("quantity", 0))
        count = int(match.get("count", 0))
        failed_swaps = match.get("failed_swaps", [])
        failed_count = len(failed_swaps)
        success_count = max(0, count - failed_count)
        deposit = int(match.get("deposit", 0)) / THORCHAIN_SCALE
        in_amt = int(match.get("in", 0)) / THORCHAIN_SCALE
        out_amt = int(match.get("out", 0)) / THORCHAIN_SCALE
        target_limit = int(match.get("trade_target", 0)) / THORCHAIN_SCALE
        remaining_usdc = max(0.0, deposit - in_amt)

        progress_pct = (count / quantity * 100.0) if quantity > 0 else 0.0
        effective_rate = (out_amt / in_amt) if in_amt > 0 else 0.0
        profit_bps = ((effective_rate - 1.0) * 10000.0) if effective_rate > 0 else 0.0
        blocks_left = max(0, quantity - count)
        est_seconds_left = blocks_left * 6

        reasons = match.get("failed_swap_reasons", [])
        latest_reason = reasons[-1] if reasons else "Sub-swaps executing smoothly"

        streaming_info = {
            "quantity": quantity,
            "count": count,
            "progress_pct": round(progress_pct, 1),
            "success_count": success_count,
            "skipped_count": failed_count,
            "deposit_usdc": deposit,
            "swapped_usdc": round(in_amt, 4),
            "remaining_usdc": round(remaining_usdc, 4),
            "out_usdp": round(out_amt, 4),
            "effective_rate": round(effective_rate, 6),
            "profit_bps": round(profit_bps, 2),
            "target_limit": round(target_limit, 4),
            "blocks_left": blocks_left,
            "est_seconds_left": est_seconds_left,
            "latest_reason": latest_reason,
            "recent_reasons": reasons[-10:] if reasons else [],
            "destination": match.get("destination"),
            "initial_height": match.get("initial_height"),
            "last_height": match.get("last_height"),
        }

    # Reconciled capital
    swapped_usdc = in_amount - refund_usdc if in_amount > 0 else 0.0
    effective_rate = (out_usdp / swapped_usdc) if swapped_usdc > 0 else 0.0
    net_profit = out_usdp - swapped_usdc if swapped_usdc > 0 else 0.0
    net_profit_bps = (net_profit / swapped_usdc * 10000) if swapped_usdc > 0 else 0.0

    refund_reason = None
    if refund_usdc > 0:
        try:
            midgard_url = f"https://gateway.liquify.com/chain/thorchain_midgard/v2/actions?txid={clean_hash}"
            mreq = urllib.request.Request(midgard_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(mreq, timeout=3.5) as mresp:
                mdata = json.loads(mresp.read().decode())
                for act in mdata.get("actions", []):
                    rr = act.get("metadata", {}).get("refund", {}).get("reason")
                    if rr:
                        refund_reason = rr
                        break
        except Exception:
            pass

    return {
        "ok": True,
        "found": True,
        "tx_id": formatted_hash,
        "status": status_label,
        "is_active": is_active,
        "is_completed": swap_finalised and outbound_signed,
        "progress_pct": streaming_info["progress_pct"] if streaming_info else (100.0 if swap_finalised else 50.0),
        "inbound": {
            "tx_hash": formatted_hash,
            "asset": in_asset,
            "amount": in_amount,
            "from_address": tx_in.get("from_address"),
            "to_address": tx_in.get("to_address"),
            "memo": tx_in.get("memo"),
        },
        "streaming": streaming_info,
        "settlement": {
            "out_usdp": round(out_usdp, 4),
            "outbound_tx_hash": outbound_tx_hash,
            "refund_usdc": round(refund_usdc, 4),
            "refund_tx_hash": refund_tx_hash,
            "refund_reason": refund_reason,
            "swapped_usdc": round(swapped_usdc, 4),
            "effective_rate": round(effective_rate, 6),
            "net_profit_usdp": round(net_profit, 4),
            "net_profit_bps": round(net_profit_bps, 2),
            "affiliate_rune": round(affiliate_rune, 6),
            "affiliate_collector": affiliate_collector,
            "all_out_txs": outbound_list,
        },
        "stages": stages,
    }


def get_active_streaming_swap(tx_hash: str | None = None, info: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Backward-compatible helper returning stream inspector fields."""
    if info is None:
        info = get_deposit_tx_info(tx_hash)
    if not info.get("found"):
        return None

    # Merge top-level streaming fields for backward compatibility
    res = dict(info)
    res["active"] = info.get("is_active", False)
    if info.get("streaming"):
        res.update(info["streaming"])
    else:
        # Fallback fields for completed/refunded swaps
        settlement = info.get("settlement", {})
        res["quantity"] = 164
        res["count"] = 164
        res["progress_pct"] = 100.0
        res["deposit_usdc"] = info.get("inbound", {}).get("amount", 10000.0)
        res["swapped_usdc"] = settlement.get("swapped_usdc", 0.0)
        res["remaining_usdc"] = 0.0
        res["out_usdp"] = settlement.get("out_usdp", 0.0)
        res["effective_rate"] = settlement.get("effective_rate", 0.0)
        res["profit_bps"] = settlement.get("net_profit_bps", 0.0)
        res["success_count"] = 1 if settlement.get("out_usdp", 0) > 0 else 0
        res["skipped_count"] = 0
        res["blocks_left"] = 0
        res["est_seconds_left"] = 0
        if settlement.get("refund_reason"):
            res["latest_reason"] = f"Refunded by THORChain: {settlement['refund_reason']}"
        else:
            res["latest_reason"] = (
                f"Settled: Delivered {settlement.get('out_usdp')} USDP, refunded {settlement.get('refund_usdc')} USDC"
            )
    return res


def get_pool_depths() -> dict[str, Any]:
    result = {"usdc_depth": 0.0, "usdp_depth": 0.0, "spot_rate": 0.0}
    try:
        usdc_pool = fetch_thornode(f"pool/{USDC_ASSET}", timeout=5.0)
        usdp_pool = fetch_thornode(f"pool/{USDP_ASSET}", timeout=5.0)
        usdc_rune = int(usdc_pool.get("balance_rune", 0))
        usdc_asset = int(usdc_pool.get("balance_asset", 0))
        usdp_rune = int(usdp_pool.get("balance_rune", 0))
        usdp_asset = int(usdp_pool.get("balance_asset", 0))

        if usdc_asset > 0 and usdp_asset > 0 and usdp_rune > 0:
            rune_per_usdc = usdc_rune / usdc_asset
            rune_per_usdp = usdp_rune / usdp_asset
            spot_rate = rune_per_usdc / rune_per_usdp
            result["usdc_depth"] = round(usdc_asset / THORCHAIN_SCALE, 2)
            result["usdp_depth"] = round(usdp_asset / THORCHAIN_SCALE, 2)
            result["spot_rate"] = round(spot_rate, 5)
    except Exception:
        pass
    return result


def reap_children() -> None:
    for pid, proc in list(children.items()):
        if proc.poll() is not None:
            del children[pid]


def sniper_daemon_pids() -> list[int]:
    """PIDs of live thor_usdp_sniper.py daemons; one-off --once/--resting swaps are excluded."""
    try:
        out = subprocess.check_output(["ps", "-A", "-o", "pid=,stat=,args="], text=True)
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3 or fields[1].startswith("Z"):
            continue
        argv = fields[2].split()
        if any(a.endswith("thor_usdp_sniper.py") for a in argv) and not {"--once", "--resting"} & set(argv):
            pids.append(int(fields[0]))
    return pids


def is_sniper_process_running() -> tuple[bool, int | None]:
    reap_children()
    pids = sniper_daemon_pids()
    if SNIPER_PID_PATH.exists():
        try:
            pid = int(SNIPER_PID_PATH.read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid in pids:
            return True, pid
        SNIPER_PID_PATH.unlink(missing_ok=True)
    if pids:
        return True, pids[0]
    return False, None


def sniper_python() -> str:
    venv_bin = PROJECT_ROOT / ".venv" / "bin" / "python3"
    return str(venv_bin) if venv_bin.exists() else sys.executable


def spawn_sniper(args: list[str]) -> subprocess.Popen:
    """Start thor_usdp_sniper.py detached, appending its output to the sniper log."""
    cmd = [sniper_python(), str(PROJECT_ROOT / "thor_usdp_sniper.py"), *args]
    # The python is already chosen; stop the launcher re-exec'ing into a second process.
    env = {**os.environ, "_ARBBOT_VENV_WRAPPED": "1"}
    with open(SNIPER_LOG_PATH, "a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    children[proc.pid] = proc
    return proc


def read_log_since(offset: int) -> str:
    with open(SNIPER_LOG_PATH, "r", encoding="utf-8", errors="replace") as log_file:
        log_file.seek(offset)
        return log_file.read()


def settle_swap(
    output: str, returncode: int | None, amount: str, resting: bool, pid: int, record: bool
) -> tuple[int, dict[str, Any]]:
    """The API reply for a swap process, from its log output; optionally record its deposit."""
    tail = output[-800:]
    hash_match = DEPOSIT_HASH_RE.search(output)
    tx_hash = hash_match.group(1) if hash_match else None

    if tx_hash:
        confirmed = returncode == 0 and "Deposit confirmed in block" in output
        memo_match = PROTECTED_MEMO_RE.search(output)
        amount_match = DEPOSIT_AMOUNT_RE.search(output)
        deposited = amount_match.group(1) if amount_match else amount
        if record:
            record_transaction({
                "tx_hash": tx_hash,
                "amount": deposited,
                "asset_in": "USDC",
                "asset_out": "USDP",
                "timestamp": int(time.time()),
                "memo": memo_match.group(1) if memo_match else None,
                "mode": "resting" if resting else "instant",
                "status": "submitted" if confirmed else ("pending" if returncode is None else "failed"),
            })
        if confirmed:
            return 200, {
                "ok": True,
                "tx_hash": tx_hash,
                "deposited": deposited,
                "output": tail,
                "message": (
                    f"Resting order of {deposited} USDC placed. THORChain will try one slice every {RESTING_INTERVAL} blocks for ~24h; track it in the inspector."
                    if resting
                    else f"Deposit of {deposited} USDC confirmed on Ethereum. THORChain is now streaming the swap; track it in the inspector."
                ),
            }
        if returncode is None:
            return 200, {
                "ok": True,
                "pending": True,
                "tx_hash": tx_hash,
                "output": tail,
                "message": f"Deposit broadcast but not confirmed after {SWAP_WAIT_SECONDS}s; still waiting in the background (PID {pid}).",
            }
        return 400, {"ok": False, "tx_hash": tx_hash, "error": f"Deposit {tx_hash} failed:\n{tail}"}
    if returncode is None:
        return 504, {
            "ok": False,
            "error": f"Swap process (PID {pid}) has not broadcast a deposit after {SWAP_WAIT_SECONDS}s; check the terminal log.",
        }
    if returncode == 0:
        reason = "the quote failed" if resting else "no deposit size clears the profit check right now"
        return 400, {"ok": False, "error": f"No swap sent: {reason}.\n{tail}"}
    return 400, {"ok": False, "error": tail or f"Swap process exited with code {returncode}"}


def watch_swap(proc: subprocess.Popen, log_start: int, amount: str, resting: bool) -> None:
    """Record the deposit of a swap that outlived its HTTP request, once its process exits."""
    returncode = proc.wait()
    settle_swap(read_log_since(log_start), returncode, amount, resting, proc.pid, record=True)


def pending_swap_status() -> dict[str, Any] | None:
    """The manual or resting swap process still running, if any, and whether it is still waiting."""
    if swap_proc is None or swap_proc.poll() is not None:
        return None
    output = read_log_since(swap_meta.get("log_start", 0))
    resting = swap_meta.get("mode") == "resting"
    waits = [line.split(RESTING_WAITING_MARK, 1)[1].strip() for line in output.splitlines() if RESTING_WAITING_MARK in line]
    return {
        "pid": swap_proc.pid,
        "mode": swap_meta.get("mode"),
        "state": "waiting" if resting and RESTING_PLACING_MARK not in output else "placing",
        "detail": waits[-1] if waits else None,
    }


def record_transaction(entry: dict[str, Any]) -> None:
    try:
        txs = json.loads(TRANSACTIONS_PATH.read_text()) if TRANSACTIONS_PATH.exists() else []
        txs.insert(0, entry)
        TRANSACTIONS_PATH.write_text(json.dumps(txs[:50], indent=2))
    except Exception:
        pass


def read_recent_logs(lines: int = 100) -> list[str]:
    log_lines = []
    if SNIPER_LOG_PATH.exists():
        try:
            with open(SNIPER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                log_lines = [line.rstrip() for line in f.readlines()[-lines:]]
        except Exception as exc:
            log_lines = [f"Error reading logs: {exc}"]

    if not log_lines:
        # Provide real-time status telemetry
        running, pid = is_sniper_process_running()
        swap = get_active_streaming_swap()
        pools = get_pool_depths()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        status_text = f"RUNNING (PID: {pid})" if running else "STANDBY / IDLE"
        log_lines.append(f"{now_str} [INFO] ================================================================")
        log_lines.append(f"{now_str} [INFO]            THORChain USDC -> USDP Web Terminal                ")
        log_lines.append(f"{now_str} [INFO] ================================================================")
        log_lines.append(f"{now_str} [INFO] Engine Process Status:  {status_text}")
        log_lines.append(f"{now_str} [INFO] THORChain Spot Price:   {pools.get('spot_rate', 1.0)} USDP/USDC")
        if swap and swap.get("active"):
            log_lines.append(f"{now_str} [INFO] Monitored Swap:         {swap.get('tx_id')}")
            log_lines.append(f"{now_str} [INFO] Progress:               Block {swap.get('count')}/{swap.get('quantity')} ({swap.get('progress_pct')}%)")
            log_lines.append(f"{now_str} [INFO] Fills Executed:         {swap.get('success_count')} sub-swaps (+{swap.get('profit_bps')} bps)")
            log_lines.append(f"{now_str} [INFO] Price-Limit Preserved:  {swap.get('skipped_count')} sub-swaps skipped")
            log_lines.append(f"{now_str} [INFO] Capital Status:         {swap.get('swapped_usdc')} USDC swapped, {swap.get('remaining_usdc')} USDC in vault")
            log_lines.append(f"{now_str} [INFO] Outbound Target:        {swap.get('out_usdp')} USDP accumulated (Target floor: {swap.get('target_limit')} USDP)")
            log_lines.append(f"{now_str} [INFO] Latest Reason:          {swap.get('latest_reason')}")
        else:
            log_lines.append(f"{now_str} [INFO] No active streaming swap in queue. Ready to quote or snipe.")
    return log_lines


class ThorDashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard HTTP access logs
        return

    def _send(self, status: int, content_type: str, body: bytes, cache_control: str = "no-cache") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, data: Any) -> None:
        payload = json.dumps(data, default=str).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", payload)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            try:
                html = TEMPLATE_PATH.read_bytes()
                self._send(200, "text/html; charset=utf-8", html)
            except OSError:
                self._send(404, "text/plain", b"Dashboard HTML template not found")
            return

        if path == "/favicon.svg":
            if FAVICON_PATH.exists():
                self._send(200, "image/svg+xml", FAVICON_PATH.read_bytes())
            else:
                self._send(404, "text/plain", b"")
            return

        # Serve static assets
        if path.startswith("/static/"):
            filename = path.replace("/static/", "").lstrip("/")
            file_path = STATIC_DIR / filename
            if file_path.exists() and file_path.is_file():
                ext = file_path.suffix.lower()
                content_type = "text/plain"
                if ext == ".css":
                    content_type = "text/css"
                elif ext == ".js":
                    content_type = "application/javascript"
                elif ext == ".svg":
                    content_type = "image/svg+xml"
                elif ext in (".png", ".jpg", ".jpeg"):
                    content_type = f"image/{ext[1:]}"
                self._send(200, content_type, file_path.read_bytes())
                return
            self._send(404, "text/plain", b"Static file not found")
            return

        # API: Deposit Tx Inspector
        if path == "/api/deposit_info":
            custom_tx = query.get("tx", [None])[0]
            deposit_info = get_deposit_tx_info(custom_tx)
            self._json(200, deposit_info)
            return

        # API: Status
        if path == "/api/status":
            custom_tx = query.get("tx", [None])[0]
            # THORNode and the Ethereum RPC are independent; query them in parallel.
            deposit_future = status_pool.submit(get_deposit_tx_info, custom_tx)
            balances_future = status_pool.submit(read_wallet_balances)
            pools_future = status_pool.submit(get_pool_depths)
            running, pid = is_sniper_process_running()
            deposit_info = deposit_future.result()
            swap_info = get_active_streaming_swap(info=deposit_info)
            balances = balances_future.result()
            pools = pools_future.result()

            self._json(200, {
                "ok": True,
                "timestamp": int(time.time()),
                "wallet": balances,
                "pools": pools,
                "active_swap": swap_info,
                "deposit_info": deposit_info,
                "sniper": {
                    "running": running,
                    "pid": pid,
                    "pending_swap": pending_swap_status(),
                },
                "affiliate": {
                    "name": os.getenv("THORCHAIN_AFFILIATE", "symbiosis"),
                    "bps": int(os.getenv("THORCHAIN_AFFILIATE_BPS", "1")),
                },
                "floor_mode": os.getenv("THORCHAIN_FLOOR_MODE", "2bps"),
                "floor_buffer_bps": int(os.getenv("THORCHAIN_FLOOR_BUFFER_BPS", "2")),
            })
            return

        # API: Affiliate Lookup
        if path == "/api/affiliate":
            name = query.get("name", [os.getenv("THORCHAIN_AFFILIATE", "symbiosis")])[0].strip()
            if not name:
                self._json(400, {"ok": False, "error": "name parameter required"})
                return
            try:
                data = fetch_thornode(f"thorname/{name}", timeout=6.0)
                # THORChain pays the affiliate fee to this alias; without it swaps get refunded.
                thor_alias = next(
                    (a.get("address") for a in data.get("aliases") or [] if a.get("chain") == "THOR" and a.get("address")),
                    None,
                )
                self._json(200, {
                    "ok": True,
                    "name": data.get("name"),
                    "thor_alias": thor_alias,
                    "owner": data.get("owner"),
                    "preferred_asset": data.get("preferred_asset"),
                    "accumulated_rune": float(Decimal(int(data.get("affiliate_collector_rune", 0))) / Decimal(THORCHAIN_SCALE)),
                    "threshold_rune": float(Decimal(int(data.get("preferred_asset_swap_threshold_rune", 0))) / Decimal(THORCHAIN_SCALE)),
                    "expire_block_height": data.get("expire_block_height"),
                    "aliases": data.get("aliases", []),
                })
            except Exception as exc:
                self._json(404, {"ok": False, "error": f"THORName '{name}' not found or error: {exc}"})
            return

        # API: Quoter
        if path == "/api/quote":
            try:
                amount_str = query.get("amount", ["10000"])[0]
                amount = Decimal(amount_str)
                slippage_bps = int(query.get("slippage_bps", ["5"])[0])
                affiliate = query.get("affiliate", [os.getenv("THORCHAIN_AFFILIATE", "symbiosis")])[0].strip()
                affiliate_bps = int(query.get("affiliate_bps", [os.getenv("THORCHAIN_AFFILIATE_BPS", "1")])[0])
                floor_mode = query.get("floor_mode", [os.getenv("THORCHAIN_FLOOR_MODE", "2bps")])[0].strip()
                resting = query.get("mode", ["instant"])[0].strip() == "resting"
                amount_1e8 = int(amount * THORCHAIN_SCALE)

                quote_params = {
                    "from_asset": USDC_ASSET,
                    "to_asset": USDP_ASSET,
                    "amount": str(amount_1e8),
                    "destination": get_operator_address(),
                    "affiliate": affiliate,
                    "affiliate_bps": str(affiliate_bps),
                }
                if resting:
                    quote_params["streaming_interval"] = str(RESTING_INTERVAL)
                    quote_params["streaming_quantity"] = str(RESTING_QUANTITY)
                data = fetch_thornode("quote/swap", params=quote_params, timeout=7.0)
                expected_raw = data.get("expected_amount_out", "0")
                expected_usdp = Decimal(expected_raw) / Decimal(THORCHAIN_SCALE)
                net_profit = expected_usdp - amount
                profit_bps = (net_profit / amount) * Decimal(10000)

                fees = data.get("fees", {})
                fees_fmt = {}
                for k, v in fees.items():
                    if isinstance(v, (int, str)) and str(v).isdigit():
                        fees_fmt[k] = float(Decimal(int(v)) / Decimal(THORCHAIN_SCALE))
                    else:
                        fees_fmt[k] = v

                streaming_blocks = data.get("streaming_swap_blocks", 0)
                swap_seconds = data.get("total_swap_seconds", 0)
                raw_memo = data.get("memo", "")

                # Build protected memo versions
                from src.engines.thorchain_usdp_sniper import build_protected_memo
                memo_2bps = ""
                memo_1to1 = ""
                memo_target = ""
                try:
                    memo_2bps = build_protected_memo(raw_memo, min_usdp_target=amount, input_amount=amount, floor_mode="2bps", buffer_bps=2)
                    memo_1to1 = build_protected_memo(raw_memo, min_usdp_target=amount, input_amount=amount, floor_mode="1to1")
                    target_min = amount * (1 + Decimal(slippage_bps) / Decimal(10000))
                    memo_target = build_protected_memo(raw_memo, min_usdp_target=target_min, input_amount=amount, floor_mode="target")
                except Exception:
                    memo_2bps = raw_memo
                    memo_1to1 = raw_memo
                    memo_target = raw_memo

                if floor_mode in ("2bps", "gas_buffer"):
                    active_memo = memo_2bps
                elif floor_mode == "1to1":
                    active_memo = memo_1to1
                else:
                    active_memo = memo_target

                resting_info = None
                if resting:
                    _, interval, quantity = memo_stream_fields(raw_memo)
                    resting_info = {
                        "interval_blocks": interval,
                        "slices": quantity,
                        "slice_usdc": float(amount / max(1, quantity)),
                        "hours": round(swap_seconds / 3600, 1),
                    }
                    try:
                        pools = fetch_pool_spot_rate(thor_client)
                        target_rate = 1 + Decimal(slippage_bps) / Decimal(10000)
                        fill_rate = slice_fill_rate(
                            pools, parse_quote(data, amount), target_rate, floor_mode, 2, affiliate_bps
                        )
                        resting_info["spot_rate"] = float(pools.spot_rate)
                        resting_info["slice_fill_rate"] = float(fill_rate) if fill_rate is not None else None
                    except Exception:
                        pass

                self._json(200, {
                    "ok": True,
                    "input_amount": float(amount),
                    "expected_output": float(round(expected_usdp, 4)),
                    "net_profit": float(round(net_profit, 4)),
                    "profit_bps": float(round(profit_bps, 2)),
                    "is_profitable": bool(net_profit > 0),
                    "streaming_blocks": streaming_blocks,
                    "total_seconds": swap_seconds,
                    "fees": fees_fmt,
                    "memo": active_memo,
                    "memo_raw": raw_memo,
                    "memo_2bps": memo_2bps,
                    "memo_1to1": memo_1to1,
                    "memo_target": memo_target,
                    "floor_mode": floor_mode,
                    "floor_buffer_bps": 2,
                    "router": data.get("router", ROUTER_V4),
                    "inbound_address": data.get("inbound_address", ""),
                    "warning": data.get("warning", ""),
                    "mode": "resting" if resting else "instant",
                    "resting": resting_info,
                })
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return

        # API: Logs
        if path == "/api/logs":
            logs = read_recent_logs(lines=150)
            running, pid = is_sniper_process_running()
            self._json(200, {
                "ok": True,
                "logs": logs,
                "running": running,
                "pid": pid,
            })
            return

        # API: Transactions History
        if path == "/api/transactions":
            txs = []
            if TRANSACTIONS_PATH.exists():
                try:
                    txs = json.loads(TRANSACTIONS_PATH.read_text())
                except Exception:
                    pass
            # Ensure KNOWN_ACTIVE_TX is recorded
            known_hashes = {t.get("tx_hash", "").lower() for t in txs}
            if KNOWN_ACTIVE_TX.lower() not in known_hashes:
                txs.append({
                    "tx_hash": KNOWN_ACTIVE_TX,
                    "amount": "10000",
                    "asset_in": "USDC",
                    "asset_out": "USDP",
                    "timestamp": 1729315000,
                    "status": "completed",
                    "note": "Mainnet streaming swap (reconciled)",
                })
            self._json(200, {"ok": True, "transactions": txs})
            return

        self._send(404, "text/plain", b"Not Found")

    def do_POST(self) -> None:
        global swap_proc
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "Invalid JSON body"})
            return

        # API: Start Sniper Daemon
        if path == "/api/sniper/start":
            confirm = payload.get("confirm", "").strip()
            if confirm != "START_SNIPER":
                self._json(400, {"ok": False, "error": "Confirmation phrase START_SNIPER required"})
                return

            running, pid = is_sniper_process_running()
            if running:
                self._json(400, {"ok": False, "error": f"Sniper daemon is already running (PID {pid})"})
                return
            if swap_proc is not None and swap_proc.poll() is None:
                self._json(409, {"ok": False, "error": f"A live swap is in progress (PID {swap_proc.pid}); wait for it to finish"})
                return

            amount = str(payload.get("amount", "10000"))
            min_profit_bps = str(payload.get("min_profit_bps", "2"))
            slippage_bps = str(payload.get("slippage_bps", "1"))
            poll_interval = str(payload.get("poll_interval", "3"))
            affiliate = str(payload.get("affiliate", os.getenv("THORCHAIN_AFFILIATE", "symbiosis"))).strip()
            affiliate_bps = str(payload.get("affiliate_bps", os.getenv("THORCHAIN_AFFILIATE_BPS", "1"))).strip()
            floor_mode = str(payload.get("floor_mode", "2bps")).strip()

            try:
                proc = spawn_sniper([
                    "--amount", amount,
                    "--min-profit-bps", min_profit_bps,
                    "--slippage-bps", slippage_bps,
                    "--poll-interval", poll_interval,
                    "--affiliate", affiliate,
                    "--affiliate-bps", affiliate_bps,
                    "--floor-mode", floor_mode,
                    "--live",
                    "--confirm-live", "EXECUTE_THOR_SNIPER",
                ])
                SNIPER_PID_PATH.write_text(str(proc.pid))
                self._json(200, {
                    "ok": True,
                    "message": f"Sniper daemon successfully launched! (PID {proc.pid}) with affiliate '{affiliate}' ({affiliate_bps} bps, floor: {floor_mode})",
                    "pid": proc.pid
                })
            except Exception as exc:
                self._json(500, {"ok": False, "error": f"Failed to spawn sniper process: {exc}"})
            return

        # API: Stop Sniper Daemon
        if path == "/api/sniper/stop":
            running, pid = is_sniper_process_running()
            if not running or not pid:
                pending = pending_swap_status()
                if pending and pending["state"] == "waiting":
                    # The sniper ignores SIGTERM once it starts placing, so check it really exited.
                    swap_proc.terminate()
                    try:
                        swap_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._json(409, {"ok": False, "error": "The resting order already started placing; it was not cancelled."})
                        return
                    self._json(200, {"ok": True, "message": f"Waiting resting order (PID {pending['pid']}) cancelled; nothing was sent."})
                    return
                self._json(400, {"ok": False, "error": "No sniper daemon or waiting resting order to stop"})
                return

            try:
                os.kill(pid, signal.SIGTERM)
                if SNIPER_PID_PATH.exists():
                    SNIPER_PID_PATH.unlink()
                self._json(200, {"ok": True, "message": f"Sniper process {pid} stopped."})
            except Exception as exc:
                self._json(500, {"ok": False, "error": f"Failed to kill process {pid}: {exc}"})
            return

        # API: Execute One-off Live Swap
        if path == "/api/swap/live":
            confirm = payload.get("confirm", "").strip()
            if confirm != "EXECUTE_LIVE_SWAP":
                self._json(400, {"ok": False, "error": "Confirmation phrase EXECUTE_LIVE_SWAP required"})
                return

            amount = str(payload.get("amount", "10000"))
            slippage_bps = str(payload.get("slippage_bps", "1"))
            affiliate = str(payload.get("affiliate", os.getenv("THORCHAIN_AFFILIATE", "symbiosis"))).strip()
            affiliate_bps = str(payload.get("affiliate_bps", os.getenv("THORCHAIN_AFFILIATE_BPS", "1"))).strip()
            floor_mode = str(payload.get("floor_mode", "2bps")).strip()
            resting = str(payload.get("mode", "instant")).strip() == "resting"

            # Two senders on one wallet would race for the same nonce.
            running, pid = is_sniper_process_running()
            if running:
                self._json(409, {"ok": False, "error": f"Sniper daemon is running (PID {pid}); stop it before sending a manual swap"})
                return

            try:
                with swap_lock:
                    if swap_proc is not None and swap_proc.poll() is None:
                        self._json(409, {"ok": False, "error": f"A live swap is already in progress (PID {swap_proc.pid}); see the terminal log"})
                        return
                    with open(SNIPER_LOG_PATH, "a", encoding="utf-8") as log_file:
                        log_start = log_file.seek(0, os.SEEK_END)
                    # --no-monitor: exit once the deposit confirms; the inspector tracks the
                    # streaming swap from there, which can take far longer than an HTTP request.
                    # A resting order runs without --once: it polls until the rate lets slices fill.
                    proc = swap_proc = spawn_sniper([
                        *([] if resting else ["--once"]),
                        "--no-monitor",
                        *(["--resting"] if resting else []),
                        "--amount", amount,
                        "--slippage-bps", slippage_bps,
                        "--affiliate", affiliate,
                        "--affiliate-bps", affiliate_bps,
                        "--floor-mode", floor_mode,
                        "--live",
                        "--confirm-live", "EXECUTE_THOR_SNIPER",
                    ])
                    swap_meta.update(log_start=log_start, mode="resting" if resting else "instant")
            except Exception as exc:
                self._json(500, {"ok": False, "error": f"Failed to spawn swap process: {exc}"})
                return

            deadline = time.monotonic() + SWAP_WAIT_SECONDS
            returncode = proc.poll()
            while returncode is None and time.monotonic() < deadline:
                output = read_log_since(log_start)
                if resting and RESTING_WAITING_MARK in output and RESTING_PLACING_MARK not in output:
                    threading.Thread(target=watch_swap, args=(proc, log_start, amount, resting), daemon=True).start()
                    detail = pending_swap_status() or {}
                    self._json(202, {
                        "ok": True,
                        "waiting": True,
                        "pid": proc.pid,
                        "message": (
                            f"Waiting to place: {detail.get('detail') or 'the pool rate is below the slice fill rate.'} "
                            "The order is placed automatically once the rate reaches it. Cancel it from the Sniper tab's Stop button."
                        ),
                    })
                    return
                time.sleep(0.5)
                returncode = proc.poll()

            if returncode is None:
                # Never kill a process that may be mid-broadcast; record its deposit when it exits.
                threading.Thread(target=watch_swap, args=(proc, log_start, amount, resting), daemon=True).start()
            status, reply = settle_swap(read_log_since(log_start), returncode, amount, resting, proc.pid, record=returncode is not None)
            self._json(status, reply)
            return

        self._send(404, "text/plain", b"Not Found")


class ThorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThorServer((host, port), ThorDashboardHandler)
    print(f"\n=======================================================")
    print(f"      THORChain USDC -> USDP Sniper WebApp Running    ")
    print(f"=======================================================")
    print(f"  Local URL:   http://{host}:{port}")
    print(f"  API Status:  http://{host}:{port}/api/status")
    print(f"  Live Quote:  http://{host}:{port}/api/quote?amount=10000")
    print(f"  Stop:        Press Ctrl+C\n")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="THORChain USDC -> USDP WebApp Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()
    run(host=args.host, port=args.port)
