#!/usr/bin/env python3
"""Deploy an EIP-1167 minimal proxy pointing to the master MorphoMatchaStableArbUsdc implementation.

Deployment cost: ~86,000 gas (~$0.05–$0.15 USD).
Initializes the proxy's storage slot 0 with the operator address upon creation.
Requires zero compiler or Solc overhead; builds raw 80-byte initcode.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
import time

try:
    from dotenv import load_dotenv
    from web3 import Web3
except ImportError as exc:
    raise SystemExit("Missing dependencies: install web3 and python-dotenv") from exc

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_DIR / ".env", override=True)

# ==============================================================================
# HARDCODED PROTOCOL & ADDRESS CONSTANTS (Only private key is secret)
# ==============================================================================
CHAIN_ID = 1
OPERATOR_ADDRESS = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
MASTER_IMPLEMENTATION = "0xD48Ab89581e77b103014DCC922279855a3b2a940"
DEFAULT_RPC_URL = "https://ethereum.publicnode.com"

# Protocol tokens & targets
PYUSD = "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8"
USDG = "0xe343167631d89B6Ffc58B88d6b7fB0228795491D"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
ALLOWANCE_HOLDER = "0x0000000000001fF3684f28c67538d4D072C22734"
STABLE_POOL = "0x4879Fe4B72b4c10658a5c3702A3e970221E72754"

MINIMAL_ABI = [
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "token", "type": "address"}],
        "name": "supportsLoanToken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint8", "name": "provider", "type": "uint8"}],
        "name": "supportsFlashProvider",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "pure",
        "type": "function",
    },
]


class ProxyDeploymentError(RuntimeError):
    pass


def build_proxy_initcode(owner_address: str = OPERATOR_ADDRESS, implementation_address: str = MASTER_IMPLEMENTATION) -> bytes:
    """Builds an 80-byte initcode that sets owner in storage slot 0 and returns standard EIP-1167 runtime code."""
    owner_clean = owner_address.lower().replace("0x", "")
    impl_clean = implementation_address.lower().replace("0x", "")
    if len(owner_clean) != 40:
        raise ValueError(f"Invalid owner address: {owner_address}")
    if len(impl_clean) != 40:
        raise ValueError(f"Invalid implementation address: {implementation_address}")

    # Standard 45-byte EIP-1167 delegatecall runtime bytecode
    # 363d3d373d3d3d363d73 <implementation> 5af43d82803e903d91602b57fd5bf3
    runtime_code = bytes.fromhex(f"363d3d373d3d3d363d73{impl_clean}5af43d82803e903d91602b57fd5bf3")
    assert len(runtime_code) == 45, f"Runtime code length is {len(runtime_code)}"

    # 35-byte initcode header (0x23):
    # PUSH20 owner, PUSH1 0, SSTORE (stores owner into slot 0)
    # PUSH1 45, DUP1, PUSH1 35, PUSH1 0, CODECOPY, PUSH1 0, RETURN
    header_len = 35
    init_header = bytes([
        0x73, *bytes.fromhex(owner_clean),  # PUSH20 owner
        0x60, 0x00,                         # PUSH1 0 (slot 0)
        0x55,                               # SSTORE
        0x60, 0x2d,                         # PUSH1 45 (runtime code size)
        0x80,                               # DUP1
        0x60, header_len,                   # PUSH1 35 (runtime code start offset)
        0x60, 0x00,                         # PUSH1 0 (memory dest)
        0x39,                               # CODECOPY
        0x60, 0x00,                         # PUSH1 0 (memory offset)
        0xf3,                               # RETURN
    ])
    assert len(init_header) == header_len
    return init_header + runtime_code


def update_env_file(new_executor_address: str) -> None:
    """Updates ETH_ARB_STABLECOIN_EXECUTOR in .env."""
    env_path = PROJECT_DIR / ".env"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if re.search(r"^ETH_ARB_STABLECOIN_EXECUTOR=.*$", content, flags=re.MULTILINE):
            new_content = re.sub(
                r"^ETH_ARB_STABLECOIN_EXECUTOR=.*$",
                f"ETH_ARB_STABLECOIN_EXECUTOR={new_executor_address}",
                content,
                flags=re.MULTILINE,
            )
        else:
            new_content = content + f"\nETH_ARB_STABLECOIN_EXECUTOR={new_executor_address}\n"
        env_path.write_text(new_content, encoding="utf-8")


def log_proxy_history(proxy_address: str, tx_hash: str, gas_used: int, block_number: int) -> None:
    """Records proxy deployment to logs/proxy_deployments.jsonl."""
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    history_file = log_dir / "proxy_deployments.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proxyAddress": proxy_address,
        "implementation": MASTER_IMPLEMENTATION,
        "owner": OPERATOR_ADDRESS,
        "transactionHash": tx_hash,
        "gasUsed": gas_used,
        "blockNumber": block_number,
    }
    with open(history_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def get_web3_connection() -> Web3:
    candidate_rpcs = [os.getenv("ETH_RPC_URL", "").strip()] + [
        u.strip() for u in os.getenv("ETH_RPC_FALLBACKS", "").split(",") if u.strip()
    ] + [DEFAULT_RPC_URL]
    for url in candidate_rpcs:
        if not url:
            continue
        try:
            cand = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if cand.is_connected() and cand.eth.chain_id == CHAIN_ID:
                return cand
        except Exception:
            continue
    raise ProxyDeploymentError("Could not connect to Ethereum mainnet RPC")


def deploy_minimal_proxy(
    private_key: str | None = None,
    owner_address: str = OPERATOR_ADDRESS,
    implementation: str = MASTER_IMPLEMENTATION,
    receipt_timeout: int = 120,
) -> tuple[str, str]:
    """Deploys a minimal proxy in 1 transaction (~86,000 gas), updates .env, and returns (proxy_address, tx_hash)."""
    pk = private_key or os.getenv("ETH_OPERATOR_PRIVATE_KEY", "").strip()
    if not pk:
        raise ProxyDeploymentError("ETH_OPERATOR_PRIVATE_KEY is missing from environment")

    web3 = get_web3_connection()
    account = web3.eth.account.from_key(pk)
    if account.address.lower() != owner_address.lower():
        raise ProxyDeploymentError(
            f"Signing key address ({account.address}) does not match operator ({owner_address})"
        )

    initcode = build_proxy_initcode(owner_address=owner_address, implementation_address=implementation)
    latest_block = web3.eth.get_block("latest")
    base_fee = int(latest_block.get("baseFeePerGas", web3.eth.gas_price))
    try:
        priority_fee = int(web3.eth.max_priority_fee)
    except Exception:
        priority_fee = int(web3.to_wei(Decimal("0.05"), "gwei"))

    # Tight, safe fee for fast confirmation (cost is only ~86k gas anyway)
    max_fee = int(Decimal(base_fee) * Decimal("1.25")) + priority_fee
    gas_limit = 120_000

    nonce = web3.eth.get_transaction_count(account.address, "pending")
    tx_params = {
        "from": account.address,
        "chainId": CHAIN_ID,
        "nonce": nonce,
        "gas": gas_limit,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "value": 0,
        "data": initcode,
    }

    signed = account.sign_transaction(tx_params)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction).hex()
    print(f"[*] Proxy deployment broadcast: https://etherscan.io/tx/0x{tx_hash.removeprefix('0x')}")

    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=receipt_timeout)
    if receipt.status != 1 or not receipt.contractAddress:
        raise ProxyDeploymentError(f"Proxy deployment failed: {tx_hash}")

    new_proxy = Web3.to_checksum_address(receipt.contractAddress)

    # Validate deployed proxy
    runtime_code = web3.eth.get_code(new_proxy).hex().lower().removeprefix("0x")
    if len(runtime_code) < 90:  # 45 bytes = 90 hex chars
        raise ProxyDeploymentError(f"Proxy has invalid bytecode length: {len(runtime_code)}")

    contract = web3.eth.contract(address=new_proxy, abi=MINIMAL_ABI)
    proxy_owner = contract.functions.owner().call()
    if proxy_owner.lower() != owner_address.lower():
        raise ProxyDeploymentError(f"Proxy owner check failed: expected {owner_address}, got {proxy_owner}")

    # Update .env
    update_env_file(new_proxy)
    log_proxy_history(new_proxy, tx_hash, receipt.gasUsed, receipt.blockNumber)
    print(f"[+] Proxy deployed and active: {new_proxy} (Gas used: {receipt.gasUsed})")
    return new_proxy, tx_hash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Broadcast deployment on-chain")
    parser.add_argument("--simulate", action="store_true", help="Simulate deployment and estimate gas")
    args = parser.parse_args()

    web3 = get_web3_connection()
    initcode = build_proxy_initcode()
    print(f"[*] Target Implementation: {MASTER_IMPLEMENTATION}")
    print(f"[*] Owner / Operator:      {OPERATOR_ADDRESS}")
    print(f"[*] Initcode size:          {len(initcode)} bytes")

    if not args.send:
        try:
            gas_est = web3.eth.estimate_gas({"from": OPERATOR_ADDRESS, "data": initcode})
            base_fee_gwei = web3.from_wei(web3.eth.get_block("latest").get("baseFeePerGas", 0), "gwei")
            cost_eth = web3.from_wei(gas_est * web3.eth.get_block("latest").get("baseFeePerGas", 0), "ether")
            print(f"[+] Simulation passed!")
            print(f"    Estimated Gas: {gas_est} units")
            print(f"    Current Base Fee: {base_fee_gwei:.4f} Gwei")
            print(f"    Estimated Cost: ~{cost_eth:.8f} ETH")
            print("\nRun with --send to broadcast.")
        except Exception as exc:
            print(f"[-] Simulation failed: {exc}", file=sys.stderr)
            return 1
        return 0

    proxy_addr, tx_hash = deploy_minimal_proxy()
    print(f"[SUCCESS] New Proxy: {proxy_addr}")
    print(f"Updated .env with ETH_ARB_STABLECOIN_EXECUTOR={proxy_addr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
