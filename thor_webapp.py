#!/usr/bin/env python3
"""Launcher for the THORChain USDC -> USDP Terminal WebApp.

Usage:
  # Start on default port 8080 and open browser:
  python3 thor_webapp.py

  # Custom port:
  python3 thor_webapp.py --port 8888

  # Start without opening browser tab:
  python3 thor_webapp.py --no-open
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Auto-redirect to local virtualenv if launched via global python
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

from src.web.thor_app import run


def parser() -> argparse.ArgumentParser:
    res = argparse.ArgumentParser(description="THORChain USDC -> USDP WebApp Terminal")
    res.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"), help="Listen host (default: 127.0.0.1)")
    res.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")), help="Listen port (default: 8080)")
    res.add_argument("--no-open", action="store_true", help="Do not open browser automatically")
    return res


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{args.port}"

    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()

    try:
        run(host=args.host, port=args.port)
    except OSError as exc:
        if "already in use" in str(exc).lower() or getattr(exc, "errno", 0) in (48, 98):
            print(f"\n[*] THORChain WebApp is already active at {url}!")
            if not args.no_open:
                print(f"[*] Opening browser tab: {url}")
                webbrowser.open(url)
            return 0
        raise
    except KeyboardInterrupt:
        print("\n[*] THORChain WebApp stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
