from web3 import Web3

w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
code = w3.eth.get_code('0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE')

# Scan around hex offset 17000 to 18500 for PUSH32 (opcode 0x7f)
hex_code = code.hex()
start = 17000
end = 18500
chunk = bytes.fromhex(hex_code[start:end])

i = 0
while i < len(chunk):
    if chunk[i] == 0x7f: # PUSH32
        val = chunk[i+1:i+33].hex()
        print(f"PUSH32 at offset {start + i*2}: 0x{val}")
        i += 33
    else:
        i += 1
