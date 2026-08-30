# Solana stablecoin flash arbitrage

`solana_flash_arb.ts` builds one atomic Solana v0 transaction:

1. Begin a zero-fee Marginfi/P0 flash loan.
2. Borrow the configured loan stablecoin from the Marginfi/P0 liquidity layer.
3. Convert the borrowed token to the counter-token through either Jupiter or
   Stable.com.
4. Return the counter-token to the borrowed token through the other venue.
5. Repay all flash-borrowed principal.
6. End the flash loan.

If either swap or repayment fails, Solana reverts the complete transaction. No
custom program deployment is required. The maintained P0 SDK builds the
Marginfi program instructions. The wallet does need one Marginfi
account with no existing liability in the selected loan bank, exactly one
standard Marginfi bank for the loan mint, and existing
associated token accounts for both route tokens.

## Install

```bash
cd /Users/perycent/Downloads/arbbot
npm install
```

Put all configuration in `.env`; the available variables and safe defaults are
documented in `.env.example`.

The implemented flash-loan provider is Marginfi/P0. `--provider auto` resolves
to Marginfi; selecting the unimplemented `solend` value now fails explicitly
instead of silently running against Marginfi.

`SOL_FLASH_ARB_LOAN_TOKEN` selects USDC, PYUSD, or USDG and is always the token
borrowed, repaid, and used for the profit floor. `SOL_FLASH_ARB_INTERMEDIATE_TOKEN`
selects a different counter-token. `SOL_FLASH_ARB_SWAP_ORDER=stable-first`
means Stable.com then Jupiter; `dex-first` means Jupiter then Stable.com. Loan
`PYUSD` plus intermediate `USDC` therefore always borrows PYUSD, regardless of
the selected venue order. The token-specific
`SOL_FLASH_ARB_AMOUNT_<LOAN>` value is the maximum principal; the legacy
`SOL_FLASH_ARB_AMOUNT_USDC` value remains the fallback. Before requesting a
signed Stable.com order, the bot compares the Stable input with Stable.com's
current `balance`, leaves the matching capacity buffer unused, and re-quotes at
a smaller loan size when necessary. This applies to both venue orders; neither
route can request more than Stable.com's reported input pool.

The default Jupiter account ceiling is 20. Every built transaction is still
serialized and rejected if the combined Marginfi, Jupiter, and Stable.com
instructions exceed Solana's 1,232-byte wire limit. The directional PYUSD/USDG
Jupiter return markets may require a Jupiter-managed hop through USDC; when one
direction cannot fit atomically, the sniper marks that direction unavailable
and waits for its cooldown instead of repeatedly calling the quote services.

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
