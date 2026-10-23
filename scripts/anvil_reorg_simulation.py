#!/usr/bin/env python3
"""Local Fork Reorganization Injection Harness (Anvil / Hardhat Node)

Demonstrates how low-confirmation (confirmations = 1) bridge listeners can be
exploited via micro-reorgs on forked EVM state without risking live capital.

Lifecycle:
  1. Connect to an Anvil fork (e.g., Polygon, BSC, or Ethereum).
  2. Take a state snapshot (evm_snapshot).
  3. Broadcast a mock lock transaction on Branch A.
  4. Mine 1 block (simulating `waitForTransaction(hash, 1)` confirmation).
  5. Verify transaction is confirmed on Branch A.
  6. Revert to snapshot (evm_revert), simulating a chain reorganization.
  7. Mine competing blocks on Branch B without the transaction.
  8. Demonstrate receipt invalidation / orphaned transaction state.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
import requests


class AnvilReorgHarness:
    def __init__(self, rpc_url: str = "http://127.0.0.1:8545"):
        self.rpc_url = rpc_url
        self.session = requests.Session()
        self._req_id = 0

    def rpc(self, method: str, params: list[Any] | None = None) -> Any:
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or [],
        }
        res = self.session.post(self.rpc_url, json=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        if "error" in data:
            raise RuntimeError(f"RPC Error [{method}]: {data['error']}")
        return data.get("result")

    def get_block_number(self) -> int:
        return int(self.rpc("eth_blockNumber"), 16)

    def snapshot(self) -> str:
        snapshot_id = self.rpc("evm_snapshot")
        print(f"[*] Created EVM Snapshot: {snapshot_id}")
        return snapshot_id

    def revert(self, snapshot_id: str) -> bool:
        success = self.rpc("evm_revert", [snapshot_id])
        print(f"[*] Reverted EVM to Snapshot {snapshot_id}: {success}")
        return bool(success)

    def mine_blocks(self, count: int = 1) -> None:
        try:
            self.rpc("anvil_mine", [hex(count), hex(0)])
        except RuntimeError:
            for _ in range(count):
                self.rpc("evm_mine")
        print(f"[*] Mined {count} block(s). Current block: {self.get_block_number()}")

    def get_accounts(self) -> list[str]:
        return self.rpc("eth_accounts")

    def send_transaction(self, from_addr: str, to_addr: str, value_wei: int) -> str:
        tx_hash = self.rpc("eth_sendTransaction", [{
            "from": from_addr,
            "to": to_addr,
            "value": hex(value_wei),
            "gas": hex(100000),
            "gasPrice": hex(300_000_000_000),
        }])
        print(f"[*] Broadcasted Transaction: {tx_hash}")
        return tx_hash

    def get_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        try:
            return self.rpc("eth_getTransactionReceipt", [tx_hash])
        except RuntimeError as e:
            # Fork node errors when looking up an orphaned tx against upstream archive
            err_msg = str(e).lower()
            if "archive" in err_msg or "not found" in err_msg or "-32603" in err_msg:
                return None
            raise


def run_simulation(rpc_url: str):
    print("=" * 68)
    print("  ANVIL FORK REORGANIZATION INJECTION SIMULATION")
    print("=" * 68)
    
    harness = AnvilReorgHarness(rpc_url)
    
    try:
        block_init = harness.get_block_number()
        print(f"[*] Connected to Anvil. Starting Block Number: #{block_init}")
    except Exception as e:
        print(f"[!] Failed to connect to Anvil RPC at {rpc_url}: {e}")
        print("    Ensure Anvil is running. Example:")
        print("    anvil --fork-url https://polygon-bor-rpc.publicnode.com --port 8545")
        sys.exit(1)

    accounts = harness.get_accounts()
    if not accounts or len(accounts) < 2:
        print("[!] Not enough test accounts available on Anvil node.")
        sys.exit(1)

    alice, bob = accounts[0], accounts[1]
    print(f"[*] Sender (User):      {alice}")
    print(f"[*] Recipient (Bridge):  {bob}")

    # Step 1: Take snapshot at canonical height N
    print("\n--- STEP 1: Snapshot Initial Canonical State ---")
    snapshot_id = harness.snapshot()

    # Step 2: Simulate lock transaction on Branch A
    print("\n--- STEP 2: Submit 'lockSource' Equivalent on Branch A ---")
    tx_hash = harness.send_transaction(alice, bob, 1_000_000_000_000_000_000)

    # Step 3: Mine 1 block (Simulating client waitForTransaction(hash, 1))
    print("\n--- STEP 3: Mine 1 Block on Branch A ---")
    harness.mine_blocks(1)

    # Step 4: Verify receipt from the perspective of low-confirmation listener
    receipt_a = harness.get_receipt(tx_hash)
    if receipt_a and receipt_a.get("blockNumber"):
        tx_block = int(receipt_a["blockNumber"], 16)
        status = int(receipt_a.get("status", "0x0"), 16)
        print(f"[+] Confirmed on Branch A at Block #{tx_block} (Status: {status})")
        print(f"[+] Vulnerable Listener (confirmations = 1): STATUS APPROVED")
        print(f"    --> Backend notifies: 'sourceLockTransactionId confirmed'")
        print(f"    --> Relayer executes: 'lockDestination()' on target chain.")
    else:
        print("[!] Transaction failed to confirm on Branch A.")
        sys.exit(1)

    # Step 5: Inject Reorganization (Revert to Snapshot)
    print("\n--- STEP 4: Inject Reorganization (Rollback Branch A) ---")
    harness.revert(snapshot_id)
    print(f"[*] Height after rollback: #{harness.get_block_number()}")

    # Step 6: Mine competing blocks on Branch B without Alice's transaction
    print("\n--- STEP 5: Mine Competing Blocks on Branch B ---")
    harness.mine_blocks(3)

    # Step 7: Re-evaluate receipt status after reorg
    print("\n--- STEP 6: Post-Reorg Verification Audit ---")
    receipt_b = harness.get_receipt(tx_hash)
    block_curr = harness.get_block_number()

    print(f"[*] Current Canonical Block: #{block_curr}")
    if receipt_b is None or receipt_b.get("blockNumber") is None:
        print("\n" + "=" * 68)
        print("  [!] EXPLOIT CONFIRMED: TRANSACTION WAS COMPLETELY ORPHANED!")
        print(f"      Tx Hash: {tx_hash}")
        print("      Status on Canonical Branch: NULL / ORPHANED")
        print("      Root Cause: Bridge executed destination payout on 1 confirmation,")
        print("      before finality checkpoint. Source collateral was never debited.")
        print("=" * 68)
    else:
        print(f"[*] Note: Transaction was re-included in mempool at block #{int(receipt_b['blockNumber'], 16)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Anvil Reorg Injection Test Harness")
    parser.add_argument("--rpc", default="http://127.0.0.1:8545", help="Anvil RPC endpoint")
    args = parser.parse_args()
    run_simulation(args.rpc)
