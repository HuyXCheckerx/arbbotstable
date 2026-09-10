# =====================================================================
# Antigravity Automated Setup Script for Windows VPS
# Run in an Administrator PowerShell prompt
# =====================================================================

$ErrorActionPreference = 'Stop'
Write-Host '=== Starting Windows VPS setup for arbbotstable ===' -ForegroundColor Cyan

function Refresh-EnvPath {
    $machine = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

# 1. Check / Install Git, Python, Node.js
Write-Host "`n[1/5] Checking core runtime dependencies..." -ForegroundColor Yellow

# Git
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing Git for Windows...' -ForegroundColor Green
    $gitUrl = 'https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe'
    $gitInstaller = "$env:TEMP\git-installer.exe"
    Invoke-WebRequest -Uri $gitUrl -OutFile $gitInstaller
    Start-Process -FilePath $gitInstaller -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS' -Wait
    Remove-Item $gitInstaller -Force -ErrorAction SilentlyContinue
    Refresh-EnvPath
} else {
    Write-Host '  [OK] Git is installed.' -ForegroundColor Green
}

# Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing Python 3.12...' -ForegroundColor Green
    $pyUrl = 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe'
    $pyInstaller = "$env:TEMP\python-installer.exe"
    Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller
    Start-Process -FilePath $pyInstaller -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1' -Wait
    Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
    Refresh-EnvPath
} else {
    $pyVer = python --version
    Write-Host "  [OK] Python is installed ($pyVer)." -ForegroundColor Green
}

# Node.js
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing Node.js LTS (v20)...' -ForegroundColor Green
    $nodeUrl = 'https://nodejs.org/dist/v20.18.1/node-v20.18.1-x64.msi'
    $nodeInstaller = "$env:TEMP\node-installer.msi"
    Invoke-WebRequest -Uri $nodeUrl -OutFile $nodeInstaller
    Start-Process -FilePath 'msiexec.exe' -ArgumentList "/i `"$nodeInstaller`" /qn" -Wait
    Remove-Item $nodeInstaller -Force -ErrorAction SilentlyContinue
    Refresh-EnvPath
} else {
    $nodeVer = node --version
    Write-Host "  [OK] Node.js is installed ($nodeVer)." -ForegroundColor Green
}

# 2. Locate or Clone Project Repository
Write-Host "`n[2/5] Locating project workspace..." -ForegroundColor Yellow

$repoDir = $PSScriptRoot
if (-not (Test-Path "$repoDir\requirements.txt")) {
    $repoDir = (Get-Location).Path
}

if (-not (Test-Path "$repoDir\requirements.txt")) {
    $defaultTarget = "$HOME\arbbotstable"
    if (-not (Test-Path "$defaultTarget\requirements.txt")) {
        Write-Host "Cloning arbbotstable repository to $defaultTarget..." -ForegroundColor Cyan
        git clone 'https://github.com/HuyXCheckerx/arbbotstable.git' $defaultTarget
    }
    $repoDir = $defaultTarget
}

Set-Location $repoDir
Write-Host "Working directory: $repoDir" -ForegroundColor Cyan

# 3. Python Virtual Environment
Write-Host "`n[3/5] Configuring Python Virtual Environment..." -ForegroundColor Yellow

if (-not (Test-Path "$repoDir\venv\Scripts\python.exe")) {
    Write-Host 'Creating virtual environment in .\venv...' -ForegroundColor Cyan
    python -m venv "$repoDir\venv"
}

$venvPython = "$repoDir\venv\Scripts\python.exe"
$venvPip = "$repoDir\venv\Scripts\pip.exe"

Write-Host 'Upgrading pip and installing Python dependencies...' -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

Write-Host 'Installing Playwright Chromium browser...' -ForegroundColor Cyan
& $venvPython -m playwright install chromium

# 4. Node.js Dependencies
Write-Host "`n[4/5] Installing Node.js dependencies..." -ForegroundColor Yellow
$appdataNpm = "$env:APPDATA\npm"
if (-not (Test-Path $appdataNpm)) {
    New-Item -ItemType Directory -Path $appdataNpm -Force | Out-Null
}
if (Get-Command npm -ErrorAction SilentlyContinue) {
    npm install
} elseif (Test-Path 'C:\Program Files\nodejs\npm.cmd') {
    & 'C:\Program Files\nodejs\npm.cmd' install
} else {
    Write-Host 'WARNING: npm not found in PATH; please run `npm install` after restarting your terminal.' -ForegroundColor Yellow
}

# 5. Environment Config (.env)
Write-Host "`n[5/5] Checking configuration (.env)..." -ForegroundColor Yellow
if (-not (Test-Path "$repoDir\.env")) {
    if (Test-Path "$repoDir\.env.example") {
        Copy-Item "$repoDir\.env.example" "$repoDir\.env"
        Write-Host '  Created .env from .env.example. PLEASE EDIT .env WITH YOUR KEYS AND RPC URLS!' -ForegroundColor Red
    } else {
        Write-Host '  WARNING: .env not found. Ensure .env is created before launching.' -ForegroundColor Red
    }
} else {
    Write-Host '  [OK] .env exists.' -ForegroundColor Green
}

Write-Host "`n=====================================================================" -ForegroundColor Green
Write-Host 'Setup Completed Successfully!' -ForegroundColor Green
Write-Host "To run the sniper from ${repoDir}:" -ForegroundColor Cyan
Write-Host '  .\start_sniper.cmd' -ForegroundColor White
Write-Host 'or via PowerShell:' -ForegroundColor Cyan
Write-Host '  .\venv\Scripts\python.exe sniper.py --live --confirm-live EXECUTE_PROFIT_SNIPER' -ForegroundColor White
Write-Host '=====================================================================' -ForegroundColor Green
