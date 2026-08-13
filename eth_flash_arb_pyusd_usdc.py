#!/usr/bin/env python3
import runpy
from pathlib import Path

target = Path(__file__).resolve().parent / "src" / "engines" / "eth_flash_arb_pyusd_usdc.py"
runpy.run_path(str(target), run_name="__main__")
