#!/usr/bin/env python3
"""Launcher for the USDP back-runner.

Usage:
  python usdp_backrun.py                     # watch every block, print dips (no orders, no transactions)
  python usdp_backrun.py --once              # scan the latest block once
  python usdp_backrun.py --block 26075895    # scan a past block (archive RPC)
  python usdp_backrun.py --dry-run           # Stable.com order + on-chain pre-flight, never broadcasts
  python usdp_backrun.py --live --confirm-live LIVE
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.engines.usdp_backrunner import main

if __name__ == "__main__":
    raise SystemExit(main())
