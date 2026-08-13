#!/bin/zsh
set -euo pipefail

cd "${0:A:h}"
npm run solana:flash -- \
  --send \
  --confirm-mainnet EXECUTE_SOLANA_FLASH_ARB
