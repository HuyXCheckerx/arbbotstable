@echo off
setlocal
cd /d "%~dp0"

rem Disable Windows Console QuickEdit mode to prevent clicks from freezing execution
powershell -NoProfile -Command "$k='HKCU:\Console'; Set-ItemProperty -Path $k -Name QuickEdit -Value 0 -Type DWord -ErrorAction SilentlyContinue; Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class WinCon { [DllImport(\"kernel32.dll\", SetLastError=true)] public static extern IntPtr GetStdHandle(int n); [DllImport(\"kernel32.dll\", SetLastError=true)] public static extern bool GetConsoleMode(IntPtr h, out uint m); [DllImport(\"kernel32.dll\", SetLastError=true)] public static extern bool SetConsoleMode(IntPtr h, uint m); public static void DisableQuickEdit() { IntPtr h = GetStdHandle(-10); uint m; if (GetConsoleMode(h, out m)) { SetConsoleMode(h, m & ~0x0040u & ~0x0010u); } } }'; [WinCon]::DisableQuickEdit();" >nul 2>&1

if "%NODE_OPTIONS%"=="" set "NODE_OPTIONS=--max-old-space-size=512"

set "ARGS=%*"
if "%~1"=="" set "ARGS=--live --confirm-live EXECUTE_PROFIT_SNIPER"

:loop
rem Clean up any orphaned background bridge holding port 18234 from previous runs
for /f "tokens=5" %%a in ('netstat -ano -p tcp ^| findstr ":18234" ^| findstr "LISTENING"') do (
  taskkill /F /T /PID %%a >nul 2>nul
)

rem Clean up stale lock and pid files
del /f /q .matcha_bridge.pid .matcha_bridge.lock logs\crosschain-sniper.pid logs\.crosschain-sniper.lock 2>nul

if exist "%~dp0venv\Scripts\python.exe" (
  "%~dp0venv\Scripts\python.exe" sniper.py %ARGS%
) else if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" sniper.py %ARGS%
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 sniper.py %ARGS%
  ) else (
    python sniper.py %ARGS%
  )
)

echo.
echo [%date% %time%] Sniper exited (code %errorlevel%). Auto-restarting in 3 seconds... (Press Ctrl+C to stop)
timeout /t 3 >nul
goto loop
