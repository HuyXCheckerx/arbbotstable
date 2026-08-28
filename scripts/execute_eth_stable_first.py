#!/usr/bin/env python3
"""Execute live atomic PYUSD -> Stable.com (USDG) -> Matcha (PYUSD) arbitrage on Ethereum mainnet."""

import os
import sys
import json
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import requests
from dotenv import load_dotenv
from web3 import Web3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=True)

PYUSD = "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8"
USDG = "0xe343167631d89B6Ffc58B88d6b7fB0228795491D"
EXECUTOR_ADDRESS = os.getenv("ETH_ARB_STABLECOIN_EXECUTOR", "0xB00f38246ea6870c2e3ed6DBa9d542a9a3fb6920")

def get_compiled_abi():
    source = PROJECT_ROOT / "contracts" / "MorphoMatchaStableArbUsdc.sol"
    npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
    with TemporaryDirectory() as tmpdir:
        cmd = [npx_cmd, "--yes", "solc@0.8.30", "--abi", str(source), "-o", tmpdir]
        subprocess.run(cmd, cwd=PROJECT_ROOT, check=True, capture_output=True)
        abi_path = Path(tmpdir) / "contracts_MorphoMatchaStableArbUsdc_sol_MorphoMatchaStableArbUsdc.abi"
        return json.loads(abi_path.read_text(encoding="utf-8"))

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    rpc_url = os.environ["ETH_RPC_URL"]
    private_key = os.environ["ETH_OPERATOR_PRIVATE_KEY"]
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    operator = w3.eth.account.from_key(private_key).address

    print("=" * 70)
    print("[*] EXECUTING ATOMIC ETHEREUM ARBITRAGE (STABLE-FIRST)")
    print(f"    Operator: {operator} | Executor: {EXECUTOR_ADDRESS}")
    print("=" * 70)

    # 1. Check Stable.com capacity & quote for PYUSD -> USDG
    loan_amount = 344_000
    raw_loan = loan_amount * 10**6

    stable_res = requests.post(
        "https://api-defi.stable.com/swap/status",
        json={
            "chainFrom": "101",
            "assetFrom": "PYUSD",
            "chainTo": "101",
            "assetTo": "USDG",
            "amountFrom": str(loan_amount),
            "addressFrom": EXECUTOR_ADDRESS,
            "addressTo": EXECUTOR_ADDRESS,
        },
        headers={"accept": "application/json", "content-type": "application/json", "origin": "https://stable.com", "referer": "https://stable.com/"}
    ).json()

    asset_data = stable_res.get("asset", {})
    pool_balance = float(asset_data.get("balance", 0))
    print(f"[*] Stable.com Pool Balance: {pool_balance:,.2f} USDG")
    if pool_balance < loan_amount:
        loan_amount = int(pool_balance - 10)
        raw_loan = loan_amount * 10**6
        print(f"[*] Adjusted loan amount to pool capacity: {loan_amount:,.2f} PYUSD")

    # Create signed order from Stable.com
    create_res = requests.post(
        "https://api-defi.stable.com/swap/create/singleChain",
        json={
            "chainFrom": "101",
            "assetFrom": "PYUSD",
            "chainTo": "101",
            "assetTo": "USDG",
            "amountFrom": str(loan_amount),
            "amountTo": str(loan_amount),
            "addressFrom": EXECUTOR_ADDRESS,
            "addressTo": EXECUTOR_ADDRESS,
            "gasLess": False,
            "device": "eth-arb-runner"
        },
        headers={"accept": "application/json", "content-type": "application/json", "origin": "https://stable.com", "referer": "https://stable.com/"}
    ).json()

    order = create_res if "maintainerSignature" in create_res else create_res.get("data", {})
    if not order.get("maintainerSignature"):
        print("ERROR: Stable.com failed to sign order:", create_res)
        return

    stable_tuple = (
        int(raw_loan),
        int(order["deadline"]),
        int(order["nonce"]),
        bytes.fromhex(order["maintainerSignature"].removeprefix("0x")),
        int(order["executionFeeNative"]),
    )

    # 2. Get Matcha DEX quote for USDG -> PYUSD
    from src.engines.eth_flash_arb_pyusd_usdc import HttpJsonClient, MatchaClient, MATCHA_AGGREGATORS, parse_matcha_quote

    http_client = HttpJsonClient(15.0, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    matcha_client = MatchaClient(http_client, "https://meta.matcha.xyz")

    matcha_quotes = matcha_client.quotes(
        EXECUTOR_ADDRESS,
        raw_loan,
        0,
        MATCHA_AGGREGATORS,
        sell_token_address=USDG,
        buy_token_address=PYUSD
    )

    best_quote = None
    best_buy_amount = 0
    for agg, resp in matcha_quotes:
        try:
            q = parse_matcha_quote(agg, resp, raw_loan)
            if q.buy_amount > best_buy_amount:
                best_buy_amount = q.buy_amount
                best_quote = q
        except Exception:
            pass

    if not best_quote or best_buy_amount <= raw_loan:
        print(f"[-] No profitable DEX quote found. Best was {best_buy_amount/1e6:,.4f} PYUSD")
        return

    gross_profit = (best_buy_amount - raw_loan) / 1e6
    print(f"[+] Best DEX Aggregator: [{best_quote.aggregator}] -> {best_buy_amount/1e6:,.4f} PYUSD | Gross Profit: +{gross_profit:,.4f} PYUSD")

    matcha_tuple = (
        w3.to_checksum_address(best_quote.target),
        w3.to_checksum_address(best_quote.allowance_target),
        int(best_quote.sell_amount),
        int(best_quote.value),
        bytes.fromhex(best_quote.data.removeprefix("0x")),
    )

    # 3. Simulate and broadcast transaction
    abi = get_compiled_abi()
    contract = w3.eth.contract(address=w3.to_checksum_address(EXECUTOR_ADDRESS), abi=abi)

    # SwapOrder: 0 = DexFirst, 1 = StableFirst
    # FlashProvider: 0 = Morpho
    min_profit = 1 # 1 micro-token minimum guaranteed profit floor

    call = contract.functions.executeArbitrageWithTokensAndProviderAndOrder(
        raw_loan,
        w3.to_checksum_address(PYUSD),
        w3.to_checksum_address(USDG),
        0, # Morpho
        1, # StableFirst
        matcha_tuple,
        stable_tuple,
        min_profit,
    )

    tx_params = {
        "from": operator,
        "value": int(order["executionFeeNative"]) + int(best_quote.value),
        "chainId": 1,
    }

    print("[*] Simulating execution on Ethereum mainnet...")
    try:
        est_gas = call.estimate_gas(tx_params)
        print(f"[+] Simulation passed! Estimated Gas: {est_gas:,} units")
    except Exception as e:
        print(f"[-] Simulation reverted: {e}")
        return

    gas_limit = int(est_gas * 1.25)
    latest_block = w3.eth.get_block("latest")
    base_fee = latest_block["baseFeePerGas"]
    priority_fee = w3.eth.max_priority_fee
    max_fee = (base_fee * 2) + priority_fee

    nonce = w3.eth.get_transaction_count(operator)
    tx_params.update({
        "nonce": nonce,
        "gas": gas_limit,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
    })

    tx = call.build_transaction(tx_params)
    signed = w3.eth.account.sign_transaction(tx, private_key)

    print(f"[*] Broadcasting transaction ({tx_params['value']} wei value)...")
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = tx_hash.hex()
    print(f"[+] BROADCASTED! Tx Hash: 0x{tx_hex}")
    print(f"    Etherscan: https://etherscan.io/tx/0x{tx_hex}")

    print("[*] Waiting for on-chain confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        gas_used = receipt.gasUsed
        actual_fee_eth = (gas_used * receipt.effectiveGasPrice) / 1e18
        print(f"[+] CONFIRMED IN BLOCK {receipt.blockNumber}!")
        print(f"    Gas Used: {gas_used:,} | Tx Fee: {actual_fee_eth:.6f} ETH")
        print(f"    Captured Gross Profit: +{gross_profit:,.4f} PYUSD")
    else:
        print("[-] Transaction reverted on-chain!")

if __name__ == "__main__":
    main()
