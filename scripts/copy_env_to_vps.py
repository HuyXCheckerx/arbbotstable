#!/usr/bin/env python3
import base64
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.vps_cmd as vps

env_path = Path(__file__).resolve().parent.parent / ".env"
with open(env_path, "rb") as f:
    raw_data = f.read()

b64 = base64.b64encode(raw_data).decode("ascii")

print(f"Transferring {len(raw_data)} bytes of .env (base64 length {len(b64)})...")

# Clear b64 temp file
vps.run_vps(r'powershell -Command "New-Item -Path C:\Users\Administrator\arbbotstable\.env.b64 -ItemType File -Force"', powershell=False)

chunk_size = 500
for i in range(0, len(b64), chunk_size):
    chunk = b64[i:i+chunk_size]
    cmd = f'powershell -Command "Add-Content -NoNewline -Path C:\\Users\\Administrator\\arbbotstable\\.env.b64 -Value \'{chunk}\'"'
    vps.run_vps(cmd, powershell=False)

# Decode b64
decode_script = r"""
$b64 = Get-Content C:\Users\Administrator\arbbotstable\.env.b64 -Raw
$bytes = [System.Convert]::FromBase64String($b64.Trim())
[System.IO.File]::WriteAllBytes('C:\Users\Administrator\arbbotstable\.env', $bytes)
Remove-Item C:\Users\Administrator\arbbotstable\.env.b64
Write-Host "SUCCESS: Written .env file ($($bytes.Length) bytes) on Windows VPS"
"""
vps.run_vps(decode_script, powershell=True)
