@echo off
setlocal
cd /d "%~dp0"

echo ===================================================
echo [HardRestart] Executing Full Hard Restart for ArbBot
echo ===================================================

rem 1. Kill any process listening on Matcha bridge port 18234
echo [HardRestart] Terminating any process on port 18234...
for /f "tokens=5" %%a in ('netstat -ano -p tcp ^| findstr ":18234" ^| findstr "LISTENING"') do (
  taskkill /F /T /PID %%a >nul 2>nul
)

rem 2. Force kill node.exe processes (solana quote engine / playwright)
echo [HardRestart] Terminating lingering Node.js / Playwright processes...
taskkill /F /IM node.exe >nul 2>nul

rem 3. Clean up stale lock and pid files
echo [HardRestart] Cleaning stale lock and PID files...
del /f /q .matcha_bridge.pid .matcha_bridge.lock logs\crosschain-sniper.pid logs\.crosschain-sniper.lock .matcha_cookies.json 2>nul

rem 4. Select python interpreter
set "PY_EXE=python"
if exist "%~dp0venv\Scripts\python.exe" (
  set "PY_EXE=%~dp0venv\Scripts\python.exe"
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    set "PY_EXE=py -3"
  )
)

rem 5. Check and rotate proxy if needed
echo [HardRestart] Verifying proxy status and rotating if needed...
%PY_EXE% scripts\manage_proxyisp.py --rotate-if-needed

echo ===================================================
echo [HardRestart] Launching Sniper Engine cleanly...
echo ===================================================
call "%~dp0start_sniper.cmd" %*
