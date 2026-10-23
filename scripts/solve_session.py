import requests
import secrets
import coincurve
import eth_utils
from eth_account import Account
from web3 import Web3

TARGET_MAINTAINER = "0x6228AB40E809D3077667630b622A4A44A2DD8919".lower()
pk = "0x8ac6334c551a5a61b07da1b9c078b56db260d30bf92e1cade6de0d192a1f089f"
user_addr = Account.from_key(pk).address
token_addr = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
htlc_addr = Web3.to_checksum_address("0xC6d27843426F31ef378D0C2101384519811933cf")

# Ephemeral session key
priv = secrets.token_bytes(32)
pub_uncompressed = coincurve.PrivateKey(priv).public_key.format(compressed=False).hex()
pub_compressed = coincurve.PrivateKey(priv).public_key.format(compressed=True).hex()

# Try with pub_uncompressed
payload = {
    "chainFrom": "106",  # Polygon
    "assetFrom": "USDC",
    "chainTo": "102",    # Solana
    "assetTo": "USDC",
    "amountFrom": "1000",
    "addressFrom": user_addr,
    "addressTo": "G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he",
    "gasLess": False,
    "sessionPubkey": pub_uncompressed,
}
headers = {
    "Origin": "https://stable.com",
    "Referer": "https://stable.com/",
    "User-Agent": "Mozilla/5.0",
}
res = requests.post("https://api-defi.stable.com/swap/create/crossChain", json=payload, headers=headers).json()
order = res["data"]
print("Order Response:", order)

secret_hash = bytes.fromhex(order["secretHash"].replace("0x", ""))
maintainer_sig = bytes.fromhex(order["maintainerSignature"].replace("0x", ""))
deadline = int(order["deadline"])
nonce = int(order["nonce"])
fee_native_str = order["executionFeeNative"]
from decimal import Decimal
fee_wei = int(Decimal(fee_native_str) * Decimal(10**18))
amount_raw = 1000 * 10**6

HTLC_LOCK_WITH_FEE_TYPEHASH = bytes.fromhex("4573195925874c158d4a55d7440848c3ca9da1e559362400086f99c86b3effe0")
HTLC_LOCK_TYPEHASH = bytes.fromhex("8615dcfc43c82180bdfbee31c7baaa251edab5f049c399d55a4fa7e9ef2a1965")

# Possible session addresses:
# 1. uncompressed without 04 (standard Ethereum)
addr1 = Web3.to_checksum_address(Web3.keccak(bytes.fromhex(pub_uncompressed)[1:])[-20:].hex())
# 2. uncompressed WITH 04
addr2 = Web3.to_checksum_address(Web3.keccak(bytes.fromhex(pub_uncompressed))[-20:].hex())
# 3. compressed
addr3 = Web3.to_checksum_address(Web3.keccak(bytes.fromhex(pub_compressed))[-20:].hex())
# 4. user address itself
addr4 = user_addr
# 5. address(0)
addr5 = "0x0000000000000000000000000000000000000000"

candidates_session = [addr1, addr2, addr3, addr4, addr5]

# Possible chain IDs in domain separator:
chain_ids = [137, 106, 1]

# Verifying contracts
contracts = [htlc_addr, "0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE", "0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da"]

print(f"Testing combinations...")
found = False
for cid in chain_ids:
    for c_addr in contracts:
        domain_sep = Web3.keccak(
            eth_utils.keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
            + eth_utils.keccak(text="CrossChainHTLC")
            + eth_utils.keccak(text="1")
            + cid.to_bytes(32, "big")
            + bytes.fromhex(c_addr[2:].rjust(64, "0"))
        )
        
        for sess in candidates_session:
            # Try with fee
            for fee_val in [fee_wei, 0, int(float(fee_native_str) * 1e18)]:
                struct_data_fee = (
                    HTLC_LOCK_WITH_FEE_TYPEHASH
                    + secret_hash
                    + amount_raw.to_bytes(32, "big")
                    + bytes.fromhex(token_addr[2:].rjust(64, "0"))
                    + bytes.fromhex(user_addr[2:].rjust(64, "0"))
                    + bytes.fromhex(sess[2:].rjust(64, "0"))
                    + deadline.to_bytes(32, "big")
                    + nonce.to_bytes(32, "big")
                    + fee_val.to_bytes(32, "big")
                )
                struct_hash = Web3.keccak(struct_data_fee)
                digest = Web3.keccak(b"\x19\x01" + domain_sep + struct_hash)
                rec = Account._recover_hash(digest, signature=maintainer_sig)
                if rec.lower() == TARGET_MAINTAINER:
                    print(f"SUCCESS MATCH! chainId={cid}, contract={c_addr}, sess={sess}, fee={fee_val}, type=WithFee")
                    found = True
                    break
            
            # Try without fee
            struct_data_nofee = (
                HTLC_LOCK_TYPEHASH
                + secret_hash
                + amount_raw.to_bytes(32, "big")
                + bytes.fromhex(token_addr[2:].rjust(64, "0"))
                + bytes.fromhex(user_addr[2:].rjust(64, "0"))
                + bytes.fromhex(sess[2:].rjust(64, "0"))
                + deadline.to_bytes(32, "big")
                + nonce.to_bytes(32, "big")
            )
            struct_hash = Web3.keccak(struct_data_nofee)
            digest = Web3.keccak(b"\x19\x01" + domain_sep + struct_hash)
            rec = Account._recover_hash(digest, signature=maintainer_sig)
            if rec.lower() == TARGET_MAINTAINER:
                print(f"SUCCESS MATCH! chainId={cid}, contract={c_addr}, sess={sess}, type=NoFee")
                found = True
                break

if not found:
    print("No match found in basic sweep.")
