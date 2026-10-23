#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.vps_cmd as vps

cmd_content = """@echo off
cd /d "C:\\Users\\Administrator\\arbbotstable"
"C:\\Users\\Administrator\\arbbotstable\\venv\\Scripts\\python.exe" thor_usdp_sniper.py --resting --amount 10000 --affiliate symbiosis --affiliate-bps 1 --floor-mode 2bps --live --confirm-live EXECUTE_THOR_SNIPER >> logs\\thor_usdp_sniper.log 2>&1
"""

# Write batch script
ps = f"""
$content = @'
{cmd_content}
'@
Set-Content -Path "C:\\Users\\Administrator\\arbbotstable\\run_thor_sniper.cmd" -Value $content
Write-Host "Created run_thor_sniper.cmd"
"""

vps.run_vps(ps, powershell=True)

# Launch detached process via WMIC
vps.run_vps(r'wmic process call create "cmd.exe /c C:\Users\Administrator\arbbotstable\run_thor_sniper.cmd"')
