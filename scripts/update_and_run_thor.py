#!/usr/bin/env python3
import winrm
import base64

s = winrm.Session("http://103.77.208.166:5985/wsman", auth=("administrator", "etvQ#5xU?dZN5JU"), transport="ntlm")

thor_cmd = """@echo off
title THORChain USDP Resting Sniper (100k USDC)
color 0A
cd /d "C:\\Users\\Administrator\\arbbotstable"
echo =======================================================
echo   THORChain USDP Resting Sniper - LIVE (100k USDC)
echo =======================================================
"C:\\Users\\Administrator\\arbbotstable\\venv\\Scripts\\python.exe" thor_usdp_sniper.py --resting --interval 1 --quantity 14400 --amount 100000 --affiliate symbiosis --affiliate-bps 1 --floor-mode 2bps --live --confirm-live EXECUTE_THOR_SNIPER
pause
"""

b64 = base64.b64encode(thor_cmd.encode("utf-8")).decode("ascii")

ps = f"""
$bytes = [System.Convert]::FromBase64String('{b64}')
[System.IO.File]::WriteAllBytes('C:\\Users\\Administrator\\Desktop\\1_START_THOR_SNIPER.cmd', $bytes)
Write-Host "Updated 1_START_THOR_SNIPER.cmd"
"""

r = s.run_ps(ps)
print(r.std_out.decode("utf-8", errors="replace"))

# Now let's create a script in Session 1 to type into the open cmd prompt, or launch it!
# In PowerShell:
# We find process ID 12912 or any cmd without a child python process, bring it to front, and send keys!
send_keys_ps = """
$wscript = New-Object -ComObject Wscript.Shell
# Try to activate the cmd window
$activated = $wscript.AppActivate(12912)
if (-not $activated) {
    $activated = $wscript.AppActivate("Command Prompt")
}
if (-not $activated) {
    $activated = $wscript.AppActivate("cmd.exe")
}

if ($activated) {
    Start-Sleep -Milliseconds 500
    $wscript.SendKeys("cd /d C:\\Users\\Administrator\\arbbotstable{ENTER}")
    Start-Sleep -Milliseconds 300
    $wscript.SendKeys("venv\\Scripts\\python.exe thor_usdp_sniper.py --resting --interval 1 --quantity 14400 --amount 100000 --affiliate symbiosis --affiliate-bps 1 --floor-mode 2bps --live --confirm-live EXECUTE_THOR_SNIPER{ENTER}")
    Write-Host "Sent command to active cmd prompt!"
} else {
    Write-Host "Could not activate window 12912, starting 1_START_THOR_SNIPER.cmd directly"
    Start-Process "C:\\Users\\Administrator\\Desktop\\1_START_THOR_SNIPER.cmd"
}
"""

b64_sk = base64.b64encode(send_keys_ps.encode("utf-8")).decode("ascii")
ps2 = f"""
$bytes = [System.Convert]::FromBase64String('{b64_sk}')
[System.IO.File]::WriteAllBytes('C:\\Users\\Administrator\\Desktop\\send_cmd.ps1', $bytes)

# Run via scheduled task in interactive session (Session 1)
schtasks /Create /TN "RunThorCmd" /TR "powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\Administrator\\Desktop\\send_cmd.ps1" /SC ONCE /ST 00:00 /IT /RU administrator /F
schtasks /Run /TN "RunThorCmd"
"""

r2 = s.run_ps(ps2)
print(r2.std_out.decode("utf-8", errors="replace"))
if r2.std_err:
    print("ERR:", r2.std_err.decode("utf-8", errors="replace"))
