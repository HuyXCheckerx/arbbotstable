#!/usr/bin/env python3
"""Launcher for the USDP back-runner.

Usage:
  python usdp_backrun.py                     # watch every block, print dips (no orders, no transactions)
  python usdp_backrun.py --once              # scan the latest block once
  python usdp_backrun.py --block 26075895    # scan a past block (archive RPC)
  python usdp_backrun.py --dry-run           # Stable.com order + on-chain pre-flight, never broadcasts
  python usdp_backrun.py --live --confirm-live LIVE

Float mode (dips under Stable.com's 1,000 USDP minimum, bought with the operator wallet's own USDC):
  python usdp_backrun.py --float-usdc 2000                      # also show small dips
  python usdp_backrun.py --block 26174899 --dry-run --float-usdc 2000   # pre-flight a past small-dip buy
  python usdp_backrun.py --dry-run --float-usdc 2000            # paper-trade (logs/usdp-float-paper.json)
  python usdp_backrun.py --live --confirm-live LIVE --float-usdc 2000 --float-sell-assets USDC,USDG
  python usdp_backrun.py --float-status                         # inventory and PnL
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.engines.usdp_backrunner import main

if __name__ == "__main__":
    raise SystemExit(main())
