import requests
import secrets
import coincurve
import eth_utils
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

maintainer_expected = "0x6228AB40E809D3077667630b622A4A44A2DD8919"
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
amount_raw = 1000 * 10**6

# EIP712 Domain on Polygon
domain_separator = Web3.keccak(
    eth_utils.keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    + eth_utils.keccak(text="CrossChainHTLC")
    + eth_utils.keccak(text="1")
    + int(137).to_bytes(32, "big")
    + bytes.fromhex(htlc_addr[2:].rjust(64, "0"))
)
print("Domain separator:", domain_separator.hex())

typehash = bytes.fromhex("4573195925874c158d4a55d7440848c3ca9da1e559362400086f99c86b3effe0")

# Test recovering signer with chainId = 137 vs 106
for cid in [137, 106]:
    # Pack the 8 struct fields
    struct_data = (
        typehash
        + secret_hash
        + amount_raw.to_bytes(32, "big")
        + bytes.fromhex(token_addr[2:].rjust(64, "0"))
        + bytes.fromhex(user_addr[2:].rjust(64, "0"))
        + cid.to_bytes(32, "big")
        + nonce.to_bytes(32, "big")
        + bytes.fromhex(session_addr[2:].rjust(64, "0"))
        + deadline.to_bytes(32, "big")
    )
    struct_hash = Web3.keccak(struct_data)
    
    # EIP-712 envelope: \x19\x01 + domainSeparator + structHash
    digest = Web3.keccak(b"\x19\x01" + domain_separator + struct_hash)
    
    recovered = Account._recover_hash(digest, signature=maintainer_sig)
    print(f"For chainId={cid}: Recovered={recovered} | Match Expected? {recovered.lower() == maintainer_expected.lower()}")
