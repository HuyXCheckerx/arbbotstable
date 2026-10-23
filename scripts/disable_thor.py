import winrm

s = winrm.Session("103.77.208.166", auth=("Administrator", "etvQ#5xU?dZN5JU"), transport="ntlm")

launch_cmd_content = """@echo off
rem THORChain Sniper disabled per user request
start "Local Quoter" "C:\\Users\\Administrator\\Desktop\\3_START_LOCAL_QUOTER.cmd"
timeout /t 1 >nul
start "Crosschain Sniper" "C:\\Users\\Administrator\\Desktop\\2_START_CROSSCHAIN_SNIPER.cmd"
"""

import base64
b64 = base64.b64encode(launch_cmd_content.encode()).decode()
s.run_ps(f"[System.IO.File]::WriteAllBytes('C:/Users/Administrator/Desktop/LAUNCH_ALL_WINDOWS.cmd', [System.Convert]::FromBase64String('{b64}'))")

# Disable or rename 1_START_THOR_SNIPER.cmd
s.run_ps("""
if (Test-Path 'C:/Users/Administrator/Desktop/1_START_THOR_SNIPER.cmd') {
    Move-Item 'C:/Users/Administrator/Desktop/1_START_THOR_SNIPER.cmd' 'C:/Users/Administrator/Desktop/1_START_THOR_SNIPER.cmd.disabled' -Force
    Write-Output "Renamed 1_START_THOR_SNIPER.cmd to disabled"
}
""")

# Delete Thor tasks
res1 = s.run_cmd('schtasks /Delete /TN "StartThorSniper" /F')
print("Delete StartThorSniper:", res1.std_out.decode('utf-8', errors='replace').strip())

res2 = s.run_cmd('schtasks /Delete /TN "RunThorCmd" /F')
print("Delete RunThorCmd:", res2.std_out.decode('utf-8', errors='replace').strip())

print("THORChain sniper auto-start disabled successfully.")
