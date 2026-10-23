#!/usr/bin/env python3
import base64
import sys
from pathlib import Path
import winrm

def copy_to_vps(local_path: str, remote_path: str):
    p = Path(local_path).resolve()
    with open(p, "rb") as f:
        raw_data = f.read()

    b64 = base64.b64encode(raw_data).decode("ascii")
    print(f"Transferring {p.name} ({len(raw_data)} bytes, b64 {len(b64)}) -> {remote_path}...")

    s = winrm.Session("http://103.77.208.166:5985/wsman", auth=("administrator", "etvQ#5xU?dZN5JU"), transport="ntlm")
    temp_remote_b64 = remote_path + ".b64"

    # Reset temp b64 file
    s.run_cmd(f'powershell -Command "New-Item -Path \'{temp_remote_b64}\' -ItemType File -Force"')

    chunk_size = 4000
    total_chunks = (len(b64) + chunk_size - 1) // chunk_size
    for i in range(0, len(b64), chunk_size):
        chunk = b64[i:i+chunk_size]
        cmd = f'powershell -Command "Add-Content -NoNewline -Path \'{temp_remote_b64}\' -Value \'{chunk}\'"'
        r = s.run_cmd(cmd)
        if r.status_code != 0:
            print(f"Chunk error: {r.std_err.decode()}")
            sys.exit(1)
        idx = i // chunk_size + 1
        print(f"Uploaded chunk {idx}/{total_chunks}...")

    decode_script = f"""
$b64 = Get-Content '{temp_remote_b64}' -Raw
$bytes = [System.Convert]::FromBase64String($b64.Trim())
[System.IO.File]::WriteAllBytes('{remote_path}', $bytes)
Remove-Item '{temp_remote_b64}'
Write-Host "SUCCESS: Written {remote_path} ($($bytes.Length) bytes) on Windows VPS"
"""
    res = s.run_ps(decode_script)
    print("Decode output:", res.std_out.decode())
    if res.std_err:
        print("Decode err:", res.std_err.decode())

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 copy_file_to_vps.py <local_path> <remote_path>")
        sys.exit(1)
    copy_to_vps(sys.argv[1], sys.argv[2])
