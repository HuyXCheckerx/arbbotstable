from web3 import Web3
from pyevmasm import disassemble_all

w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
code = w3.eth.get_code('0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE')

# Disassemble from byte 8800 to 9300 (hex 17600 to 18600)
dis = list(disassemble_all(code))
for ins in dis:
    if 8900 <= ins.pc <= 9250:
        print(f"{hex(ins.pc)}: {ins.name} {hex(ins.operand) if ins.operand is not None else ''}")
