#!/usr/bin/env python3
"""Compatibility wrapper for the repository-root server bootstrap."""

import runpy
from pathlib import Path


target = Path(__file__).resolve().parents[2] / "server_start.py"
runpy.run_path(str(target), run_name="__main__")
