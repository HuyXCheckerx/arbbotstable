#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"
exec .venv-eth/bin/python ../src/engines/multichain_flash_arb.py \
  --chain bsc \
  --send \
  --confirm-mainnet EXECUTE_BSC_ARB
