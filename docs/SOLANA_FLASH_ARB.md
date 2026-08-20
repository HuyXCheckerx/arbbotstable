# Solana stablecoin flash arbitrage

`solana_flash_arb.ts` builds one atomic Solana v0 transaction:

1. Begin a zero-fee Marginfi/P0 flash loan.
2. Borrow the configured loan stablecoin from the Marginfi/P0 liquidity layer.
3. Swap the loan stablecoin to the configured intermediate with Jupiter.
4. Swap the conservative intermediate amount back to the loan stablecoin with a Stable.com signed
   single-chain instruction.
5. Repay all flash-borrowed principal.
6. End the flash loan.

If either swap or repayment fails, Solana reverts the complete transaction. No
custom program deployment is required. The maintained P0 SDK builds the
Marginfi program instructions. The wallet does need one Marginfi
account, exactly one standard Marginfi bank for the loan mint, and existing
associated token accounts for both route tokens.

## Install

```bash
cd /Users/perycent/Downloads/arbbot
npm install
```

Put all configuration in `.env`; the available variables and safe defaults are
documented in `.env.example`.

`SOL_FLASH_ARB_LOAN_TOKEN` selects USDC, PYUSD, or USDG. The route is written as
`intermediate/loan`: for example, `SOL_FLASH_ARB_INTERMEDIATE_TOKEN=USDG` with
`SOL_FLASH_ARB_LOAN_TOKEN=PYUSD` runs `USDG/PYUSD`. The token-specific
`SOL_FLASH_ARB_AMOUNT_<LOAN>` value is the maximum principal; the legacy
`SOL_FLASH_ARB_AMOUNT_USDC` value remains the fallback. Before requesting a
signed Stable.com order, the bot compares the first Jupiter leg's guaranteed
minimum output with Stable.com's current `balance`, leaves the matching
`SOL_FLASH_ARB_STABLE_CAPACITY_BUFFER_<INTERMEDIATE>` unused, and re-quotes at a
smaller loan size when necessary.

The default Jupiter account ceiling is 20. That keeps the combined Marginfi,
Jupiter, and Stable.com transaction below Solana's 1,232-byte wire limit while
still allowing current stablecoin routes. The dashboard and sniper override
`SOL_FLASH_ARB_ONLY_DIRECT_ROUTES` to `false` for `USDG/PYUSD` and
`PYUSD/USDG`, since Jupiter currently needs a multi-hop path for those pairs.

If the wallet has no Marginfi account, create a dedicated empty one once:

```bash
npm run solana:flash -- \
  --create-marginfi-account \
  --send \
  --confirm-mainnet CREATE_MARGINFI_ACCOUNT
```

Copy the printed address into `SOL_FLASH_ARB_MARGINFI_ACCOUNT` in `.env`.

## Quote and dry-run

The quote-only command makes no RPC transaction and signs nothing:

```bash
npm run solana:quote
```

The normal command builds, signs locally, and simulates the exact atomic
transaction without sending it:

```bash
npm run solana:flash
```

The Stable.com leg spends the first Jupiter leg's `otherAmountThreshold`, not
its optimistic output. The bot first reads Stable.com's executable output from
`/swap/status`, then requests the signed Solana instruction from
`/swap/create/singleChain`. The profitability check uses that signed output and
subtracts the RPC-calculated network fee, any Stable native execution fee, and
a SOL-price buffer.

## Live execution

Only use this after the dry-run reports a successful simulation and positive
guaranteed net result:

```bash
npm run solana:flash -- \
  --send \
  --confirm-mainnet EXECUTE_SOLANA_FLASH_ARB
```

This cannot guarantee that a transaction lands before either route changes or
the Stable order expires. Jupiter's threshold, Stable's signed order, and
atomic repayment prevent a partial trade, but a reverted transaction still
consumes a Solana transaction fee.
