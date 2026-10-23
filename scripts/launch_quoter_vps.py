#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.vps_cmd as vps

cmd_content = """@echo off
cd /d "C:\\Users\\Administrator\\arbbotstable"
"C:\\Users\\Administrator\\arbbotstable\\venv\\Scripts\\python.exe" src\\engines\\local_quoter_service.py >> logs\\local_quoter.log 2>&1
"""

ps = f"""
$content = @'
{cmd_content}
'@
Set-Content -Path "C:\\Users\\Administrator\\arbbotstable\\run_local_quoter.cmd" -Value $content
Write-Host "Created run_local_quoter.cmd"
"""

vps.run_vps(ps, powershell=True)

# Launch detached process via WMIC
vps.run_vps(r'wmic process call create "cmd.exe /c C:\Users\Administrator\arbbotstable\run_local_quoter.cmd"')
