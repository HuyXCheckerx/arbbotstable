#!/usr/bin/env python3
"""Run the cross-chain sniper only for PYUSD/USDG routes."""

from __future__ import annotations

import sys

try:
    from .crosschain_sniper import SniperError, main as crosschain_main
except ImportError:  # Support direct execution from src/engines.
    from crosschain_sniper import SniperError, main as crosschain_main


FIXED_CHAINS = ("ethereum", "solana")
FIXED_PAIRS = ("PYUSD/USDG", "USDG/PYUSD")
FIXED_SWAP_ORDERS = ("dex-first", "stable-first")
ROUTE_SELECTION_FLAGS = frozenset(
    {"--chains", "--pairs", "--routes", "--orders", "--swap-orders"}
)
HELP_TEXT = """\
usage: pyusd_usdg_sniper.py [operational options]

Continuously check only PYUSD/USDG and USDG/PYUSD on Ethereum and Solana,
using both dex-first and stable-first venue orders (eight fixed routes).

Operational options are the same as sniper.py, including --threshold-usd,
--interval-seconds, cooldown controls, --once, --live, --confirm-live, and
--request-stop. Route-selection flags are intentionally disabled.
"""


def restricted_sniper_args(argv: list[str]) -> list[str]:
    """Append the fixed route matrix after rejecting attempts to override it."""
    for argument in argv:
        option = argument.split("=", 1)[0]
        if option in ROUTE_SELECTION_FLAGS:
            raise SniperError(
                "PYUSD/USDG sniper routes are fixed to Ethereum and Solana, "
                "both pair directions, and both venue orders"
            )

    return [
        *argv,
        "--chains",
        *FIXED_CHAINS,
        "--pairs",
        *FIXED_PAIRS,
        "--swap-orders",
        *FIXED_SWAP_ORDERS,
    ]


def main(argv: list[str] | None = None) -> int:
    supplied_args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in supplied_args or "-h" in supplied_args:
        print(HELP_TEXT)
        return 0
    return crosschain_main(restricted_sniper_args(supplied_args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SniperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
