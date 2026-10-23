#!/usr/bin/env python3
"""Launch the read-only USDP calculator without starting the trading dashboard."""
import argparse
from src.web.thor_calculator import run

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    run(args.host, args.port)
