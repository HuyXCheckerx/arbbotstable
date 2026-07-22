# Stable/Jupiter Flash-Loan Arbitrage Bot

This is a separate, atomic version of the existing Stable.com/Jupiter arbitrage bot. It borrows between **10,000 and 100,000 USDC** from Jupiter Lend, executes both exchange legs, and repays the exact USDC principal in one Solana transaction.

The original bot remains untouched in the parent folder.

## Routes

The scanner evaluates these direct cycles:

```text
USDC --Stable.com--> USDG  --Jupiter--> USDC
USDC --Stable.com--> PYUSD --Jupiter--> USDC
USDC --Jupiter--> USDG  --Stable.com--> USDC
USDC --Jupiter--> PYUSD --Stable.com--> USDC
USDC --Jupiter--> USDT  --Stable.com--> USDC
```

Candidate sizes default to 10k, 20k, 50k, and 100k, followed by refinement around the best candidate. The program has a hard guard that refuses amounts below 10k or above 100k even if the environment is misconfigured.

The two three-leg USDG/PYUSD cross routes from the old bot are intentionally excluded. They require two independently maintainer-signed Stable.com orders in one transaction; both orders currently resolve against the same on-chain wallet nonce when requested before submission.

## Atomic safety

Every submitted transaction has this shape:

```text
compute budget
Jupiter Lend flash borrow
first venue instruction(s)
second venue instruction(s)
Jupiter Lend payback
```

The scanner qualifies a route using Jupiter's `otherAmountThreshold`—the minimum output encoded into the swap instruction—not the optimistic `outAmount`. A route is eligible only when that minimum output covers:

```text
flash-loan principal + configured transaction-cost reserve + minimum net profit
```

If a swap, Stable.com order, or repayment fails, Solana reverts the whole transaction. Network and priority fees can still be charged for a failed transaction.

Jupiter-first routes may leave a small, positive balance of the intermediate token when execution receives better output than Jupiter's minimum. `JUPITER_SLIPPAGE_BPS=0` minimizes this, at the cost of a lower landing rate.

## Setup

Requirements: Node.js 20 or newer, a Solana RPC, a Jupiter API key, and a wallet with enough SOL for account rent and transaction fees. Working USDC is not required.

```powershell
cd D:\Stuff\arbbotstable\flashloan-arbbot
Copy-Item .env.example .env
npm install
```

Fill in `.env`. `SOLANA_PRIVATE_KEY` accepts either base58 or a JSON byte array. Never commit `.env`.

Create the wallet's USDC, USDG, PYUSD, and USDT associated token accounts:

```powershell
npm run init-atas
```

Create a custom address lookup table containing the static Jupiter Lend and Stable.com accounts:

```powershell
npm run create-alt
```

Both commands make small on-chain transactions and therefore require `SOLANA_PRIVATE_KEY` and SOL. The ALT command writes its address to `state/custom-alt.json`; wait at least one slot before scanning.

## Dry run

Dry run is the default. It fetches quotes, obtains the Stable.com signed order, constructs the complete flash-loan transaction, and simulates it without signing or submitting it.

```powershell
npm run scan
```

Continuous dry-run scanning:

```powershell
npm start
```

If transaction construction reports that the 1,232-byte limit was exceeded, verify that `state/custom-alt.json` exists and points to an active lookup table. Lowering `JUPITER_MAX_ACCOUNTS` can reduce size but may worsen routing.

## Live execution

Run successful dry simulations first. Then set both flags:

```dotenv
EXECUTION_ENABLED=true
DRY_RUN=false
```

Start the bot:

```powershell
npm start
```

Before broadcasting, the bot:

1. Checks Stable.com pool capacity and protected reserves.
2. Rebuilds the winning Jupiter route immediately before execution.
3. Rejects routes whose guaranteed output no longer clears the profit floor.
4. Builds a v0 transaction with Jupiter and custom lookup tables.
5. Simulates the complete transaction and rejects any program or compute error.
6. Persists the signed transaction's signature and blockhash before broadcasting.

An ambiguous or unconfirmed signature locks further submissions until it confirms, fails, or its blockhash expires. The durable lock is stored in `state/bot-state.json`.

## Commands

```text
npm run scan        One scan/revalidation/simulation cycle
npm start           Continuous scanner
npm run init-atas   Create required wallet token accounts
npm run create-alt  Create and populate the static address lookup table
npm run check       Type-check the codebase
npm test            Run unit tests
```

## Costs and operational limits

- Jupiter Lend currently charges no flash-loan fee, but DEX fees, Stable.com's USDT fee, price impact, priority fees, and failed-transaction fees still apply.
- Actual flash-loan availability depends on Jupiter Lend's live USDC reserves and ceilings. Simulation catches insufficient liquidity before submission, but liquidity can still change afterward.
- Jupiter Swap V2 `/build` provides composable Metis routes. Prices can differ from the original bot's `/order` meta-aggregator routes.
- This code sends through the configured RPC. For production arbitrage, use a high-quality low-latency RPC and tune the priority fee using observed landing data.
- The default profit floor is inherited from the original bot and is deliberately configurable. It is not a claim that a ten-cent opportunity is economically attractive or competitive.

## Dependency note

The implementation uses Jupiter's official `@jup-ag/lend` SDK. As of this build, `npm audit` reports unresolved transitive advisories in the Solana 1.x dependency tree. Review them and future SDK releases before operating a funded signer.
