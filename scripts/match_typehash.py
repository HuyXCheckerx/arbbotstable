import eth_utils

target = "4573195925874c158d4a55d7440848c3ca9da1e559362400086f99c86b3effe0"

candidates = [
    # Flattened or nested
    "HtlcLockSource(bytes32 secretHash,uint256 amount,address token,address user,uint64 chainId,uint256 nonce,address sessionAddress,uint64 maintainerDeadline)",
    "HtlcLockSource(Base base,uint256 nonce,address sessionAddress,uint64 maintainerDeadline)Base(bytes32 secretHash,uint256 amount,address token,address user,uint64 chainId)",
    "LockSource(bytes32 secretHash,uint256 amount,address token,address user,uint64 chainId,uint256 nonce,address sessionAddress,uint64 maintainerDeadline)",
    "LockSource(Base base,uint256 nonce,address sessionAddress,uint64 maintainerDeadline)Base(bytes32 secretHash,uint256 amount,address token,address user,uint64 chainId)",
    "Base(bytes32 secretHash,uint256 amount,address token,address user,uint64 chainId)",
    "HtlcLockSource(bytes32 secretHash,uint256 amount,address token,address user,uint256 chainId,uint256 nonce,address sessionAddress,uint256 maintainerDeadline)",
    "HtlcLockSource(Base base,uint256 nonce,address sessionAddress,uint256 maintainerDeadline)Base(bytes32 secretHash,uint256 amount,address token,address user,uint256 chainId)",
    "HtlcLockSource(bytes32 secretHash,uint256 amount,address token,address user,uint64 chainId,uint256 nonce,address sessionAddress,uint64 deadline)",
]

found = False
for c in candidates:
    h = eth_utils.keccak(text=c).hex()
    if h == target:
        print("MATCH FOUND! ->", c)
        found = True
        break

if not found:
    print("No direct match, let's generate combinations")
    for name in ["HtlcLockSource", "LockSource", "Lock", "Swap", "HtlcLock"]:
        for ctype in ["uint64", "uint256"]:
            for dname in ["maintainerDeadline", "deadline"]:
                sig = f"{name}(bytes32 secretHash,uint256 amount,address token,address user,{ctype} chainId,uint256 nonce,address sessionAddress,{ctype} {dname})"
                if eth_utils.keccak(text=sig).hex() == target:
                    print("MATCH FOUND! ->", sig)
                    found = True
                    break
