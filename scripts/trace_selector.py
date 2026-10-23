from web3 import Web3

w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
code = bytes.fromhex(w3.eth.get_code('0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE').hex())

# At 0x0227
print("Code at 0x0227:", code[0x0227:0x0227+80].hex())
