#!/usr/bin/env python3
"""Cross-chain finality verification and diagnostic telemetry harness.

Tests and monitors Stable.com cross-chain settlement latency across:
  - Ethereum (ETH, Chain 101)
  - Polygon (POL, Chain 106)
  - BNB Smart Chain (BSC, Chain 103)
Destination: Solana (SOL, Chain 102)

Measures exact millisecond deltas and source block confirmations between:
  1. Transaction broadcast
  2. Transaction mined (1st confirmation)
  3. Server acknowledges source lock
  4. Server broadcasts destination lock on Solana

Usage:
  python scripts/test_crosschain_finality.py --chain poly --dry-run
  python scripts/test_crosschain_finality.py --chain bsc --dry-run
  python scripts/test_crosschain_finality.py --chain eth --dry-run
  python scripts/test_crosschain_finality.py --chain poly --live --amount 1000
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
sys.stdout.reconfigure(encoding="utf-8")
import time
from typing import Any
from decimal import Decimal

import coincurve
from dotenv import load_dotenv
import requests
from web3 import Web3

# Load project environment
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)

STABLE_API = "https://api-defi.stable.com"
CROSS_CHAIN_CONTRACT = "0xC6d27843426F31ef378D0C2101384519811933cf"

# ERC-20 Minimal ABI
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]

# Stable.com ICrossChainHTLC lockSource ABI
HTLC_ABI = [
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
                    {"name": "nonce", "type": "uint256"},
                    {"name": "sessionAddress", "type": "address"},
                    {"name": "maintainerDeadline", "type": "uint64"},
                    {"name": "maintainerSig", "type": "bytes"},
                ],
                "name": "params",
                "type": "tuple",
            },
            {"name": "executionFeeNative", "type": "uint256"},
        ],
        "name": "lockSource",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "pool",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(frozen=True)
class ChainConfig:
    name: str
    stable_chain_id: str
    evm_chain_id: int
    rpc_url: str
    private_key: str
    token_symbol: str
    token_address: str
    decimals: int


CHAIN_CONFIGS: dict[str, dict[str, Any]] = {
    "eth": {
        "name": "Ethereum",
        "stable_chain_id": "101",
        "evm_chain_id": 1,
        "rpc_env": "ETH_RPC_URL",
        "default_rpc": "https://eth.drpc.org",
        "pk_env": "ETH_OPERATOR_PRIVATE_KEY",
        "token_symbol": "USDC",
        "token_address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "decimals": 6,
    },
    "poly": {
        "name": "Polygon",
        "stable_chain_id": "106",
        "evm_chain_id": 137,
        "rpc_env": "POLYGON_RPC_URL",
        "default_rpc": "https://polygon-bor-rpc.publicnode.com",
        "pk_env": "POLYGON_OPERATOR_PRIVATE_KEY",
        "token_symbol": "USDC",
        "token_address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "decimals": 6,
    },
    "bsc": {
        "name": "BNB Chain",
        "stable_chain_id": "103",
        "evm_chain_id": 56,
        "rpc_env": "BSC_RPC_URL",
        "default_rpc": "https://bsc-dataseed.binance.org/",
        "pk_env": "BSC_OPERATOR_PRIVATE_KEY",
        "token_symbol": "USDT",
        "token_address": "0x55d398326f99059fF775485246999027B3197955",
        "decimals": 18,
    },
}


class CrossChainTelemetryTester:
    def __init__(self, chain_key: str, destination_solana: str):
        if chain_key not in CHAIN_CONFIGS:
            raise ValueError(f"Unsupported chain: {chain_key}. Must be eth, poly, or bsc.")

        raw_conf = CHAIN_CONFIGS[chain_key]
        rpc_url = os.getenv(raw_conf["rpc_env"], raw_conf["default_rpc"])
        pk = os.getenv(raw_conf["pk_env"], "")

        self.config = ChainConfig(
            name=raw_conf["name"],
            stable_chain_id=raw_conf["stable_chain_id"],
            evm_chain_id=raw_conf["evm_chain_id"],
            rpc_url=rpc_url,
            private_key=pk,
            token_symbol=raw_conf["token_symbol"],
            token_address=Web3.to_checksum_address(raw_conf["token_address"]),
            decimals=raw_conf["decimals"],
        )
        self.dest_solana = destination_solana
        self.w3 = Web3(Web3.HTTPProvider(self.config.rpc_url))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://stable.com",
            "Referer": "https://stable.com/",
        })

        # Generate ephemeral session key
        self.session_priv = secrets.token_bytes(32)
        curve_key = coincurve.PrivateKey(self.session_priv)
        self.session_pub_hex = curve_key.public_key.format(compressed=False).hex()
        # Ethereum address derived from uncompressed pubkey (keccak256(pubkey[1:])[-20:])
        pub_bytes = bytes.fromhex(self.session_pub_hex)[1:]
        self.session_address = Web3.to_checksum_address(Web3.keccak(pub_bytes)[-20:].hex())

    def get_user_address(self) -> str:
        if not self.config.private_key:
            return "0x000000000000000000000000000000000000dEaD"
        account = self.w3.eth.account.from_key(self.config.private_key)
        return account.address

    def query_status(self, amount_human: str) -> dict[str, Any]:
        payload = {
            "chainFrom": self.config.stable_chain_id,
            "assetFrom": self.config.token_symbol,
            "chainTo": "102",  # Solana
            "assetTo": "USDC",
            "amountFrom": amount_human,
            "addressFrom": self.get_user_address(),
            "addressTo": self.dest_solana,
            "gasLess": False,
        }
        res = self.session.post(f"{STABLE_API}/swap/status", json=payload, timeout=10)
        res.raise_for_status()
        return res.json()

    def create_order(self, amount_human: str) -> dict[str, Any]:
        payload = {
            "chainFrom": self.config.stable_chain_id,
            "assetFrom": self.config.token_symbol,
            "chainTo": "102",  # Solana
            "assetTo": "USDC",
            "amountFrom": amount_human,
            "addressFrom": self.get_user_address(),
            "addressTo": self.dest_solana,
            "gasLess": False,
            "sessionPubkey": self.session_pub_hex,
        }
        res = self.session.post(f"{STABLE_API}/swap/create/crossChain", json=payload, timeout=12)
        if res.status_code != 200:
            raise RuntimeError(f"Order creation failed [{res.status_code}]: {res.text}")
        return res.json().get("data", res.json())

    def monitor_settlement_telemetry(self, secret_hash: str, tx_hash: str):
        print("\n" + "=" * 70)
        print("  LIVE SETTLEMENT LATENCY & CONFIRMATION MONITORING")
        print("=" * 70)
        print(f"[*] Target SecretHash: 0x{secret_hash}")
        print(f"[*] Source Tx Hash:   {tx_hash}")

        # Wait for block inclusion
        mined_time = None
        mined_block = None
        while True:
            try:
                receipt = self.w3.eth.get_transaction_receipt(tx_hash)
                if receipt and receipt.get("blockNumber") is not None:
                    mined_block = receipt["blockNumber"]
                    mined_time = time.time()
                    print(f"[+] Mined on {self.config.name} at Block #{mined_block} (Status: {receipt['status']})")
                    break
            except Exception:
                pass
            time.sleep(0.3)

        # Notify backend
        print("[*] Dispatching /swap/notify/userLockSourceTransaction...")
        try:
            self.session.post(
                f"{STABLE_API}/swap/notify/userLockSourceTransaction",
                json={"transactionID": tx_hash, "secretHash": secret_hash},
                timeout=5,
            )
        except Exception as e:
            print(f"[!] Notification warning: {e}")

        # High-frequency poll for destination lock
        print("[*] Polling backend order state every 400ms...")
        last_reported_block = mined_block

        while True:
            now = time.time()
            elapsed = now - mined_time
            curr_block = self.w3.eth.block_number
            confs = curr_block - mined_block + 1

            if curr_block != last_reported_block:
                print(f"    [Source Block #{curr_block}] Confirmations: {confs} | Elapsed: {elapsed:.2f}s")
                last_reported_block = curr_block

            try:
                res = self.session.get(f"{STABLE_API}/swap/crossChain/status?secretHash={secret_hash}", timeout=4)
                if res.status_code == 200:
                    status = res.json().get("data", {})
                    dest_tx = status.get("destinationLockTransactionId")
                    if dest_tx:
                        dest_time = time.time()
                        total_latency = dest_time - mined_time
                        print("\n" + "=" * 70)
                        print("  [!] DESTINATION LOCK BROADCAST DETECTED!")
                        print("=" * 70)
                        print(f"[*] Solana Destination Tx: {dest_tx}")
                        print(f"[*] Total Latency from Source Mined: {total_latency:.2f} seconds")
                        print(f"[*] Source Confirmations at Broadcast: {confs} blocks")
                        print("=" * 70)

                        if confs <= 2 and total_latency <= 5.0:
                            print("\n[VERDICT: VULNERABLE]")
                            print(f"--> Server broadcasted to Solana on {confs} confirmation(s).")
                            print("--> Confirmed: The backend relies on soft unfinalized source blocks.")
                        else:
                            print("\n[VERDICT: HARDENED]")
                            print(f"--> Server waited {confs} confirmations ({total_latency:.1f}s).")
                            print("--> Confirmed: Backend enforces multi-block confirmation margins.")
                        break
            except Exception:
                pass

            time.sleep(0.4)

    def execute_live_swap(self, amount_human: str):
        if not self.config.private_key:
            raise RuntimeError(f"Private key not configured in {CHAIN_CONFIGS[self.config.name.lower()[:3]]['pk_env']}")

        account = self.w3.eth.account.from_key(self.config.private_key)
        user_addr = account.address
        amount_raw = int(float(amount_human) * 10**self.config.decimals)

        print(f"[*] Checking token balance on {self.config.name}...")
        token_contract = self.w3.eth.contract(address=self.config.token_address, abi=ERC20_ABI)
        balance = token_contract.functions.balanceOf(user_addr).call()
        if balance < amount_raw:
            raise RuntimeError(f"Insufficient {self.config.token_symbol} balance: {balance / 10**self.config.decimals} < {amount_human}")

        # Check allowance to Liquidity Pool (the custody contract)
        htlc_addr = Web3.to_checksum_address(CROSS_CHAIN_CONTRACT)
        htlc_contract = self.w3.eth.contract(address=htlc_addr, abi=HTLC_ABI)
        pool_addr = htlc_contract.functions.pool().call()

        allowance = token_contract.functions.allowance(user_addr, pool_addr).call()
        if allowance < amount_raw:
            print(f"[*] Approving {self.config.token_symbol} allowance to Liquidity Pool ({pool_addr})...")
            gas_price = max(int(self.w3.eth.gas_price * 1.3), self.w3.to_wei(35, "gwei"))
            tx = token_contract.functions.approve(pool_addr, 2**256 - 1).build_transaction({
                "from": user_addr,
                "nonce": self.w3.eth.get_transaction_count(user_addr),
                "gasPrice": gas_price,
            })
            signed = account.sign_transaction(tx)
            tx_h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"[*] Approve tx broadcasted: {tx_h.hex()}")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_h)
            if receipt["status"] != 1:
                raise RuntimeError(f"Token approval failed on-chain: {receipt}")
            print("[+] Allowance approved.")

        # Request order parameters
        print(f"[*] Creating cross-chain order for {amount_human} {self.config.token_symbol} -> SOL USDC...")
        order = self.create_order(amount_human)
        secret_hash = order["secretHash"].replace("0x", "")
        maintainer_sig = bytes.fromhex(order["maintainerSignature"].replace("0x", ""))
        deadline = int(order["deadline"])
        nonce = int(order["nonce"])
        # Exact decimal conversion avoids float precision truncation
        exec_fee_wei = int(Decimal(order["executionFeeNative"]) * Decimal(10**18))

        print(f"[+] Order created: {order.get('orderId')}")
        print(f"    SecretHash: 0x{secret_hash}")
        print(f"    Maintainer Deadline: {deadline} (Unix)")
        print(f"    Execution Fee Native: {exec_fee_wei} wei")

        # Build lockSource call
        lock_params = (
            (
                bytes.fromhex(secret_hash),
                amount_raw,
                self.config.token_address,
                user_addr,
                self.config.evm_chain_id,
            ),
            nonce,
            self.session_address,
            deadline,
            maintainer_sig,
        )

        # Pre-flight simulation via eth_call
        print("[*] Running pre-flight simulation (eth_call) to verify parameters...")
        try:
            htlc_contract.functions.lockSource(lock_params, exec_fee_wei).call({
                "from": user_addr,
                "value": exec_fee_wei,
            })
            print("[+] Pre-flight simulation SUCCEEDED with 0 errors!")
        except Exception as e:
            raise RuntimeError(f"Pre-flight lockSource simulation failed: {e}")

        # Estimate gas and build transaction
        gas_price = max(int(self.w3.eth.gas_price * 1.3), self.w3.to_wei(35, "gwei"))
        estimated_gas = htlc_contract.functions.lockSource(lock_params, exec_fee_wei).estimate_gas({
            "from": user_addr,
            "value": exec_fee_wei,
        })
        gas_limit = int(estimated_gas * 1.25)

        tx = htlc_contract.functions.lockSource(lock_params, exec_fee_wei).build_transaction({
            "from": user_addr,
            "value": exec_fee_wei,
            "nonce": self.w3.eth.get_transaction_count(user_addr),
            "gas": gas_limit,
            "gasPrice": gas_price,
        })
        signed = account.sign_transaction(tx)
        print(f"[*] Broadcasting lockSource on {self.config.name} (gas: {gas_limit}, gasPrice: {gas_price / 1e9:.2f} Gwei)...")
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        print(f"[+] lockSource broadcasted: {tx_hash}")

        # Begin telemetry monitoring
        self.monitor_settlement_telemetry(secret_hash, tx_hash)


def main():
    parser = argparse.ArgumentParser(description="Stable.com Cross-Chain Finality Verification Harness")
    parser.add_argument("--chain", choices=["eth", "poly", "bsc"], default="poly", help="Source EVM chain")
    parser.add_argument("--solana-dest", default=os.getenv("CROSSCHAIN_SOLANA_ADDRESS", "G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he"), help="Destination Solana address")
    parser.add_argument("--amount", default="1000", help="Human amount of tokens (minimum 1000)")
    parser.add_argument("--dry-run", action="store_true", help="Perform quote and order creation check without broadcasting")
    parser.add_argument("--live", action="store_true", help="Execute on-chain lockSource and monitor server latency")
    args = parser.parse_args()

    tester = CrossChainTelemetryTester(args.chain, args.solana_dest)
    print("=" * 70)
    print(f"  STABLE.COM CROSS-CHAIN DIAGNOSTIC [{tester.config.name.upper()} -> SOLANA]")
    print("=" * 70)
    print(f"[*] User EVM Address:       {tester.get_user_address()}")
    print(f"[*] Destination Solana:     {tester.dest_solana}")
    print(f"[*] Ephemeral Session Addr: {tester.session_address}")

    if args.dry_run or not args.live:
        print("\n--- DRY-RUN / QUOTE & CAPACITY INSPECTION ---")
        status = tester.query_status(args.amount)
        asset_info = status.get("asset", {})
        print(f"[+] Quote Status: OK")
        print(f"    Available Capacity: {asset_info.get('balance')} {tester.config.token_symbol}")
        print(f"    Min Amount:        {asset_info.get('min')}")
        print(f"    Max Amount:        {asset_info.get('max')}")
        print(f"    Native Fee USD:    ${asset_info.get('nativeFeeUsd')}")
        print(f"    Execution Fee USD: ${asset_info.get('executionFeeUSD')}")

        print("\n--- TESTING API ORDER CREATION ---")
        order = tester.create_order(args.amount)
        print(f"[+] Successfully generated maintainer order signature!")
        print(f"    Order ID:    {order.get('orderId')}")
        print(f"    SecretHash:  0x{order.get('secretHash')}")
        print(f"    Deadline:    {order.get('deadline')}")
        print(f"    Nonce:       {order.get('nonce')}")
        print("\n[i] Dry-run passed. To broadcast a live swap and track settlement latency, run with --live")
    else:
        tester.execute_live_swap(args.amount)


if __name__ == "__main__":
    main()
