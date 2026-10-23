#!/usr/bin/env python3
import time
import winrm

s = winrm.Session('http://103.77.208.166:5985/wsman', auth=('administrator', 'etvQ#5xU?dZN5JU'), transport='ntlm')

stop_and_clean_ps = r'''
# 1. Kill old python processes running local_quoter_service or sniper
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | ForEach-Object {
    if ($_.CommandLine -match "local_quoter_service|sniper\.py") {
        Write-Host "Killing Process $($_.ProcessId): $($_.CommandLine)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# 2. Kill orphan CMD windows holding quoter or sniper
Get-CimInstance Win32_Process -Filter "Name like 'cmd%'" | ForEach-Object {
    if ($_.CommandLine -match "START_LOCAL_QUOTER|START_CROSSCHAIN_SNIPER|start_sniper\.cmd") {
        Write-Host "Killing CMD Process $($_.ProcessId): $($_.CommandLine)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# 3. Clean up stale pid and lock files
cd "C:\Users\Administrator\arbbotstable"
Remove-Item -Force -ErrorAction SilentlyContinue .matcha_bridge.pid, .matcha_bridge.lock, logs\crosschain-sniper.pid, logs\.crosschain-sniper.lock

Write-Host "Cleanup completed."
'''

print("Stopping old services on Windows VPS...")
r1 = s.run_ps(stop_and_clean_ps)
print(r1.std_out.decode('utf-8', errors='replace'))
if r1.std_err:
    print("[STDERR]", r1.std_err.decode('utf-8', errors='replace'))

time.sleep(2)

# Create a small launcher batch script that starts both quoter and sniper in separate visible windows
launcher_cmd = r'''@echo off
start "Local Quoter Service" cmd /k "C:\Users\Administrator\Desktop\3_START_LOCAL_QUOTER.cmd"
timeout /t 2 >nul
start "Crosschain Sniper" cmd /k "C:\Users\Administrator\Desktop\2_START_CROSSCHAIN_SNIPER.cmd"
'''

setup_launcher_ps = f'''
$code = @'
{launcher_cmd}
'@
Set-Content -Path "C:\\Users\\Administrator\\arbbotstable\\start_all_services.cmd" -Value $code

# Create scheduled task to run in interactive Session 1
schtasks /Create /TN "StartArbServices" /TR "cmd.exe /c C:\\Users\\Administrator\\arbbotstable\\start_all_services.cmd" /SC ONCE /ST 00:00 /IT /RU administrator /F
schtasks /Run /TN "StartArbServices"
Write-Host "Scheduled task StartArbServices launched."
'''

print("Launching updated quoter and sniper in interactive Session 1...")
r2 = s.run_ps(setup_launcher_ps)
print(r2.std_out.decode('utf-8', errors='replace'))
if r2.std_err:
    print("[STDERR]", r2.std_err.decode('utf-8', errors='replace'))

print("Waiting 5 seconds for services to initialize...")
time.sleep(5)

# Verify health and live quote
verify_ps = r'''
Write-Host "=== VERIFYING EVM QUOTER (PORT 8795) ==="
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8795/health" -Method Get -TimeoutSec 3
    Write-Host "Health: " ($health | ConvertTo-Json -Compress)
} catch {
    Write-Host "Health check failed: " $_.Exception.Message
}

try {
    $quote = Invoke-RestMethod -Uri "http://127.0.0.1:8795/quote?input=PYUSD&output=USDG&amount=100000" -Method Get -TimeoutSec 5
    Write-Host "Live Quote PYUSD -> USDG 100k: " ($quote | ConvertTo-Json -Compress)
} catch {
    Write-Host "Quote check failed: " $_.Exception.Message
}

Write-Host "`n=== ACTIVE PYTHON PROCESSES ==="
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Select-Object ProcessId, CommandLine | Format-Table -AutoSize
'''

r3 = s.run_ps(verify_ps)
print(r3.std_out.decode('utf-8', errors='replace'))
if r3.std_err:
    print("[STDERR]", r3.std_err.decode('utf-8', errors='replace'))
