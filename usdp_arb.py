#!/usr/bin/env python3
"""Launcher for the standalone USDP Arbitrage Test Engine.

Usage:
  python usdp_arb.py
  python usdp_arb.py --amount 10000 --create-order
  python usdp_arb.py --continuous --interval 3
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.engines.usdp_arb_engine import main

if __name__ == "__main__":
    raise SystemExit(main())
