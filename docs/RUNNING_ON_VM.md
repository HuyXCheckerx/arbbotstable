# =====================================================================
# WINDOWS VPS SETUP & RUN
# =====================================================================

# 1. Open PowerShell as Administrator and run the automated setup:
git clone https://github.com/HuyXCheckerx/arbbotstable.git
cd arbbotstable
powershell -ExecutionPolicy Bypass -File .\setup_windows_vps.ps1

# 2. Configure .env with your private keys and RPC URLs:
# Make sure ETH_ARB_QUOTE_PROVIDER=okx is set for browser-free, high-speed ETH routing:
notepad .env

# 3. Run the sniper (auto-restarts on crash):
.\start_sniper.cmd
# or in PowerShell:
.\venv\Scripts\python.exe sniper.py --live --confirm-live EXECUTE_PROFIT_SNIPER

# To run Ethereum only:
.\venv\Scripts\python.exe sniper.py --chains ethereum --live --confirm-live EXECUTE_PROFIT_SNIPER

# To run Solana only:
.\venv\Scripts\python.exe sniper.py --chains solana --live --confirm-live EXECUTE_PROFIT_SNIPER
# =====================================================================
# LINUX VPS SETUP (UBUNTU / DEBIAN)
# =====================================================================
# 1. Automated setup:
curl -fsSL https://raw.githubusercontent.com/HuyXCheckerx/arbbotstable/main/setup_ubuntu_vm.sh | bash
# 2. Or start in a persistent tmux session:
cd ~/arbbotstable
tmux new -s sniper './start_sniper.sh --live --confirm-live EXECUTE_PROFIT_SNIPER'
tmux attach -t sniper

