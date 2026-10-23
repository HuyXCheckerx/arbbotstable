from web3 import Web3
import re

w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
code = w3.eth.get_code('0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE')

# Look for ASCII strings in the bytecode
ascii_strings = re.findall(b'[a-zA-Z0-9_,() ]{8,}', code)
for s in set(ascii_strings):
    if b'(' in s and b')' in s:
        print("Type/Function candidate:", s.decode('latin1', errors='ignore'))
