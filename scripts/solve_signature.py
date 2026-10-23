import itertools
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
import coincurve

# Target maintainer
TARGET_MAINTAINER = "0x6228AB40E809D3077667630b622A4A44A2DD8919".lower()

# Values from the order
user_addr = Web3.to_checksum_address("0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C")
token_addr = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
proxy_addr = Web3.to_checksum_address("0xC6d27843426F31ef378D0C2101384519811933cf")
impl_addr = Web3.to_checksum_address("0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE")
pool_addr = Web3.to_checksum_address("0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da")

secret_hash_hex = "310536fff272bed7ef5342a1bc3feba92a402d4c864097bfd0815f039856ea18"
secret_hash = bytes.fromhex(secret_hash_hex)
sig_hex = "2ee0d4a3b45470417d406ece80261eb938560e7a0d5140aec22743a60d3ee24e6d631bc57eea114eab26472f0629d1a5f1d948b82c4ffd691b719947f87587eb1c"
sig = bytes.fromhex(sig_hex)

deadline = 1792774758
nonce = 1
exec_fee_wei = 1238762950991034595
amount_raw = 1000 * 10**6

print(f"Sig length: {len(sig)} bytes, v={sig[-1]}")

# Let's test what public key / session address derivation was used
# In our test:
# priv was random, pub_hex was passed to API
# But wait! We need to know what session_addr the maintainer expected!
