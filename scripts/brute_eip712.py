import eth_utils

target = "4573195925874c158d4a55d7440848c3ca9da1e559362400086f99c86b3effe0"

# Struct name could be: HtlcLockSource, LockSource, Lock, SourceLock
struct_names = ["HtlcLockSource", "LockSource", "Lock", "SourceLock", "Params", "LockParams"]
# types for chainId, nonce, deadline
for sname in struct_names:
    for ctype in ["uint64", "uint256", "uint32"]:
        for ntype in ["uint256", "uint64"]:
            for dname in ["maintainerDeadline", "deadline"]:
                for dtype in ["uint64", "uint256", "uint32"]:
                    sig = f"{sname}(bytes32 secretHash,uint256 amount,address token,address user,{ctype} chainId,{ntype} nonce,address sessionAddress,{dtype} {dname})"
                    if eth_utils.keccak(text=sig).hex() == target:
                        print("FOUND EXACT TYPESTRING:", sig)
                        break
