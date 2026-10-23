from __future__ import annotations

import argparse
import json
import logging
import math
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
from datetime import datetime
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
    resolve_sniper_settings,
    load_quote_fee_floors,
    DEFAULT_THORNODE_BASE,
    THORCHAIN_SCALE,
    USDC_ASSET,
    USDP_ASSET,
    USDC_CONTRACT,
    USDP_CONTRACT,
)

ROUTER_V4 = "0xD37BbE5744D730a1d98d8DC97c42F0Ca46aD7146"
USDT_ASSET = "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7"
# Only USDC -> USDP can be executed by the sniper; other pairs are quote-only.
QUOTE_SOURCES = {"usdc": USDC_ASSET, "usdt": USDT_ASSET, "usdp": USDP_ASSET}
MIDGARD_BASE = "https://gateway.liquify.com/chain/thorchain_midgard/v2"

# A one-off swap quotes, may wait up to 120s for an approval receipt, then up to
# 180s for the deposit receipt. The process is never killed when this runs out.
SWAP_WAIT_SECONDS = 420
DEPOSIT_HASH_RE = re.compile(r"Deposit transaction broadcasted! Hash: (0x[0-9a-fA-F]{64})")
PROTECTED_MEMO_RE = re.compile(r"Protected Memo: (\S+)")
# The sniper may send less than requested: it sizes deposits to what the pool can absorb.
DEPOSIT_AMOUNT_RE = re.compile(r"Amount: \d+ atomic units \(([\d.]+) USDC\)")

thor_client = ThorClient(base_url=DEFAULT_THORNODE_BASE, timeout=8.0)
status_pool = ThreadPoolExecutor(max_workers=6)
metric_cache: dict[str, dict[str, Any]] = {}
metric_lock = threading.Lock()


def cached_metric(name: str, loader, fallback: dict[str, Any]) -> dict[str, Any]:
    """Slow Ethereum RPCs must not block current swap updates or pile up polls."""
    with metric_lock:
        entry = metric_cache.setdefault(name, {"at": 0, "data": fallback, "future": None})
        future = entry["future"]
        if future is not None and future.done():
            try:
                entry.update(data=future.result(), at=time.time())
            except Exception:
                pass
            entry["future"] = None
        if entry["future"] is None and time.time() - entry["at"] >= 15:
            entry["future"] = status_pool.submit(loader)
        return {**entry["data"], "updated_at": entry["at"] or None, "refreshing": entry["future"] is not None}

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
    return ThorClient(base_url=thor_client.base_url, timeout=timeout).get(path, params=params)


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


# Status polling asks for the default deposit every few seconds; Midgard only needs a refresh now and then.
deposits_cache: dict[str, Any] = {"at": float("-inf"), "items": [], "error": None}
queue_cache: dict[str, Any] = {"at": float("-inf"), "items": [], "error": None}
queue_lock = threading.Lock()
actions_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
block_times: dict[int, int] = {}


def wallet_queue() -> list[dict[str, Any]]:
    """Find wallet swaps before Midgard indexes them, including native limit orders."""
    with queue_lock:
        if time.monotonic() - queue_cache["at"] < 3:
            return queue_cache["items"]
        operator = get_operator_address().lower()
        items, errors = [], []
        paths = ("swaps/streaming", "queue/swap", "queue/limit_swaps?sender=" + operator)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = [pool.submit(fetch_thornode, path, timeout=5) for path in paths]
        for path, result in zip(paths, results):
            try:
                data = result.result()
                rows = data.get("limit_swaps", []) if isinstance(data, dict) else data
                for row in rows or []:
                    tx = row.get("tx") or {}
                    if operator not in {
                        str(row.get("destination", "")).lower(),
                        str(row.get("sender", "")).lower(),
                        str(row.get("from_address", "")).lower(),
                        str(tx.get("from_address", "")).lower(),
                    }:
                        continue
                    tx_id = row.get("tx_id") or tx.get("id")
                    if tx_id and re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", tx_id):
                        items.append({**row, "tx_id": tx_id.removeprefix("0x").upper(),
                                      "queue_type": "limit" if "limit_swaps" in path else "market"})
            except Exception as exc:
                errors.append(str(exc))
        queue_cache.update(at=time.monotonic(), items=items, error="; ".join(errors) or None)
        return items


def recorded_deposits() -> list[dict[str, Any]]:
    try:
        data = json.loads(TRANSACTIONS_PATH.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def local_deposits() -> list[dict[str, Any]]:
    items = recorded_deposits()
    # CLI deposits may precede Midgard indexing and aren't recorded by the web server.
    try:
        with SNIPER_LOG_PATH.open("rb") as stream:
            stream.seek(max(0, SNIPER_LOG_PATH.stat().st_size - 128_000))
            for line in stream.read().decode("utf-8", errors="replace").splitlines():
                match = DEPOSIT_HASH_RE.search(line)
                if match:
                    try:
                        timestamp = int(datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp())
                    except ValueError:
                        timestamp = 0
                    items.append({"tx_hash": match[1], "timestamp": timestamp, "status": "broadcast"})
    except OSError:
        pass
    return items


def merge_deposits(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {}
    for group in groups:
        for row in group:
            tx_id = str(row.get("tx_hash", "")).lower()
            if re.fullmatch(r"(?:0x)?[0-9a-f]{64}", tx_id):
                tx_id = "0x" + tx_id.removeprefix("0x")
                merged[tx_id] = {**merged.get(tx_id, {}), **row, "tx_hash": tx_id}
    return sorted(merged.values(), key=lambda row: int(row.get("timestamp") or 0), reverse=True)


def recent_deposits(limit: int = 2) -> list[dict[str, Any]]:
    """The operator wallet's newest THORChain swap deposits, pending ones included, newest first.

    Read from Midgard so deposits placed from the CLI count too, not just ones this
    webapp recorded.
    """
    if time.monotonic() - deposits_cache["at"] < 15:
        return merge_deposits(local_deposits(), deposits_cache["items"])[:limit]
    operator = get_operator_address().lower()
    try:
        req = urllib.request.Request(f"{MIDGARD_BASE}/actions?address={operator}&limit=20", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            actions = json.loads(resp.read().decode()).get("actions", [])
    except Exception as exc:
        deposits_cache.update(at=time.monotonic(), error=str(exc))
        return merge_deposits(local_deposits(), deposits_cache["items"])[:limit]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for act in actions:
        # A refunded swap shows up as both a swap and a refund action with the same inbound tx.
        if act.get("type") not in ("swap", "refund"):
            continue
        for tx_in in act.get("in") or []:
            tx_id = (tx_in.get("txID") or "").lower()
            if (tx_in.get("address") or "").lower() != operator or not tx_id or tx_id in seen:
                continue
            seen.add(tx_id)
            items.append({
                "tx_hash": "0x" + tx_id.removeprefix("0x"),
                "status": act.get("status"),
                "timestamp": int(act.get("date") or 0) // 10**9,
            })
    deposits_cache.update(at=time.monotonic(), items=items, error=None)
    return merge_deposits(local_deposits(), items)[:limit]


def block_timestamp(height: int) -> int | None:
    if height in block_times:
        return block_times[height]
    try:
        data = fetch_thornode("block", {"height": height}, timeout=5)
        stamp = data.get("header", {}).get("time") or data.get("block", {}).get("header", {}).get("time")
        timestamp = int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
        if len(block_times) > 128:
            block_times.pop(next(iter(block_times)))
        block_times[height] = timestamp
        return timestamp
    except Exception:
        return None


def transaction_actions(tx_id: str) -> list[dict[str, Any]]:
    cached = actions_cache.get(tx_id)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    try:
        req = urllib.request.Request(f"{MIDGARD_BASE}/actions?txid={tx_id}", headers={"User-Agent": "arbbot-dashboard"})
        with urllib.request.urlopen(req, timeout=4) as response:
            actions = json.load(response).get("actions", [])
        if len(actions_cache) > 128:
            actions_cache.pop(next(iter(actions_cache)))
        actions_cache[tx_id] = (time.monotonic(), actions)
        return actions
    except Exception:
        return []


def get_deposit_tx_info(tx_hash: str | None = None, queue: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Retrieve complete, institutional-grade information for any THORChain deposit transaction.
    
    Parses both active streaming swaps from /swaps/streaming and finalized/refunded
    outbounds from /tx/status/{hash}.
    """
    if not tx_hash:
        with ThreadPoolExecutor(max_workers=2) as pool:
            queue_future = pool.submit(wallet_queue)
            recent_future = pool.submit(recent_deposits, 20)
            queue, recent = queue_future.result(), recent_future.result()
        candidates = ["0x" + row["tx_id"].lower() for row in sorted(queue, key=lambda r: int(r.get("initial_height") or r.get("created_height") or 0), reverse=True)]
        # Include recent CLI/web deposits and Midgard's pending actions. A newer
        # completed swap must never hide an older swap that is still running.
        candidates += [r["tx_hash"] for r in recent if r.get("status") not in ("success", "completed", "refunded", "failed")]
        unresolved = None
        for candidate in dict.fromkeys(candidates):
            info = get_deposit_tx_info(candidate, queue=queue)
            if info.get("found") and not info.get("is_completed"):
                return info
            if not info.get("found") and candidate in candidates[:2]:
                unresolved = info
        if unresolved:
            return unresolved
        discovery_error = queue_cache.get("error") or deposits_cache.get("error")
        return {"ok": not bool(discovery_error), "found": False, "status": "UNAVAILABLE" if discovery_error else "NO_ACTIVE_SWAP",
                "message": "No active swap found for this wallet. Select a recent deposit to inspect its settlement.",
                "error": discovery_error}
    search_hash = tx_hash.strip()
    if not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{64}", search_hash):
        return {"ok": False, "found": False, "status": "INVALID_HASH", "error": "Enter a 64-character Ethereum transaction hash"}

    clean_hash = search_hash[2:].upper() if (search_hash.startswith("0x") or search_hash.startswith("0X")) else search_hash.upper()
    formatted_hash = "0x" + clean_hash.lower()

    # 1. Check swaps/streaming
    match = None
    try:
        streaming_list = queue if queue is not None else fetch_thornode("swaps/streaming", timeout=6.0)
        if isinstance(streaming_list, list):
            for item in streaming_list:
                item_tx = str(item.get("tx_id", "")).upper()
                if item_tx.removeprefix("0X") == clean_hash:
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
            "error": status_data.get("error"),
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
        elif "USDC" in asset and "REFUND" in memo.upper():
            refund_usdc += raw_amt / THORCHAIN_SCALE
            if not refund_tx_hash and "0000000000" not in tx_id:
                refund_tx_hash = tx_id_fmt
        elif "RUNE" in asset:
            affiliate_rune += raw_amt / THORCHAIN_SCALE
            affiliate_collector = out.get("to_address")

    stages = status_data.get("stages", {})
    swap_finalised = stages.get("swap_finalised", {}).get("completed", False)
    outbound_signed = stages.get("outbound_signed", {}).get("completed", False)

    is_completed = bool(swap_finalised and outbound_signed)
    is_active = not is_completed
    is_limit = str(tx_in.get("memo") or "").startswith("=<:") or bool(match and match.get("queue_type") == "limit")

    # Status classification
    if not swap_finalised and (match is not None or stages.get("swap_status", {}).get("pending", False)):
        status_label = "LIMIT_WAITING" if is_limit else "STREAMING"
    elif swap_finalised and not outbound_signed:
        status_label = "OUTBOUND_PENDING"
    elif is_completed:
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
    if match and int(match.get("quantity") or 0) > 0:
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
        interval = int(match.get("interval") or 0)
        current_height = None
        try:
            current_height = max(int(row.get("thorchain") or 0) for row in fetch_thornode("lastblock", timeout=4))
        except Exception:
            pass
        last_height = int(match.get("last_height") or match.get("initial_height") or 0)
        elapsed_blocks = last_height - int(match.get("initial_height") or last_height)
        # Rapid streams have no fixed cadence. Estimate from actual attempts
        # over observed blocks when available, rather than inventing a cadence.
        cadence = None if is_limit else interval if interval > 0 else elapsed_blocks / count if count > 0 and elapsed_blocks > 0 else None
        end_height = last_height + math.ceil(max(0, quantity - count) * cadence) if cadence is not None else None
        blocks_left = max(0, end_height - current_height) if end_height and current_height else None
        est_seconds_left = blocks_left * 6 if blocks_left is not None else None

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
            "interval_blocks": interval,
            "estimated_finalize_height": end_height,
            "estimate_basis": "scheduled_interval" if interval > 0 else "observed_pace" if cadence else None,
            "latest_reason": latest_reason,
            "recent_reasons": reasons[-10:] if reasons else [],
            "destination": match.get("destination"),
            "initial_height": match.get("initial_height"),
            "last_height": match.get("last_height"),
        }

    # Reconciled capital
    planned_refund = sum(int(o.get("coin", {}).get("amount") or 0) / THORCHAIN_SCALE
                         for o in status_data.get("planned_out_txs", [])
                         if o.get("refund") and "USDC" in o.get("coin", {}).get("asset", ""))
    # The inbound-minus-refund difference includes the refund fee; retain that
    # cost in settlement P&L. While streaming, only network-reported fills count.
    swapped_usdc = streaming_info["swapped_usdc"] if streaming_info else max(0, in_amount - max(refund_usdc, planned_refund)) if swap_finalised else 0

    refund_reason = None
    finalized_at = None
    started_at = None
    history_stream = None
    swapped_is_estimate = not bool(streaming_info)
    if swap_finalised:
        actions = transaction_actions(clean_hash)
        final_height = 0
        for action in actions:
            metadata = action.get("metadata", {})
            refund_reason = metadata.get("refund", {}).get("reason") or refund_reason
            meta = metadata.get("swap", {}).get("streamingSwapMeta")
            if meta:
                if meta.get("inCoin", {}).get("amount") is not None:
                    swapped_usdc = int(meta["inCoin"]["amount"]) / THORCHAIN_SCALE
                    swapped_is_estimate = False
                history_stream = {"count": int(meta.get("count") or 0), "quantity": int(meta.get("quantity") or 0),
                                  "skipped_count": len(meta.get("failedSwaps") or []), "interval_blocks": int(meta.get("interval") or 0)}
                history_stream["success_count"] = max(0, history_stream["count"] - history_stream["skipped_count"])
                final_height = max(final_height, int(meta.get("lastHeight") or 0))
            # Refund actions are emitted when the stream ends; the swap action
            # itself is dated at its start and must not be called finalization.
            if action.get("type") == "refund":
                final_height = max(final_height, int(action.get("height") or 0))
            elif not meta and action.get("type") == "swap":
                final_height = max(final_height, int(action.get("height") or 0))
            if action.get("type") == "swap":
                started_at = int(action.get("date") or 0) // 10**9 or None
        if final_height:
            finalized_at = block_timestamp(final_height)
    effective_rate = out_usdp / swapped_usdc if swapped_usdc > 0 else 0
    # Total-token result includes refund fees; the filled portion alone does not.
    net_profit = out_usdp + refund_usdc - in_amount if is_completed else None
    net_profit_bps = net_profit / in_amount * 10000 if net_profit is not None and in_amount > 0 else None
    seconds_left = streaming_info.get("est_seconds_left") if streaming_info else None
    delay = stages.get("outbound_delay", {}).get("remaining_delay_seconds")
    if streaming_info and streaming_info.get("initial_height"):
        started_at = block_timestamp(int(streaming_info["initial_height"]))
    estimated_finalize_at = int(time.time()) + seconds_left if seconds_left is not None else None

    return {
        "ok": True,
        "found": True,
        "tx_id": formatted_hash,
        "status": status_label,
        "is_active": is_active,
        "is_completed": is_completed,
        "progress_pct": streaming_info["progress_pct"] if streaming_info else (100.0 if swap_finalised else 0.0),
        "timing": {"started_at": started_at, "finalized_at": finalized_at,
                   "estimated_finalize_at": estimated_finalize_at, "seconds_left": seconds_left,
                   "estimate_basis": streaming_info.get("estimate_basis") if streaming_info else None,
                   "outbound_delay_seconds": delay,
                   "estimated_delivery_at": int(time.time()) + int(delay) if delay is not None and not outbound_signed else None,
                   "estimate_note": "Estimated using 6-second THORChain blocks. Delivery and refunds follow stream finalization; network delays can change timing."},
        "history_stream": history_stream,
        "inbound": {
            "tx_hash": formatted_hash,
            "asset": in_asset,
            "amount": in_amount or (streaming_info["deposit_usdc"] if streaming_info else 0),
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
            "planned_refund_usdc": planned_refund,
            "swapped_usdc": round(swapped_usdc, 4),
            "swapped_is_estimate": swapped_is_estimate,
            "effective_rate": round(effective_rate, 6),
            "net_profit_usdp": round(net_profit, 4) if net_profit is not None else None,
            "net_profit_bps": round(net_profit_bps, 2) if net_profit_bps is not None else None,
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
        res["quantity"] = (info.get("history_stream") or {}).get("quantity")
        res["count"] = (info.get("history_stream") or {}).get("count")
        res["progress_pct"] = info.get("progress_pct", 0)
        res["deposit_usdc"] = info.get("inbound", {}).get("amount", 0)
        res["swapped_usdc"] = settlement.get("swapped_usdc", 0.0)
        res["remaining_usdc"] = 0.0
        res["out_usdp"] = settlement.get("out_usdp", 0.0)
        res["effective_rate"] = settlement.get("effective_rate", 0.0)
        res["profit_bps"] = settlement.get("net_profit_bps", 0.0)
        res["success_count"] = (info.get("history_stream") or {}).get("success_count")
        res["skipped_count"] = (info.get("history_stream") or {}).get("skipped_count")
        res["blocks_left"] = 0 if info.get("is_completed") else None
        res["est_seconds_left"] = 0 if info.get("is_completed") else None
        if settlement.get("refund_reason"):
            res["latest_reason"] = f"Refunded by THORChain: {settlement['refund_reason']}"
        else:
            res["latest_reason"] = (
                f"Settled: Delivered {settlement.get('out_usdp')} USDP, refunded {settlement.get('refund_usdc')} USDC"
            )
    return res


def pool_rate_to_usdp(source_asset: str, target_asset: str = USDP_ASSET) -> float | None:
    """USDP per unit of source_asset at the two pools' current RUNE prices, before fees."""
    try:
        # Two single-pool lookups: the full pools list is much larger and slow on the gateway.
        src, dst = fetch_thornode(f"pool/{source_asset}"), fetch_thornode(f"pool/{target_asset}")
        return (int(src["balance_rune"]) / int(src["balance_asset"])) / (int(dst["balance_rune"]) / int(dst["balance_asset"]))
    except Exception:
        return None


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
    """Find daemons, including explicitly marked resting daemons, excluding one-offs."""
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
        if (any(a.endswith("thor_usdp_sniper.py") for a in argv) and "--once" not in argv
                and ("--resting" not in argv or "--daemon" in argv)):
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


def daemon_arguments(payload: dict[str, Any]) -> list[str]:
    """Validate daemon controls without spawning a process or contacting RPCs."""
    floor_mode = str(payload.get("floor_mode", "2bps")).strip()
    if floor_mode not in ("2bps", "1bps", "1to1", "target"):
        raise ValueError("Unknown slice execution price floor")
    mode = str(payload.get("mode", "instant")).strip()
    if mode not in ("instant", "resting"):
        raise ValueError("Daemon mode must be instant or resting")
    settings = argparse.Namespace(
        amount=Decimal(str(payload.get("amount", "10000"))),
        min_profit_bps=Decimal(str(payload.get("min_profit_bps", "2"))),
        min_usdp=None,
        min_net_profit=Decimal(str(payload.get("min_net_profit", os.getenv("THORCHAIN_SNIPER_MIN_NET_PROFIT", "1")))),
        poll_interval=float(payload.get("poll_interval", "3")),
        slippage_bps=int(str(payload.get("slippage_bps", "1"))),
        affiliate_bps=int(str(payload.get("affiliate_bps", os.getenv("THORCHAIN_AFFILIATE_BPS", "1")))),
        floor_mode=floor_mode,
        floor_buffer_bps=int(str(payload.get("floor_buffer_bps", os.getenv("THORCHAIN_FLOOR_BUFFER_BPS", "2")))),
        interval=RESTING_INTERVAL, quantity=RESTING_QUANTITY,
    )
    resolve_sniper_settings(settings)
    affiliate = str(payload.get("affiliate", os.getenv("THORCHAIN_AFFILIATE", "symbiosis"))).strip()
    arguments = [
        "--amount", str(settings.amount),
        "--min-profit-bps", str(settings.min_profit_bps),
        "--min-net-profit", str(settings.min_net_profit),
        "--slippage-bps", str(settings.slippage_bps),
        "--poll-interval", str(settings.poll_interval),
        "--affiliate", affiliate,
        "--affiliate-bps", str(settings.affiliate_bps),
        "--floor-mode", settings.floor_mode,
        "--floor-buffer-bps", str(settings.floor_buffer_bps),
        "--interval", str(settings.interval),
        "--quantity", str(settings.quantity),
        "--daemon", "--live", "--confirm-live", "EXECUTE_THOR_SNIPER",
    ]
    if mode == "resting":
        arguments.append("--resting")
    return arguments


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
                    f"Resting order of {deposited} USDC placed. THORChain will try the maximum permitted slices, one every {RESTING_INTERVAL} blocks; track its estimated completion in the inspector."
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
        from src.web.thor_calculator import serve_calculator
        if serve_calculator(self):
            return
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

        # API: the wallet's newest deposits, for the inspector's Latest/Previous chips
        if path == "/api/latest_deposits":
            self._json(200, {"ok": True, "deposits": recent_deposits(2)})
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
            balances = cached_metric("wallet", read_wallet_balances, {"operator": get_operator_address(), "rpc_connected": False})
            pools = cached_metric("pools", get_pool_depths, {})
            running, pid = is_sniper_process_running()
            deposit_info = deposit_future.result()
            swap_info = get_active_streaming_swap(info=deposit_info)

            self._json(200, {
                "ok": True,
                "timestamp": int(time.time()),
                "wallet": balances,
                "pools": pools,
                "active_swap": swap_info if deposit_info.get("is_active") else None,
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
                source = query.get("source", ["usdc"])[0].strip().lower()
                slippage_bps = int(query.get("slippage_bps", ["5" if source == "usdc" else "50"])[0])
                affiliate = query.get("affiliate", [os.getenv("THORCHAIN_AFFILIATE", "symbiosis")])[0].strip()
                affiliate_bps = int(query.get("affiliate_bps", [os.getenv("THORCHAIN_AFFILIATE_BPS", "1")])[0])
                floor_mode = query.get("floor_mode", [os.getenv("THORCHAIN_FLOOR_MODE", "2bps") if source == "usdc" else "market"])[0].strip()
                resting = query.get("mode", ["instant"])[0].strip() == "resting"
                if source not in QUOTE_SOURCES:
                    self._json(400, {"ok": False, "error": f"source must be one of {sorted(QUOTE_SOURCES)}"})
                    return
                target = "usdc" if source == "usdp" else "usdp"
                target_asset = QUOTE_SOURCES[target]
                if not amount.is_finite() or amount <= 0:
                    raise ValueError("Amount must be a positive, finite number")
                if not 0 <= slippage_bps <= 10000 or not 0 <= affiliate_bps <= 10000:
                    raise ValueError("Basis points must be between 0 and 10000")
                if floor_mode not in ("market", "2bps", "gas_buffer", "1to1", "target"):
                    raise ValueError("Unknown protection mode")
                if floor_mode == "market" and (source == "usdc" or slippage_bps >= 10000):
                    raise ValueError("Market quote mode is for quote-only pairs with slippage below 10000 bps")
                if resting and source != "usdc":
                    raise ValueError("Resting execution is available for USDC -> USDP only")
                amount_1e8 = int(amount * THORCHAIN_SCALE)
                if amount_1e8 <= 0:
                    raise ValueError("Amount is below THORChain precision")

                quote_params = {
                    "from_asset": QUOTE_SOURCES[source],
                    "to_asset": target_asset,
                    "amount": str(amount_1e8),
                    "destination": get_operator_address(),
                    "affiliate": affiliate,
                    "affiliate_bps": str(affiliate_bps),
                    "streaming_interval": str(RESTING_INTERVAL),
                    "streaming_quantity": str(RESTING_QUANTITY),
                }
                if floor_mode == "market":
                    quote_params["liquidity_tolerance_bps"] = str(slippage_bps)
                # Look up the pool rate while THORNode builds the quote.
                pool_rate_future = status_pool.submit(pool_rate_to_usdp, QUOTE_SOURCES[source], target_asset)
                data = fetch_thornode("quote/swap", params=quote_params, timeout=7.0)
                if data.get("error") or not data.get("memo") or int(data.get("expected_amount_out") or 0) <= 0:
                    raise ValueError(data.get("error") or "THORNode returned no executable quote")
                if data.get("expiry") is not None and int(data["expiry"]) <= time.time():
                    raise ValueError("THORNode quote has expired; request a fresh quote")
                expected_raw = data.get("expected_amount_out", "0")
                expected_usdp = Decimal(expected_raw) / Decimal(THORCHAIN_SCALE)
                net_profit = expected_usdp - amount
                profit_bps = (net_profit / amount) * Decimal(10000)

                fees = data.get("fees", {})
                fees_fmt = {}
                for k, v in fees.items():
                    if k not in ("slippage_bps", "total_bps") and isinstance(v, (int, str)) and str(v).isdigit():
                        fees_fmt[k] = float(Decimal(int(v)) / Decimal(THORCHAIN_SCALE))
                    else:
                        fees_fmt[k] = v

                streaming_blocks = data.get("streaming_swap_blocks", 0)
                swap_seconds = data.get("total_swap_seconds", 0)
                raw_memo = data.get("memo", "")
                memo_parts = raw_memo.split(":")
                if len(memo_parts) < 4 or memo_parts[1].upper() not in (target_asset.upper(), target_asset.split("-")[0].upper()):
                    raise ValueError("THORNode returned a memo for the wrong output asset")
                if fees.get("asset") and fees["asset"].upper() != target_asset.upper():
                    raise ValueError("THORNode returned fees in the wrong output asset")

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
                except Exception as exc:
                    raise ValueError(f"Could not build a protected quote memo: {exc}") from exc

                if floor_mode == "market":
                    # Base tolerance on expected output AFTER quoted fees, not
                    # on nominal input parity. Never replace this with +2 bps.
                    minimum = max(1, int(Decimal(expected_raw) * (10000 - slippage_bps) / 10000))
                    stream_parts = memo_parts[3].split("/")
                    stream_parts[0] = str(minimum)
                    memo_parts[3] = "/".join(stream_parts)
                    active_memo = ":".join(memo_parts)
                elif floor_mode in ("2bps", "gas_buffer"):
                    active_memo = memo_2bps
                elif floor_mode == "1to1":
                    active_memo = memo_1to1
                else:
                    active_memo = memo_target
                minimum_raw, _, _ = memo_stream_fields(active_memo)
                quote_meets_floor = int(expected_raw) >= minimum_raw
                floor_warning = (
                    "Market estimate is below the selected minimum. This quote does not demonstrate a full fill at that floor; slices may fail and refund fees can apply."
                    if not quote_meets_floor else
                    "A premium floor applies to filled slices; it does not guarantee the premium after settlement and refund fees."
                    if floor_mode != "market" else ""
                )

                resting_info = None
                if resting:
                    _, interval, quantity = memo_stream_fields(raw_memo)
                    quantity = quantity or int(data.get("max_streaming_quantity") or 0)
                    resting_info = {
                        "interval_blocks": interval,
                        "slices": quantity,
                        "slice_usdc": float(amount / max(1, quantity)),
                        "hours": round(swap_seconds / 3600, 1),
                    }
                    try:
                        if source != "usdc":
                            raise ValueError("slice fill rate is modeled for the USDC pool only")
                        pools = fetch_pool_spot_rate(thor_client)
                        target_rate = 1 + Decimal(slippage_bps) / Decimal(10000)
                        resting_quote = parse_quote(data, amount)
                        load_quote_fee_floors(thor_client, resting_quote, affiliate, affiliate_bps)
                        fill_rate = slice_fill_rate(pools, resting_quote, target_rate, floor_mode, 2, affiliate_bps)
                        resting_info["spot_rate"] = float(pools.spot_rate)
                        resting_info["slice_fill_rate"] = float(fill_rate) if fill_rate is not None else None
                    except Exception:
                        pass

                self._json(200, {
                    "ok": True,
                    "input_amount": float(amount),
                    "expected_output": float(round(expected_usdp, 4)),
                    "minimum_output": float(Decimal(minimum_raw) / THORCHAIN_SCALE),
                    "minimum_output_atomic": str(minimum_raw),
                    "quote_meets_floor": quote_meets_floor,
                    "quote_status": "QUOTED" if quote_meets_floor else "FLOOR_NOT_MET",
                    "floor_warning": floor_warning,
                    "slippage_bps": slippage_bps,
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
                    "floor_buffer_bps": None if floor_mode == "market" else 2,
                    "router": data.get("router", ROUTER_V4),
                    "inbound_address": data.get("inbound_address", ""),
                    "warning": data.get("warning", ""),
                    "mode": "resting" if resting else "instant",
                    "resting": resting_info,
                    "source": source,
                    "target": target,
                    "can_execute": source == "usdc",
                    "quoted_at": int(time.time()),
                    "expiry": data.get("expiry"),
                    "pool_rate": pool_rate_future.result(),
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
            txs = recent_deposits(20)
            self._json(200, {"ok": True, "transactions": txs})
            return

        self._send(404, "text/plain", b"Not Found")

    def do_POST(self) -> None:
        from src.web.thor_calculator import serve_calculator
        if serve_calculator(self):
            return
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

            try:
                arguments = daemon_arguments(payload)
            except (ValueError, TypeError, ArithmeticError, RuntimeError) as exc:
                self._json(400, {"ok": False, "error": f"Invalid daemon settings: {exc}"})
                return

            try:
                with swap_lock:
                    # A second Start request or manual swap may have won while
                    # the first request was being validated.
                    running, pid = is_sniper_process_running()
                    if running or (swap_proc is not None and swap_proc.poll() is None):
                        self._json(409, {"ok": False, "error": "A daemon or manual deposit already owns this wallet"})
                        return
                    proc = spawn_sniper(arguments)
                    SNIPER_PID_PATH.write_text(str(proc.pid))
                self._json(200, {
                    "ok": True,
                    "message": (
                        f"Resting daemon launched (PID {proc.pid}). It waits for the slice-price condition, places one stream, then exits. The full deposit is locked until settlement; the immediate net-profit check does not apply."
                        if "--resting" in arguments else
                        f"Immediate daemon launched (PID {proc.pid}). It waits for fills meeting the modeled net-profit threshold; a positive full-fill quote alone will not trigger a deposit."
                    ),
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
                if pending:
                    self._json(409, {"ok": False, "error": f"The swap (PID {pending['pid']}) is already being placed; it can't be cancelled."})
                    return
                self._json(400, {"ok": False, "error": "No sniper daemon or waiting resting order to stop"})
                return

            try:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while pid in sniper_daemon_pids() and time.monotonic() < deadline:
                    reap_children()
                    time.sleep(0.05)
                if pid in sniper_daemon_pids():
                    self._json(409, {"ok": False, "error": "The daemon has not stopped; it may already be placing a deposit. It was not force-killed. Any submitted on-chain stream continues."})
                    return
                if SNIPER_PID_PATH.exists():
                    SNIPER_PID_PATH.unlink()
                self._json(200, {"ok": True, "message": f"Sniper process {pid} stopped. Any stream already submitted to THORChain continues on-chain."})
            except Exception as exc:
                self._json(500, {"ok": False, "error": f"Failed to kill process {pid}: {exc}"})
            return

        # API: Execute One-off Live Swap
        if path == "/api/swap/live":
            if payload.get("source", "usdc").lower() != "usdc" or payload.get("target", "usdp").lower() != "usdp":
                self._json(400, {"ok": False, "error": "Live execution supports USDC -> USDP only; other pairs are quote-only"})
                return
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
                        *([] if resting else ["--once", "--no-capacity-check"]),
                        "--no-monitor",
                        "--interval", str(RESTING_INTERVAL),
                        "--quantity", str(RESTING_QUANTITY),
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
