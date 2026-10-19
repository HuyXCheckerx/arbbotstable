# Local quoters

Two quote engines that price stablecoin swaps without aggregator APIs. Each one mirrors on-chain state from RPC and
runs the real, deployed DEX programs locally on it, so its outputs match the chain to the unit. The sniper uses them as
the `local` DEX provider (see [Sniper](#sniper)). They never sign or send.

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

## Sniper

Start the servers first, then set the providers in `.env`:

```
SOL_FLASH_ARB_DEX_PROVIDER=local          # solana_flash_arb.ts quotes and builds its DEX leg from the Solana server
SOL_LOCAL_QUOTE_URL=http://127.0.0.1:8787
# SOL_LOCAL_QUOTE_TOKEN=                  # the server's QUOTE_SERVER_TOKEN, when it listens beyond loopback
ETH_ARB_QUOTE_PROVIDER=local              # eth_flash_arb_pyusd_usdc.py prices its DEX leg on the EVM server
ETH_LOCAL_QUOTE_URL=http://127.0.0.1:8795
ETH_ARB_LOCAL_EXECUTION_PROVIDER=okx      # okx, direct, matcha, auto or defillama
```

- **Solana** takes both the price and the instructions from the local server, so its DEX leg needs no aggregator.
  Serve every pair and its USDC legs (`QUOTE_PAIRS=PYUSD/USDG,USDG/USDC,PYUSD/USDC QUOTE_VIA=USDC`): the sniper
  allows two-hop routes for the local provider, and a direct PYUSD/USDG market alone often cannot fill the size. A
  route that moved below its quote between `/quote` and `/swap-instructions` is refused and re-quoted.
- **Ethereum** has no executor for direct pool swaps yet, so the local server only prices the leg. The route is
  checked against the profit floor on the local price. Only a route that clears it fetches calldata from
  `ETH_ARB_LOCAL_EXECUTION_PROVIDER`. The floor is then checked again on that executable quote, and the route is
  dropped if the executable price no longer clears it. The log prints both prices and the local route's pools.
- A server whose mirror is behind the chain answers 503. The sniper pauses that chain's local routes
  (`transient-local`, dependency `local:<chain>`) and resumes when the server is current. A size the modelled pools
  cannot fill (422) is reported as "no routes found".
- Observed in an 11-hour dry run (2026-10-18/19): the EVM server on PublicNode often fell behind the 30 s sync limit,
  so Ethereum routes paused. Give it a dedicated RPC, not the one the engines use. After the host slept, the
  Solana server's websockets stayed open but silent; it now opens new ones when it rebuilds a stalled market.
