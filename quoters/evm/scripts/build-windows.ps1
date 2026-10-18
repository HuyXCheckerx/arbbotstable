# Builds evm-quoter.exe on Windows with Rust's GNU toolchain (no Visual Studio / MSVC license).
#
# One-time setup (run in PowerShell):
#   1. Rust, GNU host, pinned to the version this crate was built with:
#        Invoke-WebRequest https://win.rustup.rs/x86_64 -OutFile rustup-init.exe
#        .\rustup-init.exe -y --default-host x86_64-pc-windows-gnu --default-toolchain 1.90.0 --profile minimal
#      Open a new PowerShell afterwards so %USERPROFILE%\.cargo\bin is on PATH.
#   2. A C compiler for `ring` (rustls' crypto; it is the only dependency with C code):
#        winget install MSYS2.MSYS2
#        C:\msys64\usr\bin\bash.exe -lc "pacman -S --needed --noconfirm mingw-w64-x86_64-gcc"
#
# Then, from the evm-quoter folder:
#   powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
#
# The binary lands in target\release\evm-quoter.exe. Example:
#   .\target\release\evm-quoter.exe serve --chain eth --port 8795 --warm-pairs "PYUSD,USDG"
#   curl "http://127.0.0.1:8795/quote?input=PYUSD&output=USDG&amount=10000"

$ErrorActionPreference = "Stop"
$mingw = "C:\msys64\mingw64\bin"
if (Test-Path $mingw) { $env:PATH = "$mingw;$env:PATH" }

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "cargo not found: install Rust with the GNU host first (see the top of this script)."
}
if (-not (Get-Command gcc -ErrorAction SilentlyContinue)) {
    throw "gcc not found: install MSYS2's mingw-w64-x86_64-gcc and keep $mingw on PATH (see the top of this script)."
}
$host_triple = (rustc -vV | Select-String "host:").ToString().Split(" ")[1]
Write-Host "rustc host: $host_triple"
if ($host_triple -notlike "*windows-gnu*") {
    Write-Warning "The default toolchain is $host_triple; building for x86_64-pc-windows-gnu explicitly."
    rustup target add x86_64-pc-windows-gnu
    cargo build --release --target x86_64-pc-windows-gnu
    $exe = "target\x86_64-pc-windows-gnu\release\evm-quoter.exe"
} else {
    cargo build --release
    $exe = "target\release\evm-quoter.exe"
}
& $exe --help | Select-Object -First 5
Write-Host "built $exe"
