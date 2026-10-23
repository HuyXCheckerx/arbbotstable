from web3 import Web3

w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
code = bytes.fromhex(w3.eth.get_code('0x5f8e163045FC31A63E6261089F6dBc08D5ceF9EE').hex())

def disasm(code_bytes, start, length):
    opcodes = {
        0x00: "STOP", 0x10: "LT", 0x11: "GT", 0x14: "EQ", 0x15: "ISZERO",
        0x20: "KECCAK256", 0x33: "CALLER", 0x34: "CALLVALUE", 0x35: "CALLDATALOAD",
        0x50: "POP", 0x51: "MLOAD", 0x52: "MSTORE", 0x54: "SLOAD", 0x55: "SSTORE",
        0x56: "JUMP", 0x57: "JUMPI", 0x5b: "JUMPDEST", 0xfd: "REVERT", 0xf3: "RETURN"
    }
    for i in range(1, 33):
        opcodes[0x5f + i] = f"PUSH{i}"
        opcodes[0x7f + i] = f"DUP{i}" if i <= 16 else f"SWAP{i-16}"
    
    pc = start
    while pc < start + length and pc < len(code_bytes):
        op = code_bytes[pc]
        name = opcodes.get(op, f"0x{op:02x}")
        if 0x60 <= op <= 0x7f:
            push_len = op - 0x5f
            val = code_bytes[pc+1:pc+1+push_len].hex()
            print(f"  {hex(pc)}: {name} 0x{val}")
            pc += 1 + push_len
        else:
            print(f"  {hex(pc)}: {name}")
            pc += 1

print("\n--- Disassembly from 0x22b3 ---")
disasm(code, 0x22b3, 250)
