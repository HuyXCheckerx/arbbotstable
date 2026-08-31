#!/usr/bin/env python3
"""Repository entry point for the PYUSD/USDG-only profit sniper."""

from __future__ import annotations

import sys

from src.engines.crosschain_sniper import SniperError
from src.engines.pyusd_usdg_sniper import main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SniperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
