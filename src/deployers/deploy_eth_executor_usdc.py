#!/usr/bin/env python3
"""Compile, estimate, and optionally deploy the MorphoMatchaStableArbUsdc executor from .env."""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

try:
    from dotenv import load_dotenv
    from web3 import Web3
except ImportError as exc:
    raise SystemExit("Install dependencies: python3 -m pip install -r requirements-eth.txt") from exc


PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
SOURCE = PROJECT_DIR / "contracts" / "MorphoMatchaStableArbUsdc.sol"
SOLC_VERSION = "0.8.30"
EXPECTED_CHAIN_ID = 1
CONFIRMATION_TEXT = "DEPLOY_EXECUTOR"


class DeploymentError(RuntimeError):
    pass


def compile_contract() -> tuple[list[dict], str]:
    with TemporaryDirectory(prefix="morpho-executor-usdc-solc-") as output_dir:
        npx_cmd = "npx.cmd" if sys.platform == "win32" else "npx"
        command = [
            npx_cmd,
            "--yes",
            f"solc@{SOLC_VERSION}",
            "--bin",
            "--abi",
            str(SOURCE),
            "-o",
            output_dir,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=PROJECT_DIR,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError(f"Solidity compiler failed: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown compiler error"
            raise DeploymentError(f"Solidity compilation failed: {detail}")

        artifact_prefix = "contracts_MorphoMatchaStableArbUsdc_sol_MorphoMatchaStableArbUsdc"
        abi_path = Path(output_dir) / f"{artifact_prefix}.abi"
        bytecode_path = Path(output_dir) / f"{artifact_prefix}.bin"
        if not abi_path.exists() or not bytecode_path.exists():
            raise DeploymentError("compiler did not produce the expected executor artifacts")
        abi = json.loads(abi_path.read_text(encoding="utf-8"))
        bytecode = bytecode_path.read_text(encoding="utf-8").strip()
        if not bytecode:
            raise DeploymentError("compiled executor bytecode is empty")
        return abi, "0x" + bytecode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="broadcast the deployment")
    parser.add_argument(
        "--confirm-mainnet",
        help=f"must equal {CONFIRMATION_TEXT} when --send is used",
    )
    parser.add_argument("--max-fee-gwei", type=Decimal)
    parser.add_argument("--gas-multiplier", type=Decimal, default=Decimal("1.15"))
    parser.add_argument("--receipt-timeout", type=int, default=180)
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_DIR / ".env", override=True)
    args = parse_args()
    rpc_url = os.getenv("ETH_RPC_URL", "").strip()
    private_key = os.getenv("ETH_OPERATOR_PRIVATE_KEY", "").strip()
    configured_operator = os.getenv("ETH_OPERATOR_ADDRESS", "").strip()
    if not rpc_url:
        raise DeploymentError("ETH_RPC_URL is missing from .env")
    if not private_key:
        raise DeploymentError("ETH_OPERATOR_PRIVATE_KEY is missing from .env")
    if args.gas_multiplier < 1:
        raise DeploymentError("--gas-multiplier must be at least 1")
    if args.send and args.confirm_mainnet != CONFIRMATION_TEXT:
        raise DeploymentError(
            f"--send requires --confirm-mainnet {CONFIRMATION_TEXT}"
        )

    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not web3.is_connected():
        raise DeploymentError("cannot connect using ETH_RPC_URL")
    chain_id = web3.eth.chain_id
    if chain_id != EXPECTED_CHAIN_ID:
        raise DeploymentError(
            f"RPC chain ID is {chain_id}; Ethereum mainnet chain ID 1 is required"
        )

    try:
        account = web3.eth.account.from_key(private_key)
    except Exception as exc:
        raise DeploymentError("ETH_OPERATOR_PRIVATE_KEY is invalid") from exc
    if configured_operator:
        try:
            expected_operator = Web3.to_checksum_address(configured_operator)
        except ValueError as exc:
            raise DeploymentError("ETH_OPERATOR_ADDRESS in .env is invalid") from exc
        if expected_operator != account.address:
            raise DeploymentError(
                "ETH_OPERATOR_ADDRESS does not match ETH_OPERATOR_PRIVATE_KEY"
            )

    abi, bytecode = compile_contract()
    factory = web3.eth.contract(abi=abi, bytecode=bytecode)
    constructor = factory.constructor(account.address)
    try:
        estimated_gas = constructor.estimate_gas({"from": account.address})
    except Exception as exc:
        raise DeploymentError(f"deployment gas estimation failed: {exc}") from exc

    gas_limit = int(
        (Decimal(estimated_gas) * args.gas_multiplier).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    latest = web3.eth.get_block("latest")
    try:
        priority_fee = int(web3.eth.max_priority_fee)
    except Exception:
        priority_fee = int(web3.to_wei(Decimal("0.05"), "gwei"))
    if args.max_fee_gwei is not None:
        max_fee = int(web3.to_wei(args.max_fee_gwei, "gwei"))
    else:
        base_fee = int(latest.get("baseFeePerGas", web3.eth.gas_price))
        max_fee = base_fee * 2 + priority_fee
    if max_fee < priority_fee:
        raise DeploymentError("maximum fee per gas is below the priority fee")

    balance = web3.eth.get_balance(account.address)
    maximum_cost = gas_limit * max_fee
    if balance < maximum_cost:
        raise DeploymentError(
            "operator has insufficient ETH for the conservative deployment cost"
        )
    transaction = constructor.build_transaction(
        {
            "from": account.address,
            "chainId": chain_id,
            "nonce": web3.eth.get_transaction_count(account.address, "pending"),
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "value": 0,
        }
    )

    summary = {
        "mode": "broadcast" if args.send else "estimate-only",
        "chainId": chain_id,
        "operator": account.address,
        "operatorBalanceEth": str(web3.from_wei(balance, "ether")),
        "estimatedGas": estimated_gas,
        "gasLimit": gas_limit,
        "maxFeePerGasGwei": str(web3.from_wei(max_fee, "gwei")),
        "maximumDeploymentCostEth": str(web3.from_wei(maximum_cost, "ether")),
        "compiler": SOLC_VERSION,
    }
    print(json.dumps(summary, indent=2))
    if not args.send:
        print("Estimate only: no transaction was signed or broadcast.")
        return 0

    signed = account.sign_transaction(transaction)
    transaction_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Deployment transaction: {transaction_hash.hex()}")
    receipt = web3.eth.wait_for_transaction_receipt(
        transaction_hash,
        timeout=args.receipt_timeout,
    )
    if receipt.status != 1 or not receipt.contractAddress:
        raise DeploymentError(f"deployment failed in transaction {transaction_hash.hex()}")
    address = Web3.to_checksum_address(receipt.contractAddress)
    if len(web3.eth.get_code(address)) == 0:
        raise DeploymentError("deployment receipt succeeded but contract has no bytecode")
    executor = web3.eth.contract(address=address, abi=abi)
    if Web3.to_checksum_address(executor.functions.owner().call()) != account.address:
        raise DeploymentError("deployed executor owner verification failed")
    supported_tokens = {
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "PYUSD": "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
        "USDG": "0xe343167631d89B6Ffc58B88d6b7fB0228795491D",
    }
    for symbol, token in supported_tokens.items():
        if not executor.functions.supportsLoanToken(
            Web3.to_checksum_address(token)
        ).call():
            raise DeploymentError(f"deployed executor does not support {symbol}")
    for provider_id, provider_name in (
        (0, "Morpho"),
        (1, "Uniswap v4"),
        (2, "Aave v3"),
    ):
        if not executor.functions.supportsFlashProvider(provider_id).call():
            raise DeploymentError(
                f"deployed executor does not support {provider_name} flash funding"
            )

    print(
        json.dumps(
            {
                "transactionHash": transaction_hash.hex(),
                "executorAddress": address,
                "setEnv": f"ETH_ARB_STABLECOIN_EXECUTOR={address}",
                "owner": account.address,
                "blockNumber": receipt.blockNumber,
                "gasUsed": receipt.gasUsed,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
