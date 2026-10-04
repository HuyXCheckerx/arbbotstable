#!/usr/bin/env python3
import os
import runpy
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

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

if __name__ == "__main__":
    target = PROJECT_ROOT / "src" / "engines" / "crosschain_sniper.py"
    runpy.run_path(str(target), run_name="__main__")
