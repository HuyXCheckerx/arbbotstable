# =====================================================================
# Antigravity Automated Setup Script for Windows VPS
# Run in an Administrator PowerShell prompt
# =====================================================================

$ErrorActionPreference = "Stop"
Write-Host ">>> Starting Windows VPS setup for arbbotstable..." -ForegroundColor Cyan

# 1. Check / Install Git, Python, Node.js
function Install-Tools {
    Write-Host "`n[1/5] Checking core runtime dependencies..." -ForegroundColor Yellow

    # Check Git
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "Installing Git..." -ForegroundColor Green
        $gitUrl = "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe"
        $gitInstaller = "$env:TEMP\git-installer.exe"
        Invoke-WebRequest -Uri $gitUrl -OutFile $gitInstaller
        Start-Process $gitInstaller -ArgumentList "/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS" -Wait
        Remove-Item $gitInstaller -Force -ErrorAction SilentlyContinue
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    } else {
        Write-Host "  ✓ Git is installed." -ForegroundColor Green
    }

    # Check Python
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Host "Installing Python 3.12..." -ForegroundColor Green
        $pyUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
        $pyInstaller = "$env:TEMP\python-installer.exe"
        Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller
        Start-Process $pyInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1" -Wait
        Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    } else {
        Write-Host "  ✓ Python is installed ($(python --version))." -ForegroundColor Green
    }

    # Check Node.js
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Host "Installing Node.js LTS (v20)..." -ForegroundColor Green
        $nodeUrl = "https://nodejs.org/dist/v20.18.1/node-v20.18.1-x64.msi"
        $nodeInstaller = "$env:TEMP\node-installer.msi"
        Invoke-WebRequest -Uri $nodeUrl -OutFile $nodeInstaller
        Start-Process msiexec.exe -ArgumentList "/i `"$nodeInstaller`" /qn" -Wait
        Remove-Item $nodeInstaller -Force -ErrorAction SilentlyContinue
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    } else {
        Write-Host "  ✓ Node.js is installed ($(node --version))." -ForegroundColor Green
    }
}

Install-Tools

# 2. Virtual Environment Setup
Write-Host "`n[2/5] Configuring Python Virtual Environment..." -ForegroundColor Yellow
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path "$projectRoot\requirements.txt")) {
    $projectRoot = (Get-Location).Path
}

Set-Location $projectRoot

if (-not (Test-Path "venv")) {
    Write-Host "Creating virtual environment in .\venv..." -ForegroundColor Cyan
    python -m venv venv
}

$venvPython = "$projectRoot\venv\Scripts\python.exe"
$venvPip = "$projectRoot\venv\Scripts\pip.exe"

# 3. Python Dependencies
Write-Host "`n[3/5] Installing Python packages from requirements.txt..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

Write-Host "`nInstalling Playwright Chromium browser..." -ForegroundColor Cyan
& $venvPython -m playwright install chromium

# 4. Node.js Dependencies
Write-Host "`n[4/5] Installing Node.js dependencies..." -ForegroundColor Yellow
npm install

# 5. Environment Config (.env)
Write-Host "`n[5/5] Checking configuration (.env)..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "  Created .env from .env.example. PLEASE EDIT .env WITH YOUR PRIVATE KEYS AND RPC URLs!" -ForegroundColor Red
    } else {
        Write-Host "  WARNING: .env not found. Ensure .env is populated before running." -ForegroundColor Red
    }
} else {
    Write-Host "  ✓ .env exists." -ForegroundColor Green
}

Write-Host "`n=====================================================================" -ForegroundColor Green
Write-Host "Setup Completed Successfully!" -ForegroundColor Green
Write-Host "To run the sniper:" -ForegroundColor Cyan
Write-Host "  .\venv\Scripts\python.exe sniper.py --live --confirm-live EXECUTE_PROFIT_SNIPER" -ForegroundColor White
Write-Host "or simply:" -ForegroundColor Cyan
Write-Host "  .\start_sniper.cmd" -ForegroundColor White
Write-Host "=====================================================================" -ForegroundColor Green
