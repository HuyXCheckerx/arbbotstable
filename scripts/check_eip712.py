from eth_account import Account
from web3 import Web3

# Maintainer address on Polygon:
maintainer = "0x6228AB40E809D3077667630b622A4A44A2DD8919"
print("Target Maintainer:", maintainer)

# EIP712 Domain on Polygon
domain = {
    "name": "CrossChainHTLC",
    "version": "1",
    "chainId": 137,
    "verifyingContract": "0xC6d27843426F31ef378D0C2101384519811933cf",
}

# Let's inspect the fields hashed at 0x2360 onwards in bytecode:
# Looking at the offsets at 0x2360:
# 0x20: secretHash
# 0x40: amount
# 0x60: token
# 0x80: user
# 0xa0: chainId
# 0xc0: nonce
# 0xe0: sessionAddress
# 0x100: maintainerDeadline
# All 8 fields packed into memory and hashed with 0x4573195925874c158d4a55d7440848c3ca9da1e559362400086f99c86b3effe0!
print("Typehash: 0x4573195925874c158d4a55d7440848c3ca9da1e559362400086f99c86b3effe0")
