@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 sniper.py --live --confirm-live EXECUTE_PROFIT_SNIPER
) else (
  python sniper.py --live --confirm-live EXECUTE_PROFIT_SNIPER
)
if errorlevel 1 pause
