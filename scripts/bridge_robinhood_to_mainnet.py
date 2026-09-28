#!/usr/bin/env python3
"""Bridge all Robinhood Chain USDG and ETH to Ethereum Mainnet using Relay.link protocol."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import time
import urllib.request

# Patch DNS for rpc.mainnet.chain.robinhood.com if blocked/routed to 127.0.0.1 locally
orig_getaddrinfo = socket.getaddrinfo

def patched_getaddrinfo(host, port, *args, **kwargs):
    if host == "rpc.mainnet.chain.robinhood.com":
        return orig_getaddrinfo("104.20.46.209", port, *args, **kwargs)
    return orig_getaddrinfo(host, port, *args, **kwargs)

socket.getaddrinfo = patched_getaddrinfo

from dotenv import load_dotenv
from web3 import Web3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)

ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_CHAIN_ID = 4663
MAINNET_CHAIN_ID = 1

USDG_ROBINHOOD = Web3.to_checksum_address("0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")
NATIVE_ZERO = "0x0000000000000000000000000000000000000000"

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

def get_relay_quote(user: str, origin_currency: str, amount_raw: int) -> dict:
    url = "https://api.relay.link/quote"
    payload = {
        "user": user,
        "originChainId": ROBINHOOD_CHAIN_ID,
        "destinationChainId": MAINNET_CHAIN_ID,
        "originCurrency": origin_currency,
        "destinationCurrency": NATIVE_ZERO,
        "amount": str(amount_raw),
        "recipient": user,
        "tradeType": "EXACT_INPUT"
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

def wait_for_relay_fill(request_id: str, timeout: int = 180) -> dict | None:
    print(f"Monitoring Relay fill for request: {request_id}...")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(4)
        try:
            url = f"https://api.relay.link/requests/{request_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                status = data.get("status")
                print(f"  Relay status: {status} ({int(time.time() - t0)}s)")
                if status == "success":
                    return data
                elif status in ("failure", "refunded"):
                    print(f"Relay request ended with status: {status}")
                    return data
        except Exception as exc:
            pass
    print("Timed out waiting for Relay fill confirmation (check explorer).")
    return None

def main():
    private_key = os.getenv("ETH_OPERATOR_PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: ETH_OPERATOR_PRIVATE_KEY is missing from .env")
        sys.exit(1)

    w3_rh = Web3(Web3.HTTPProvider(ROBINHOOD_RPC, request_kwargs={"timeout": 20}))
    if not w3_rh.is_connected():
        print(f"ERROR: Failed to connect to Robinhood RPC: {ROBINHOOD_RPC}")
        sys.exit(1)

    account = w3_rh.eth.account.from_key(private_key)
    user_address = account.address
    print(f"=== Robinhood to Ethereum Mainnet Bridge ===")
    print(f"User Address: {user_address}")

    # Check Balances on Robinhood Chain
    rh_eth_bal_wei = w3_rh.eth.get_balance(user_address)
    rh_eth_bal = float(w3_rh.from_wei(rh_eth_bal_wei, "ether"))

    usdg_contract = w3_rh.eth.contract(address=USDG_ROBINHOOD, abi=ERC20_ABI)
    usdg_bal_raw = usdg_contract.functions.balanceOf(user_address).call()
    usdg_decimals = usdg_contract.functions.decimals().call()
    usdg_bal = usdg_bal_raw / (10 ** usdg_decimals)

    print(f"Robinhood Chain USDG Balance: {usdg_bal:,.6f} USDG ({usdg_bal_raw} raw)")
    print(f"Robinhood Chain ETH Balance:  {rh_eth_bal:.6f} ETH ({rh_eth_bal_wei} wei)")

    # Check Mainnet Starting ETH Balance
    w3_eth = Web3(Web3.HTTPProvider(os.getenv("ETH_RPC_URL", "https://eth.drpc.org")))
    start_mainnet_bal = w3_eth.from_wei(w3_eth.eth.get_balance(user_address), "ether")
    print(f"Mainnet Starting ETH Balance: {start_mainnet_bal:.6f} ETH")

    if usdg_bal_raw == 0 and rh_eth_bal_wei < 100000000000000:
        print("No significant assets found on Robinhood Chain to bridge.")
        return

    # -------------------------------------------------------------
    # Step 1: Bridge All USDG -> Mainnet ETH
    # -------------------------------------------------------------
    if usdg_bal_raw > 0:
        print("\n[Step 1] Preparing Relay quote to swap all USDG -> Mainnet ETH...")
        quote = get_relay_quote(user_address, USDG_ROBINHOOD, usdg_bal_raw)
        details = quote.get("details", {})
        expected_eth = details.get("currencyOut", {}).get("amountFormatted")
        usd_value = details.get("currencyOut", {}).get("amountUsd")
        print(f"  Bridging: {usdg_bal:,.6f} USDG")
        print(f"  Expected Mainnet Output: ~{expected_eth} ETH (~${usd_value})")

        steps = quote.get("steps", [])
        for step in steps:
            step_id = step.get("id")
            for item in step.get("items", []):
                tx_data = item.get("data", {})
                to_addr = Web3.to_checksum_address(tx_data["to"])
                calldata = tx_data["data"]
                val = int(tx_data.get("value", 0))

                nonce = w3_rh.eth.get_transaction_count(user_address, "pending")
                gas_est = w3_rh.eth.estimate_gas({"from": user_address, "to": to_addr, "data": calldata, "value": val})
                gas_limit = int(gas_est * 1.25)
                gas_price = int(w3_rh.eth.gas_price * 1.15)

                tx_params = {
                    "from": user_address,
                    "to": to_addr,
                    "value": val,
                    "data": calldata,
                    "nonce": nonce,
                    "gas": gas_limit,
                    "gasPrice": gas_price,
                    "chainId": ROBINHOOD_CHAIN_ID
                }

                print(f"  Broadcasting {step_id} tx to {to_addr[:10]}... (gas: {gas_limit})")
                signed = account.sign_transaction(tx_params)
                tx_hash = w3_rh.eth.send_raw_transaction(signed.raw_transaction)
                print(f"  {step_id} Tx Hash: {tx_hash.hex()}")
                receipt = w3_rh.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                if receipt.status != 1:
                    print(f"ERROR: {step_id} transaction failed on-chain!")
                    sys.exit(1)
                print(f"  {step_id} confirmed in block {receipt.blockNumber}!")

        # Wait for Relay solver execution
        req_id = quote.get("requestId") or (steps[-1].get("items", [{}])[0].get("check", {}).get("endpoint", "").split("/")[-1])
        if req_id:
            wait_for_relay_fill(req_id)

    # -------------------------------------------------------------
    # Step 2: Bridge Remaining Robinhood ETH -> Mainnet ETH
    # -------------------------------------------------------------
    rh_eth_bal_wei = w3_rh.eth.get_balance(user_address)
    # Reserve 0.00015 ETH for gas margin on Robinhood Chain
    reserve_wei = 150000000000000  # 0.00015 ETH
    if rh_eth_bal_wei > reserve_wei + 200000000000000:
        eth_to_bridge_wei = rh_eth_bal_wei - reserve_wei
        eth_to_bridge = float(w3_rh.from_wei(eth_to_bridge_wei, "ether"))
        print(f"\n[Step 2] Bridging remaining {eth_to_bridge:.6f} Robinhood ETH -> Mainnet ETH...")
        quote_eth = get_relay_quote(user_address, NATIVE_ZERO, eth_to_bridge_wei)
        details_eth = quote_eth.get("details", {})
        expected_out = details_eth.get("currencyOut", {}).get("amountFormatted")
        print(f"  Expected Mainnet Output: ~{expected_out} ETH")

        for step in quote_eth.get("steps", []):
            for item in step.get("items", []):
                tx_data = item.get("data", {})
                to_addr = Web3.to_checksum_address(tx_data["to"])
                calldata = tx_data.get("data", "0x")
                val = int(tx_data.get("value", eth_to_bridge_wei))

                nonce = w3_rh.eth.get_transaction_count(user_address, "pending")
                gas_est = w3_rh.eth.estimate_gas({"from": user_address, "to": to_addr, "data": calldata, "value": val})
                gas_limit = int(gas_est * 1.25)
                gas_price = int(w3_rh.eth.gas_price * 1.15)

                tx_params = {
                    "from": user_address,
                    "to": to_addr,
                    "value": val,
                    "data": calldata,
                    "nonce": nonce,
                    "gas": gas_limit,
                    "gasPrice": gas_price,
                    "chainId": ROBINHOOD_CHAIN_ID
                }

                print(f"  Broadcasting ETH deposit to Relay depository {to_addr[:10]}...")
                signed = account.sign_transaction(tx_params)
                tx_hash = w3_rh.eth.send_raw_transaction(signed.raw_transaction)
                print(f"  Deposit Tx Hash: {tx_hash.hex()}")
                receipt = w3_rh.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                if receipt.status != 1:
                    print("ERROR: ETH deposit transaction failed!")
                    sys.exit(1)
                print(f"  ETH deposit confirmed in block {receipt.blockNumber}!")

        req_id_eth = quote_eth.get("requestId")
        if req_id_eth:
            wait_for_relay_fill(req_id_eth)

    # -------------------------------------------------------------
    # Final Balance Check on Ethereum Mainnet
    # -------------------------------------------------------------
    time.sleep(3)
    final_mainnet_bal = w3_eth.from_wei(w3_eth.eth.get_balance(user_address), "ether")
    diff = float(final_mainnet_bal - start_mainnet_bal)
    print("\n" + "=" * 65)
    print(f"BRIDGE COMPLETE!")
    print(f"Ethereum Mainnet Balance Before: {start_mainnet_bal:.6f} ETH")
    print(f"Ethereum Mainnet Balance After:  {final_mainnet_bal:.6f} ETH (+{diff:.6f} ETH / ~${diff * 2646:.2f})")
    print("=" * 65)

if __name__ == "__main__":
    main()
