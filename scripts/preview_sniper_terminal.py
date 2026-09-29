"""Preview the terminal with synthetic quotes. No bot, wallet or network access."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.engines.sniper_terminal import render_dashboard


def sample_snapshot():
    now = datetime.now(timezone.utc)
    routes = {}
    tokens = ("USDC", "USDG", "PYUSD")
    for chain in ("ethereum", "solana"):
        for loan in tokens:
            for counter in tokens:
                if loan == counter:
                    continue
                for order in ("dex-first", "stable-first"):
                    index = len(routes)
                    key = f"{chain}-{order}-{loan}-{counter}"
                    state = ("NO TRADE", "CHECKING", "NO TRADE", "PAUSED", "READY", "NO TRADE")[index % 6]
                    routes[key] = {
                        "key": key, "chain": chain, "pair": f"{loan}/{counter}",
                        "swap_order": order, "dex_name": "MetaMatcha", "state": state,
                        "gross_profit": None if state == "PAUSED" else "7.2851" if state == "READY" else "-0.193082",
                        "net_profit": None if state == "PAUSED" else "5.1068" if state == "READY" else "-0.325008",
                        "checked_at": (now - timedelta(seconds=index + 2)).isoformat(),
                        "cooldown_until": (now + timedelta(seconds=48)).isoformat() if state == "PAUSED" else None,
                        "detail": "Simulated quote only - no transactions or network requests.",
                    }
    return {
        "session": {"mode": "dry-run", "status": "DEMO", "started_at": (now - timedelta(seconds=125)).isoformat()},
        "summary": {"checks": 128, "ready": 4, "confirmed": 0, "submitted": 0, "no_trade": 112, "paused": 12, "errors": 0},
        "routes": routes, "recent_results": list(routes.values())[:6],
    }


def main():
    from rich.console import Console
    from rich.live import Live

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="print one preview frame")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    args = parser.parse_args()
    console = Console(width=args.width, height=args.height)
    snapshot = sample_snapshot()
    started = time.monotonic()

    def frame():
        return render_dashboard(snapshot, width=console.width, height=console.height, elapsed=time.monotonic() - started)

    if args.once:
        console.print(frame())
        return
    try:
        with Live(get_renderable=frame, console=console, refresh_per_second=2, vertical_overflow="crop"):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
