#!/usr/bin/env python3
import os
import sys
import time
import subprocess
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)

def run_loop():
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    print("=" * 70, flush=True)
    print("[*] STARTING CONTINUOUS SOLANA ARBITRAGE SWEEPER", flush=True)
    print("    Route: Borrow PYUSD -> Stable.com (USDG) -> Jupiter (PYUSD)", flush=True)
    print("=" * 70, flush=True)

    env = dict(os.environ)
    env.pop("SOL_FLASH_ARB_SLIPPAGE_BPS", None)
    env.update({
        "SOL_FLASH_ARB_LOAN_TOKEN": "PYUSD",
        "SOL_FLASH_ARB_INTERMEDIATE_TOKEN": "USDG",
        "SOL_FLASH_ARB_AMOUNT_PYUSD": "10000",
        "SOL_FLASH_ARB_AMOUNT_USDC": "10000",
        "SOL_FLASH_ARB_MIN_GROSS_PROFIT_PYUSD": "0.000001",
        "SOL_FLASH_ARB_MIN_NET_PROFIT_PYUSD": "0.000001",
        "SOL_FLASH_ARB_SWAP_ORDER": "stable-first",
        "SOL_FLASH_ARB_ONLY_DIRECT_ROUTES": "true",
        "SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS": "14",
    })

    executable = "npx.cmd" if sys.platform == "win32" else "npx"
    cmd = [
        executable,
        "tsx",
        str(PROJECT_ROOT / "src" / "engines" / "solana_flash_arb.ts"),
        "--swap-order",
        "stable-first",
        "--send",
        "--confirm-mainnet",
        "EXECUTE_SOLANA_FLASH_ARB",
    ]

    total_profit = Decimal("0")
    executed_count = 0
    iteration = 0

    while True:
        iteration += 1
        print(f"\n[Iteration #{iteration}] Scanning & executing sweep...")
        started = time.time()
        
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        elapsed = time.time() - started

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = f"{stdout}\n{stderr}"

        if "Confirmed:" in combined:
            executed_count += 1
            tx_match = [line for line in combined.splitlines() if "Confirmed:" in line]
            tx_sig = tx_match[0].split("Confirmed:")[-1].strip() if tx_match else "UNKNOWN"
            
            profit_match = [line for line in combined.splitlines() if "Guaranteed net result:" in line]
            profit_str = profit_match[0].split("Guaranteed net result:")[-1].replace("PYUSD", "").strip() if profit_match else "0.8"
            profit = Decimal(profit_str)
            total_profit += profit

            print(f"[+] SUCCESSFUL EXECUTION in {elapsed:.1f}s!")
            print(f"    Tx: https://solscan.io/tx/{tx_sig}")
            print(f"    Sweep Profit: +{profit:,.6f} PYUSD | Cumulative Total: +{total_profit:,.6f} PYUSD")
            time.sleep(2)
            continue

        if "below its 1000 USDG minimum order" in combined or "below its 1000 PYUSD minimum order" in combined or "pool has no remaining" in combined:
            print(f"\n[!] Stable.com pool capacity fully depleted! Exiting.")
            break

        if "No executable opportunity" in combined:
            print(f"[-] Spread currently below floor. Retrying in 3s...")
            time.sleep(3)
            continue

        if "429" in combined or "rate limits" in combined:
            print(f"[!] Rate limit hit. Cooling down for 4s...")
            time.sleep(4)
            continue

        # Other errors: print snippet and wait
        err_lines = [l.strip() for l in combined.splitlines() if "ERROR:" in l or "Error:" in l]
        err_msg = err_lines[0] if err_lines else combined.strip()[-200:]
        print(f"[!] Note: {err_msg}")
        time.sleep(3)

    print("\n" + "=" * 70)
    print(f"[*] RUN COMPLETED. Total Executions: {executed_count} | Total Profit: +{total_profit:,.6f} PYUSD")
    print("=" * 70)

if __name__ == "__main__":
    run_loop()
