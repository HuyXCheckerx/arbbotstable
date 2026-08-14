#!/usr/bin/env python3
"""Launch only the local arbitrage dashboard from one root command."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import threading
import webbrowser

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent


def browser_url(host: str, port: int) -> str:
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{display_host}:{port}"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Start the arbitrage web console and open it in your browser.",
    )
    result.add_argument(
        "--host",
        default=os.environ.get("WEB_HOST", "127.0.0.1"),
        help="listen address (default: 127.0.0.1; use 0.0.0.0 for LAN access)",
    )
    result.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "25284")),
        help="listen port (default: PORT or 25284)",
    )
    result.add_argument(
        "--no-open",
        action="store_true",
        help="start the server without opening a browser tab",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    args = parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    # web.py reads these at import time.
    os.environ["WEB_HOST"] = args.host
    os.environ["PORT"] = str(args.port)
    from src.web.web import run

    url = browser_url(args.host, args.port)
    print("\nArbitrage web console")
    print(f"  Open: {url}")
    print("  Stop: Ctrl+C\n")
    if not args.no_open:
        threading.Timer(0.7, webbrowser.open, args=(url,)).start()

    try:
        run(args.host, args.port)
    except KeyboardInterrupt:
        print("\n[*] Dashboard stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
