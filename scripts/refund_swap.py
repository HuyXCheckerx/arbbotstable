#!/usr/bin/env python3
"""Automated refund caller for Stable.com HTLC source swap.

Watches block timestamp until lockUntil (1792776683) is reached,
then broadcasts refundSource to transfer 1000 USDC back to user.
"""

from __future__ import annotations
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
from web3 import Web3

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)

RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon.drpc.org")
PRIVATE_KEY = os.getenv("POLYGON_OPERATOR_PRIVATE_KEY", "")
CROSS_CHAIN_CONTRACT = "0xC6d27843426F31ef378D0C2101384519811933cf"

REFUND_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {
                        "components": [
                            {"name": "secretHash", "type": "bytes32"},
                            {"name": "amount", "type": "uint256"},
                            {"name": "token", "type": "address"},
                            {"name": "user", "type": "address"},
                            {"name": "chainId", "type": "uint64"},
                        ],
                        "name": "base",
                        "type": "tuple",
                    },
                    {"name": "sessionAddress", "type": "address"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "refundSource",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "", "type": "bytes32"}],
        "name": "swaps",
        "outputs": [
            {"name": "lockUntil", "type": "uint32"},
            {"name": "status", "type": "uint8"},
            {"name": "paramsHash", "type": "bytes27"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

def main():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    account = w3.eth.account.from_key(PRIVATE_KEY)
    user_addr = account.address
    print(f"[*] Operator Address: {user_addr}", flush=True)

    htlc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(CROSS_CHAIN_CONTRACT),
        abi=REFUND_ABI,
    )

    secret_hash_hex = "c14e0e7d4fbf171dc97955abd04a708e92ab9423f0cf27759a7bf6b93968c518"
    secret_hash = bytes.fromhex(secret_hash_hex)
    amount_raw = 1000 * 10**6
    token_addr = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
    session_addr = Web3.to_checksum_address("0xB8e27cfa6A1A268acB3131AeAdB399acF5836A4C")

    # Check status
    lock_until, status, _ = htlc_contract.functions.swaps(secret_hash).call()
    print(f"[*] Swap Lock Until: {lock_until} (Status: {status})", flush=True)

    refund_params = (
        (
            secret_hash,
            amount_raw,
            token_addr,
            user_addr,
            137,
        ),
        session_addr,
    )

    while True:
        curr_time = int(time.time())
        diff = lock_until - curr_time
        if diff > 0:
            print(f"[i] Waiting for lock expiry... {diff}s remaining (approx {diff/60:.1f} mins)", flush=True)
            time.sleep(min(30, max(5, diff)))
        else:
            print("\n[+] Lock expired! Executing refundSource...", flush=True)
            break

    # Build and broadcast refund transaction
    gas_price = max(int(w3.eth.gas_price * 1.3), w3.to_wei(35, "gwei"))
    tx = htlc_contract.functions.refundSource(refund_params).build_transaction({
        "from": user_addr,
        "nonce": w3.eth.get_transaction_count(user_addr),
        "gasPrice": gas_price,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    print(f"[+] refundSource broadcasted: {tx_hash}", flush=True)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"[+] Refund Confirmed in Block #{receipt['blockNumber']} (Status: {receipt['status']})", flush=True)
    print(f"[+] 1000 USDC successfully refunded to {user_addr}!", flush=True)

if __name__ == "__main__":
    main()
