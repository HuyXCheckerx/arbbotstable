@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 sniper.py --request-stop
) else (
  python sniper.py --request-stop
)
if errorlevel 1 pause
