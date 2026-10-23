from web3 import Web3

w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
code = w3.eth.get_code('0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE')

# Print instructions byte by byte around 17968 (byte 8984)
# 17968 / 2 = 8984
offset = 8980
print("Bytes:", code[offset:offset+120].hex())
