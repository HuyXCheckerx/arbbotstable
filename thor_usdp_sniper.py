#!/usr/bin/env python3
"""Launcher for the standalone THORChain USDC -> USDP Sniper Engine.

Usage:
  # Monitor / quote loop (dry-run):
  python3 thor_usdp_sniper.py

  # Test single quote:
  python3 thor_usdp_sniper.py --once

  # Custom amount and threshold:
  python3 thor_usdp_sniper.py --amount 10000 --min-usdp 10010 --poll-interval 10

  # Live execution:
  python3 thor_usdp_sniper.py --live --confirm-live EXECUTE_THOR_SNIPER
"""

import os
import subprocess
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Auto-redirect to local virtualenv if launched via global/system python
if sys.platform == "win32":
    venv_python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
else:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"

if (
    venv_python.exists()
    and sys.executable.lower() != str(venv_python).lower()
    and os.getenv("_ARBBOT_VENV_WRAPPED") != "1"
):
    os.environ["_ARBBOT_VENV_WRAPPED"] = "1"
    sys.exit(subprocess.call([str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:]))

from src.engines.thorchain_usdp_sniper import main

if __name__ == "__main__":
    raise SystemExit(main())
