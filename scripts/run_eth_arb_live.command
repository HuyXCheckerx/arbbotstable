#!/bin/zsh
set -euo pipefail

cd -- "$(dirname -- "$0")"

if (( $# != 1 )); then
  echo "Usage: ./run_eth_arb_live.command USDC|PYUSD" >&2
  exit 2
fi

loan_token=$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')
case "$loan_token" in
  USDC|PYUSD) ;;
  *)
    echo "ERROR: loan token must be USDC or PYUSD" >&2
    exit 2
    ;;
esac

echo "LIVE ETHEREUM MAINNET: borrow $loan_token, swap to USDT on Matcha,"
echo "then swap USDT back to $loan_token on Stable and repay Morpho."
echo "The Python script will still require a profitable atomic simulation."
expected="RUN_${loan_token}_MAINNET"
read "confirmation?Type $expected to continue: "


python_command="python3"
if [[ -x ".venv-eth/bin/python" ]]; then
  python_command=".venv-eth/bin/python"
fi

exec "$python_command" ../src/engines/eth_flash_arb.py \
  --loan-token "$loan_token" \
  --send \
  --confirm-mainnet EXECUTE_ATOMIC_ARB
