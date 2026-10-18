# Local quoters

Two quote engines that price stablecoin swaps without aggregator APIs. Each one mirrors on-chain state from RPC and
runs the real, deployed DEX programs locally on it, so its outputs match the chain to the unit. Neither is wired into
the sniper or `swapstable` yet: nothing in this repository calls them, and they never sign or send.

| | `solana/` | `evm/` |
|---|---|---|
| Language | TypeScript (Node), LiteSVM | Rust, revm |
| Chains | Solana | Ethereum, BNB Chain, Polygon PoS |
| Pairs | USDC, USDT, USDG, PYUSD (any pair, two-hop through USDC/SOL) | USDC/USDT everywhere; on Ethereum any pair of USDC, USDT, PYUSD, USDG, USDS, crvUSD, up to three hops |
| Venues | Orca Whirlpool, Raydium CLMM, PancakeSwap, Manifest, GoonFi V2, Deriverse, JupLend AMM, BisonFi, Kipseli | Uniswap v3/v4, PancakeSwap v3/Infinity, Sushi v3, QuickSwap v3, Curve, Balancer v2, Fluid, DODO, Maverick v2, Sky PSM |
| Service | `npm run quote-server` (127.0.0.1:8787; `QUOTE_PAIRS` serves several pairs in one process) | `evm-quoter serve --chain eth` (127.0.0.1:8795) |
| Executable output | `GET /swap-instructions?taker=PUBKEY`: unsigned transaction for the taker to sign | quotes only (an executor contract for direct pool swaps is a later step) |
| Windows VPS | LiteSVM has no Windows build: run under WSL1 or Linux (see `solana/README.md`) | native build with Rust's GNU toolchain: `evm/scripts/build-windows.ps1` |

Measured on 2026-10-17/18 (details in each README):
- **Solana:** at or above Jupiter's best route in 24 of 24 cases (1k–1M, both directions; median +0.006 bp). OKX was ahead by a median 0.022 bp through venues not modelled here. Quote server p50 ~80 ms.
- **EVM:** about even with 0x (median −0.04 to +0.01 bp per chain). It beats 1inch's LlamaSwap quote by ~1 bp, and trails LI.FI's RFQ fills by 0–0.9 bp. On PYUSD/USDG: identical to 0x. Warm server p50 8–35 ms.

Neither sees RFQ or private market-maker liquidity. Each needs a good RPC; Solana also needs Helius (or another
RPC that serves `getProgramAccounts`) for pool discovery.
