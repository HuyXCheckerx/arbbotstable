@echo off
setlocal
cd /d "%~dp0"

set "ARGS=%*"
if "%~1"=="" set "ARGS=--live --confirm-live EXECUTE_PROFIT_SNIPER"

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
