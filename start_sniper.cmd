@echo off
setlocal
cd /d "%~dp0"

if "%NODE_OPTIONS%"=="" set "NODE_OPTIONS=--max-old-space-size=512"

set "ARGS=%*"
if "%~1"=="" set "ARGS=--live --confirm-live EXECUTE_PROFIT_SNIPER"

rem Clean up any orphaned background bridge holding port 18234 from previous runs
for /f "tokens=5" %%a in ('netstat -ano -p tcp ^| findstr ":18234" ^| findstr "LISTENING"') do (
  taskkill /F /T /PID %%a >nul 2>nul
)

rem Clean up stale lock and pid files
del /f /q .matcha_bridge.pid .matcha_bridge.lock logs\crosschain-sniper.pid logs\.crosschain-sniper.lock 2>nul

if exist "%~dp0venv\Scripts\python.exe" (
  "%~dp0venv\Scripts\python.exe" sniper.py %ARGS%
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 sniper.py %ARGS%
  ) else (
    python sniper.py %ARGS%
  )
)
if errorlevel 1 pause
