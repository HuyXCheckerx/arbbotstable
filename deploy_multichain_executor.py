#!/usr/bin/env python3
"""Compile, estimate, and optionally deploy the Polygon/BSC arb executor."""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

try:
    from dotenv import load_dotenv
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError as exc:
    raise SystemExit(
        "Install dependencies: python3 -m pip install -r requirements-eth.txt"
    ) from exc

from multichain_flash_arb import (
    CHAINS,
    ERC20_ABI,
    EXECUTOR_ABI,
    STABLE_POOL,
    ChainConfig,
)


PROJECT_DIR = Path(__file__).resolve().parent
SOURCE = PROJECT_DIR / "contracts" / "MultichainMatchaStableArb.sol"
SOLC_VERSION = "0.8.30"


class DeploymentError(RuntimeError):
    pass


def compile_contract() -> tuple[list[dict], str]:
    with TemporaryDirectory(prefix="multichain-executor-solc-") as output_dir:
        command = [
            "npx",
            "--yes",
            f"solc@{SOLC_VERSION}",
            "--optimize",
            "--optimize-runs",
            "200",
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
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise DeploymentError(f"Solidity compilation failed: {detail}")

        prefix = (
            "contracts_MultichainMatchaStableArb_sol_"
            "MultichainMatchaStableArb"
        )
        abi_path = Path(output_dir) / f"{prefix}.abi"
        bytecode_path = Path(output_dir) / f"{prefix}.bin"
        if not abi_path.exists() or not bytecode_path.exists():
            raise DeploymentError("compiler did not produce the expected artifacts")
        abi = json.loads(abi_path.read_text(encoding="utf-8"))
        bytecode = bytecode_path.read_text(encoding="utf-8").strip()
        if not bytecode:
            raise DeploymentError("compiled executor bytecode is empty")
        return abi, "0x" + bytecode


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument(
        "--chain",
        choices=tuple(CHAINS),
        default=os.getenv("EVM_ARB_CHAIN", "polygon").lower(),
    )
    selected, _ = bootstrap.parse_known_args(argv)
    config = CHAINS[selected.chain]
    parser = argparse.ArgumentParser(description=__doc__, parents=[bootstrap])
    parser.add_argument("--send", action="store_true", help="broadcast deployment")
    parser.add_argument(
        "--confirm-mainnet",
        help=(
            "required confirmation when sending: "
            f"DEPLOY_{config.env_prefix}_EXECUTOR"
        ),
    )
    configured_max_fee = (
        os.getenv(f"{config.env_prefix}_DEPLOY_MAX_FEE_GWEI", "").strip()
        or os.getenv(f"{config.env_prefix}_ARB_MAX_FEE_GWEI", "").strip()
        or None
    )
    parser.add_argument(
        "--max-fee-gwei",
        type=Decimal,
        default=configured_max_fee,
    )
    parser.add_argument(
        "--max-total-fee-native",
        type=Decimal,
        default=os.getenv(
            f"{config.env_prefix}_DEPLOY_MAX_TOTAL_FEE_NATIVE", "0.95"
        ),
        help=(
            "cap gasLimit * maxFeePerGas; defaults to 0.95 native token to "
            "stay below common RPC transaction-fee caps"
        ),
    )
    parser.add_argument("--gas-multiplier", type=Decimal, default=Decimal("1.15"))
    parser.add_argument("--receipt-timeout", type=int, default=180)
    return parser.parse_args(argv)


def validate_system_contracts(web3: Web3, config: ChainConfig) -> None:
    addresses = {
        config.flash_provider_name: config.flash_lender,
        "USDC": config.usdc,
        "USDT": config.usdt,
        "Stable.com pool": STABLE_POOL,
    }
    for label, address in addresses.items():
        checksum = Web3.to_checksum_address(address)
        if len(web3.eth.get_code(checksum)) == 0:
            raise DeploymentError(f"configured {label} address has no bytecode")
    for symbol, address in (("USDC", config.usdc), ("USDT", config.usdt)):
        token = web3.eth.contract(
            address=Web3.to_checksum_address(address), abi=ERC20_ABI
        )
        try:
            decimals = int(token.functions.decimals().call())
        except Exception as exc:
            raise DeploymentError(f"cannot read configured {symbol} decimals") from exc
        if decimals != config.decimals:
            raise DeploymentError(
                f"configured {symbol} has {decimals} decimals; "
                f"expected {config.decimals}"
            )


def validate_deployment(
    web3: Web3,
    address: str,
    owner: str,
    config: ChainConfig,
) -> None:
    expected = {
        "owner": owner,
        "expectedChainId": config.chain_id,
        "providerKind": config.provider_kind,
        "flashLender": config.flash_lender,
        "loanToken": config.usdc,
        "intermediateToken": config.usdt,
        "stablePool": STABLE_POOL,
    }
    last_error: Exception | None = None
    # Shared RPC gateways may send the receipt and the following eth_call to
    # backends at slightly different heads. Retry verification without ever
    # resubmitting the already-mined deployment transaction.
    for attempt in range(8):
        try:
            if len(web3.eth.get_code(address)) == 0:
                raise DeploymentError("deployed executor bytecode is not indexed yet")
            executor = web3.eth.contract(address=address, abi=EXECUTOR_ABI)
            for getter, wanted in expected.items():
                actual = getattr(executor.functions, getter)().call()
                if isinstance(wanted, str):
                    valid = str(actual).lower() == wanted.lower()
                else:
                    valid = int(actual) == wanted
                if not valid:
                    raise DeploymentError(
                        f"deployed executor {getter} is {actual}; expected {wanted}"
                    )
            return
        except Exception as exc:
            last_error = exc
            if attempt < 7:
                time.sleep(2)
    raise DeploymentError(
        "deployment mined, but the RPC could not verify its getters after retries"
    ) from last_error


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_DIR / ".env", override=True)
    args = parse_args(argv)
    config = CHAINS[args.chain]
    prefix = config.env_prefix
    confirmation = f"DEPLOY_{prefix}_EXECUTOR"
    rpc_url = os.getenv(f"{prefix}_RPC_URL", "").strip()
    private_key = os.getenv(f"{prefix}_OPERATOR_PRIVATE_KEY", "").strip()
    configured_operator = os.getenv(f"{prefix}_OPERATOR_ADDRESS", "").strip()
    if not rpc_url:
        raise DeploymentError(f"{prefix}_RPC_URL is missing from .env")
    if not private_key and not configured_operator:
        raise DeploymentError(
            f"{prefix}_OPERATOR_ADDRESS or {prefix}_OPERATOR_PRIVATE_KEY is required"
        )
    if args.gas_multiplier < 1:
        raise DeploymentError("--gas-multiplier must be at least 1")
    if args.max_total_fee_native <= 0:
        raise DeploymentError("--max-total-fee-native must be positive")
    if args.send and args.confirm_mainnet != confirmation:
        raise DeploymentError(
            f"--send requires --confirm-mainnet {confirmation}"
        )
    if args.send and not private_key:
        raise DeploymentError(f"{prefix}_OPERATOR_PRIVATE_KEY is required for --send")

    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    try:
        chain_id = web3.eth.chain_id
    except Exception as exc:
        detail = str(exc).replace(rpc_url, "<redacted RPC URL>")
        raise DeploymentError(f"cannot connect to {config.name} RPC: {detail}") from exc
    if chain_id != config.chain_id:
        raise DeploymentError(
            f"RPC chain ID is {chain_id}; {config.name} requires {config.chain_id}"
        )
    validate_system_contracts(web3, config)

    account = None
    if private_key:
        try:
            account = web3.eth.account.from_key(private_key)
        except Exception as exc:
            raise DeploymentError(f"{prefix}_OPERATOR_PRIVATE_KEY is invalid") from exc
    if configured_operator:
        try:
            operator = Web3.to_checksum_address(configured_operator)
        except ValueError as exc:
            raise DeploymentError(f"{prefix}_OPERATOR_ADDRESS is invalid") from exc
    elif account is not None:
        operator = account.address
    else:
        raise DeploymentError("operator configuration is incomplete")
    if account is not None and account.address.lower() != operator.lower():
        raise DeploymentError(
            f"{prefix}_OPERATOR_ADDRESS does not match its private key"
        )

    abi, bytecode = compile_contract()
    factory = web3.eth.contract(abi=abi, bytecode=bytecode)
    constructor = factory.constructor(
        operator,
        config.chain_id,
        config.provider_kind,
        Web3.to_checksum_address(config.flash_lender),
        Web3.to_checksum_address(config.usdc),
        Web3.to_checksum_address(config.usdt),
        Web3.to_checksum_address(STABLE_POOL),
    )
    try:
        estimated_gas = int(constructor.estimate_gas({"from": operator}))
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
    base_fee = latest.get("baseFeePerGas")
    if base_fee is not None:
        proposed_max_fee = (
            int(web3.to_wei(args.max_fee_gwei, "gwei"))
            if args.max_fee_gwei is not None
            else int(base_fee) * 2 + priority_fee
        )
    else:
        proposed_max_fee = (
            int(web3.to_wei(args.max_fee_gwei, "gwei"))
            if args.max_fee_gwei is not None
            else int(web3.eth.gas_price)
        )
    max_total_fee_wei = int(
        (
            args.max_total_fee_native * Decimal(10**18)
        ).to_integral_value(rounding=ROUND_DOWN)
    )
    fee_cap_per_gas = max_total_fee_wei // gas_limit
    max_fee = min(proposed_max_fee, fee_cap_per_gas)
    fee_was_capped = max_fee != proposed_max_fee
    if base_fee is not None:
        minimum_includable_fee = int(base_fee) + priority_fee
        if max_fee < minimum_includable_fee:
            required_native = Decimal(gas_limit * minimum_includable_fee) / Decimal(
                10**18
            )
            raise DeploymentError(
                "the configured maximum total deployment fee is below the "
                f"current base fee requirement ({required_native} "
                f"{config.native_symbol})"
            )
        fee_fields = {
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
        }
    else:
        fee_fields = {"gasPrice": max_fee}

    balance = web3.eth.get_balance(operator)
    maximum_cost = gas_limit * max_fee
    if args.send and balance < maximum_cost:
        raise DeploymentError(
            f"operator has insufficient {config.native_symbol} for deployment"
        )
    transaction = constructor.build_transaction(
        {
            "from": operator,
            "chainId": config.chain_id,
            "nonce": web3.eth.get_transaction_count(operator, "pending"),
            "gas": gas_limit,
            "value": 0,
            **fee_fields,
        }
    )
    summary = {
        "mode": "broadcast" if args.send else "estimate-only",
        "chain": config.name,
        "chainId": config.chain_id,
        "flashProvider": config.flash_provider_name,
        "operator": operator,
        "estimatedGas": estimated_gas,
        "gasLimit": gas_limit,
        "maxFeePerGasGwei": str(web3.from_wei(max_fee, "gwei")),
        "maxFeeCappedByNativeLimit": fee_was_capped,
        f"configuredMaximumTotalFee{config.native_symbol}": str(
            args.max_total_fee_native
        ),
        f"maximumDeploymentCost{config.native_symbol}": str(
            web3.from_wei(maximum_cost, "ether")
        ),
        "compiler": SOLC_VERSION,
        "optimizerRuns": 200,
    }
    print(json.dumps(summary, indent=2))
    if not args.send:
        print("Estimate only: no transaction was signed or broadcast.")
        return 0

    signed = account.sign_transaction(transaction)
    local_hash = signed.hash.hex()
    try:
        transaction_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as exc:
        raise DeploymentError(
            "RPC rejected or did not acknowledge deployment transaction "
            f"{local_hash}: {exc}. Check the chain explorer before retrying."
        ) from exc
    print(f"Deployment transaction: {transaction_hash.hex()}")
    try:
        receipt = web3.eth.wait_for_transaction_receipt(
            transaction_hash, timeout=args.receipt_timeout
        )
    except Exception as exc:
        raise DeploymentError(
            f"deployment {transaction_hash.hex()} was submitted but receipt "
            "confirmation failed; check the explorer before retrying"
        ) from exc
    if receipt.status != 1 or not receipt.contractAddress:
        raise DeploymentError(f"deployment failed: {transaction_hash.hex()}")
    address = Web3.to_checksum_address(receipt.contractAddress)
    validate_deployment(web3, address, operator, config)
    print(
        json.dumps(
            {
                "transactionHash": transaction_hash.hex(),
                "executorAddress": address,
                "setEnv": f"{prefix}_EXECUTOR_ADDRESS={address}",
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
