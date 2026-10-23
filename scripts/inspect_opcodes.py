from web3 import Web3
import re

w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
code = w3.eth.get_code('0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE').hex()

for err in ["f525e320", "8baa579f"]:
    indices = [m.start() for m in re.finditer(err, code)]
    print(f"Error {err} found at hex offsets:", indices)
    for idx in indices:
        start = max(0, idx - 200)
        end = min(len(code), idx + 100)
        print(f"--- Chunk around {err} at {idx} ---")
        print(code[start:end])
