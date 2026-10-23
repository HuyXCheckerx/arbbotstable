import sys
sys.stdout.reconfigure(encoding="utf-8")
import subprocess
import requests
import secrets
import coincurve
from web3 import Web3
from eth_account import Account

pk = "0x8ac6334c551a5a61b07da1b9c078b56db260d30bf92e1cade6de0d192a1f089f"
user_addr = Account.from_key(pk).address
token_addr = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
htlc_addr = Web3.to_checksum_address("0xC6d27843426F31ef378D0C2101384519811933cf")

# Ephemeral session key
priv = secrets.token_bytes(32)
pub_hex = coincurve.PrivateKey(priv).public_key.format(compressed=False).hex()
pub_bytes = bytes.fromhex(pub_hex)[1:]
session_addr = Web3.to_checksum_address(Web3.keccak(pub_bytes)[-20:].hex())

payload = {
    "chainFrom": "106",  # Polygon
    "assetFrom": "USDC",
    "chainTo": "102",    # Solana
    "assetTo": "USDC",
    "amountFrom": "1000",
    "addressFrom": user_addr,
    "addressTo": "G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he",
    "gasLess": False,
    "sessionPubkey": pub_hex,
}
headers = {
    "Origin": "https://stable.com",
    "Referer": "https://stable.com/",
    "User-Agent": "Mozilla/5.0",
}
res = requests.post("https://api-defi.stable.com/swap/create/crossChain", json=payload, headers=headers).json()
order = res["data"]
print("Order created:", order["orderId"])

secret_hash = bytes.fromhex(order["secretHash"].replace("0x", ""))
maintainer_sig = bytes.fromhex(order["maintainerSignature"].replace("0x", ""))
deadline = int(order["deadline"])
nonce = int(order["nonce"])
exec_fee_wei = int(Web3.to_wei(float(order["executionFeeNative"]), "ether"))
amount_raw = 1000 * 10**6

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
    }
]

w3 = Web3()
contract = w3.eth.contract(address=htlc_addr, abi=HTLC_ABI)

for cid in [137, 106]:
    lock_params = (
        (
            secret_hash,
            amount_raw,
            token_addr,
            user_addr,
            cid,
        ),
        nonce,
        session_addr,
        deadline,
        maintainer_sig,
    )
    calldata = contract.encode_abi("lockSource", [lock_params, exec_fee_wei])
    print(f"\n--- CAST CALL TRACE FOR chainId={cid} ---")
    cmd = [
        "cast", "call", htlc_addr,
        calldata,
        "--from", user_addr,
        "--value", str(exec_fee_wei),
        "--rpc-url", "https://polygon-bor-rpc.publicnode.com",
        "--trace",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("STDOUT:")
    print(proc.stdout)
    if proc.stderr:
        print("STDERR:")
        print(proc.stderr)
