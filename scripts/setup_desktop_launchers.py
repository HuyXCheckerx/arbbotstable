#!/usr/bin/env python3
import winrm

thor_cmd = r"""@echo off
title THORChain USDP Resting Sniper (10k USDC)
color 0A
cd /d "C:\Users\Administrator\arbbotstable"
echo =======================================================
echo   THORChain USDP Resting Sniper - LIVE (10k USDC)
echo =======================================================
"C:\Users\Administrator\arbbotstable\venv\Scripts\python.exe" thor_usdp_sniper.py --resting --amount 10000 --affiliate symbiosis --affiliate-bps 1 --floor-mode 2bps --live --confirm-live EXECUTE_THOR_SNIPER
pause
"""

sniper_cmd = r"""@echo off
title Crosschain Multi-DEX Profit Sniper (Local Quoter)
color 0B
cd /d "C:\Users\Administrator\arbbotstable"
call start_sniper.cmd --live --confirm-live EXECUTE_PROFIT_SNIPER
pause
"""

quoter_cmd = r"""@echo off
title Local Quoter Service (EVM 8795 / Solana 8787)
color 0E
cd /d "C:\Users\Administrator\arbbotstable"
echo =======================================================
echo   Local Quoter Engine Service (EVM: 8795, SOL: 8787)
echo =======================================================
"C:\Users\Administrator\arbbotstable\venv\Scripts\python.exe" src\engines\local_quoter_service.py
pause
"""

all_cmd = r"""@echo off
start "THORChain Sniper" "C:\Users\Administrator\Desktop\1_START_THOR_SNIPER.cmd"
timeout /t 1 >nul
start "Local Quoter" "C:\Users\Administrator\Desktop\3_START_LOCAL_QUOTER.cmd"
timeout /t 1 >nul
start "Crosschain Sniper" "C:\Users\Administrator\Desktop\2_START_CROSSCHAIN_SNIPER.cmd"
"""

def main():
    s = winrm.Session("http://103.77.208.166:5985/wsman", auth=("administrator", "etvQ#5xU?dZN5JU"), transport="ntlm")
    
    files = {
        r"C:\Users\Administrator\Desktop\1_START_THOR_SNIPER.cmd": thor_cmd,
        r"C:\Users\Administrator\Desktop\2_START_CROSSCHAIN_SNIPER.cmd": sniper_cmd,
        r"C:\Users\Administrator\Desktop\3_START_LOCAL_QUOTER.cmd": quoter_cmd,
        r"C:\Users\Administrator\Desktop\LAUNCH_ALL_WINDOWS.cmd": all_cmd,
    }
    
    for path, content in files.items():
        # Encode as base64 to avoid escaping issues
        import base64
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        ps = f"""
$bytes = [System.Convert]::FromBase64String('{b64}')
[System.IO.File]::WriteAllBytes('{path}', $bytes)
Write-Host "Created {path}"
"""
        r = s.run_ps(ps)
        print(r.std_out.decode().strip())
        if r.std_err:
            print("[STDERR]", r.std_err.decode())

if __name__ == "__main__":
    main()
