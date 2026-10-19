# evm-quoter

A local stablecoin exact-input quote engine for Ethereum, BNB Chain and Polygon PoS, modelled on the Solana engine in `../solana/src/quote`. It quotes USDC/USDT on all three chains and, on Ethereum, any pair of USDC, USDT, PYUSD, USDG, USDS and crvUSD, through routes of up to three hops. It executes the real, deployed pool contracts in revm against chain state mirrored from RPC. It never reimplements pool math. It needs no API key, wallet or private RPC: everything below ran on PublicNode's free endpoints (2026-10-17/18).

```bash
cargo build --release
./target/release/evm-quoter quote --chain eth --amount 100000                     # USDC -> USDT
./target/release/evm-quoter quote --chain eth --input PYUSD --output USDG --amount 100000
./target/release/evm-quoter quote --chain eth --input USDG --output PYUSD --amount 10000 --max-hops 2 --via USDC
./target/release/evm-quoter quote --chain bsc --amount 1000000 --input USDT --output USDC --json
./target/release/evm-quoter discover --chain polygon       # refresh artifacts/pools-polygon.json
./target/release/evm-quoter verify --chain eth --wait 30   # local vs node eth_call, after 30 s of sync
./target/release/evm-quoter serve --chain eth --warm-pairs "PYUSD,USDG"   # http://127.0.0.1:8795/quote?input=PYUSD&output=USDG&amount=100000
./target/release/evm-quoter bench --chains eth,bsc,polygon --aggs paraswap,cow,lifi
./target/release/evm-quoter bench --chains eth --pair PYUSD,USDG --sizes 1000,10000,100000 --aggs cow,lifi
```

Chains are `eth`, `bsc` and `polygon`; `--rpc URL` overrides the default endpoint. Amounts are in whole tokens, and decimals are exact (`--amount 2.5`). Tokens are symbols from the chain's list in `src/chains.rs` (or addresses).

| Token (Ethereum) | Address | Decimals |
|---|---|---|
| PYUSD | `0x6c3ea9036406852006290770BEdFcAbA0e23A0e8` | 6 |
| USDG | `0xe343167631d89B6Ffc58B88d6b7fB0228795491D` | 6 |
| USDS | `0xdC035D45d973E3EC169d2276DDab16f1e407384F` | 18 |
| crvUSD | `0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E` | 18 |

These were checked on chain (symbol, decimals, supply). They have no real pools on BSC or Polygon (GeckoTerminal lists only pools of a few thousand dollars there), so those chains stay USDC/USDT.

## How it works

**Local execution.** The engine uses revm 36, the newest revm that builds with the installed Rust 1.90.

The engine injects a helper contract (`helper/Helper.sol`, compiled by `helper/compile.cjs` into `src/helper.hex`) at a synthetic address and never deploys it. The engine finds each token's balance slot by probing `balanceOf` and pins a large balance there. It then runs the helper's approvals once and pins the storage they write. A simulation is one call to the helper, which swaps against the real pool:

| Entry point | Venues |
|---|---|
| `v3Swap` | Uniswap v3, PancakeSwap v3, Sushi v3, QuickSwap v3 (Algebra), via their swap callbacks |
| `v4Swap` | Uniswap v4: `unlock` → `swap` → `sync`/`settle`/`take`. Hooks run for real. |
| `infSwap` | PancakeSwap Infinity CL: Vault `lock` → `CLPoolManager.swap` → settle/take |
| `mavSwap` | Maverick v2: `swap` with a tick limit, paid in `maverickV2SwapCallback` |
| `callSwap` | approval or pre-transfer venues: Curve, Fluid DEX, Balancer v2, DODO PMM, Sky LitePSM (`sellGem`/`buyGem`) |

Each call returns the output and input amounts as balance deltas, so it doesn't depend on a venue's return values. It also returns the gas the pool interaction used, measured with `gasleft()`, as an aggregator executor's leg would spend it. Concentrated-liquidity swaps stop 1% (in sqrt price) from the current price. A swap that a pool can't absorb near market therefore stops instead of walking every tick, and the engine treats that pool as over capacity at that size.

**State** (`src/mirror.rs`) loads lazily, fork style:
1. A simulation runs against the cache and records every account and slot it misses (read as empty).
2. The misses are fetched in one batch, and the simulation reruns. A result counts only once a run completes with no misses.

Multi-slot reads use one `eth_getProof` per account rather than one `eth_getStorageAt` per slot, because free endpoints rate-limit per batch item.

`Mirror::sync` moves the mirror to the latest block. It reads the logs of every mirrored contract since the last sync, then re-reads the cached slots of every contract that logged, plus the token contracts. The engine also records the accounts each pool's simulation reads (its group). When any member of a group logs, the whole group is re-read. This matters because a v4 hook's storage changes on every swap, but only the PoolManager emits the event.

**One block per snapshot.** PublicNode load-balances requests across nodes whose heads differ by up to 7 blocks, measured 2026-10-19. So the mirror never reads at `latest`:
- Every state read is pinned to the mirror's block number. A node that has not imported that block refuses it ("block not found"), and the read is retried.
- Each log query is batched with a header read of the head block. It is only accepted when the hash matches, because a lagging node returns the logs it has without an error.
- A group also records the token balance slots its pool reads. If one of those balances changes and no log of the pool was seen, the group is re-read at the same block. DODO prices from `balanceOf(pool) − reserve`, so a balance one swap ahead of its reserve is free input.
- A sync drops what a concurrent load inserted while it was reading, because those values come from the old block.
- If the node no longer has the mirror's block (after the host sleeps), the load fails and the quote gets a 503. The next sync re-reads everything at the new head.

**Gas price.** Local transactions pay the chain's `eth_gasPrice`, because some pools price by `tx.gasprice` (see Verification).

**Discovery** (`src/venues.rs`, cached for a day in `artifacts/pools-<chain>.json`) covers every pair of the chain's tokens (15 pairs on Ethereum):
- **v3 forks:** factory lookups at every fee tier, keeping pools with in-range liquidity.
- **Uniswap v4:** pool ids from GeckoTerminal, resolved through `PositionManager.poolKeys` and checked by hashing the key. Hookless pools at 17 common fee tiers are added when their slot0 and liquidity in PoolManager are non-zero.
- **PancakeSwap Infinity:** ids resolved through `CLPoolManager.poolIdToPoolKey`.
- **Curve:** the Curve API, every pool holding at least two of the tokens. **Balancer v2:** the Balancer API, likewise.
- **Fluid, DODO and Maverick v2:** GeckoTerminal (Maverick's tokens read from `tokenA`/`tokenB`).
- **Sky LitePSM:** the USDS wrapper (`0xA188EEC8…`), USDC↔USDS at 1:1 minus `tout` (0 on 2026-10-18).

A cache written before pools carried their token lists, or for a different token list, is rediscovered. Every indexed pool on a venue this engine can't simulate is reported as unsupported, with its TVL.

| Chain | Pools quoted (2026-10-18) | Indexed pools not simulated |
|---|---|---|
| Ethereum | 78: Uniswap v4 ×48, Uniswap v3 ×12, Curve ×9, Maverick v2 ×2, Balancer v2 ×2, PancakeSwap v3, Sushi v3, Fluid, DODO, Sky PSM | Ekubo: USDG/USDC $682k + $483k, USDC/USDT $135k, USDG/USDT $129k; a Balancer USDC/USDT pool $94k |
| BNB Chain | 19: PancakeSwap v3 ×4, Uniswap v3 ×4, v4 ×6, Infinity CL ×5 (three hooked) | PancakeSwap v2 $1.56M, Nomiswap stable $0.83M, Topaz $0.26M, Thena v3 $0.13M |
| Polygon | 13: Uniswap v3 ×4, v4 ×5, Sushi v3 ×2, QuickSwap v3, Fluid | none in the index |

The Ethereum PYUSD/USDG liquidity is all indirect:
- **PYUSD:** USDS on Uniswap v4 ($102.7M), USDC on Curve PayPool ($15.3M) and Uniswap v3, crvUSD on Curve ($1.3M).
- **USDG:** USDC on Curve ($20.0M), Uniswap v4 ($3.6M plus smaller pools), Maverick ($242k) and Ekubo; USDT on Uniswap v4 ($208k).
- **No direct PYUSD/USDG pool, and no USDS/USDG pool of size.** The best path is three hops: PYUSD → USDS (v4) → USDC (Sky PSM) → USDG.

**Ekubo is not simulated.** Its core's swap entry point has a mined zero selector and takes custom-packed calldata. Its pool keys come only from initialization events (archive data, which the free endpoint refuses) or Ekubo's own API.

**Route search** (`src/router.rs`) follows the Solana engine, generalized from two hops to three:
1. **Pruning.** Direct pools whose best rate is below another pool's marginal rate at the full amount are dropped.
2. **Multi-hop candidates.** Paths go through each intermediate and each ordered pair of intermediates (`--via`, default USDC, USDT, USDS, crvUSD on Ethereum; `--max-hops`, default 3).
   - Each hop's pools are ranked by simulated output at the full input and at one grid unit, each hop on the best previous hop's output.
   - The top 3 per hop (2 for three-hop paths) are combined and simulated end to end.
   - The best 4 two-hop and 3 three-hop paths at each size join the direct pools.
   - A path whose product of best rates can't beat the best direct marginal rate is skipped.
3. **Grid DP.** A dynamic program over a 20-step grid keeps the best path per (grid units, routes used). It never combines routes that share a pool, so no pool's liquidity counts twice.
4. **Hop-level local search.** Legs are grouped by the token they spend. A move shifts `step` of a token between the shortlisted pools spending it. The resulting output change passes down the path: one leg spending that output changes, or one opens if none exist, down to the output token. Each group's inputs always sum exactly to the outputs feeding it, and each pool appears once. Steps halve from one grid unit to 10 ppm.

The objective is the output-token amount minus every leg's simulated gas times `gas_cost`, which converts gas into output tokens using `eth_gasPrice` and a locally simulated wrapped-native/stablecoin swap. An unused leg pays nothing. Gas netting is the main difference from the Solana engine. Each leg is simulated on the mirrored state independently, as in the Solana engine, so two legs that touch the same contract (two Fluid DEXes share Fluid's liquidity layer) are not sequenced.

Gas mattered less than expected on 2026-10-18: mainnet gas was 0.054 gwei and BSC's 0.05 gwei. A 100k-gas leg cost about $0.013 on Ethereum, which is 0.13 bp at 1k. Polygon's gas was 280 gwei at POL ≈ $0.09.

If nothing fills the amount within the 1% limit, the quote retries at 10% (about 20% in price) with no marginal-rate cutoff. `--full-fill` also allows an unlimited swap, which walks every tick.

## Verification against the node

`verify` replays every pool's local simulation on the node as `eth_call` at the mirror's block, in both directions at 1k and 100k. The call carries the helper's code and every pinned slot as state overrides, and block overrides for the number and timestamp the local EVM uses. With `--wait 30` it first simulates, lets the chain move on, syncs, and only then compares, which tests the log-driven sync too.

| Chain | Result after the wait | Blocks synced |
|---|---|---|
| Ethereum | 116 of 116 simulations identical to the atomic unit, gas included | 8 |
| Polygon | 88 of 88 identical | 48 |
| BNB Chain | 106 of 110 identical | 154 |
| Ethereum, 2026-10-18, all 78 pools and every token pair (PYUSD, USDG, USDS, crvUSD, Maverick, Sky PSM included), pure-Rust precompiles | 312 of 312 identical | 12 |

On BSC, one hooked PancakeSwap Infinity pool (`0xb158d1b9`, $0.3M) differs by 20–30 bp. Its hook prices by block context in a way I couldn't pin down without its source: the node's output moves with the gas price (above about 1 gwei) and with block overrides. Every other pool fails identically on both sides (empty sides, over capacity), or matches exactly.

Two findings came out of this:
- **Two Ethereum Curve pools price by gas price.** `Curve USD-LP` (`0x706ddbd9`) and `Yield USDC-USDT` (`0x509c6c89`) read Chainlink USDC/USD and USDT/USD feeds. They pay about 49 bp differently when `tx.gasprice` is 0, which is `eth_call`'s default. At any real gas price they match the local result exactly. Simulators that quote through gas-price-0 `eth_call`s misprice them.
- **Hook storage needs grouped refreshes.** Before pool groups existed, hook storage that changed without the hook emitting logs went stale and produced mismatches.

## Engine choice: revm vs ethereumjs

`dump-state` writes the exact prestate of five swaps. `engine-bench` and `bench-ts/run.mjs` replay it with no RPC in revm and in `@ethereumjs/evm` 10.1.3, the EVM that tevm builds on. Both produced identical outputs. Times are per swap transaction, setup included, on an Apple M4:

| 10k USDC→USDT via | revm | ethereumjs | ratio |
|---|---:|---:|---:|
| Uniswap v3 0.01% | 61 µs | 10.2 ms | 169× |
| Uniswap v4 0.001% | 73 µs | 12.6 ms | 172× |
| Fluid DEX | 102 µs | 9.3 ms | 91× |
| Curve 3pool | 50 µs | 7.3 ms | 148× |
| Balancer v2 GHO/USDT/USDC | 92 µs | 9.4 ms | 102× |

A quote takes about 275–450 simulations. In revm, with rayon across 10 cores, that's 10–25 ms; in ethereumjs it would be several seconds.

## Benchmark

`bench` ran on 2026-10-18 at 00:20–01:00 (+07). For each chain it:
1. warms the mirror at every size;
2. for each case, syncs the mirror to the latest block, then requests the local quote and the aggregators' quotes at the same moment.

Δ is local minus aggregator, in bp of the aggregator's output; positive means the local quote pays more. It compares outputs before network fees:
- **CoW:** its `buyAmount` is net of its fee, so it is scaled from the fee-reduced sell amount back to the full input.
- **LI.FI:** its 0.25% fixed fee is added back the same way.

Tables in `artifacts/bench-eth-polygon.md` and `artifacts/bench-bsc.md`; full JSON next to them.

| Chain | Pair | 1k | 10k | 100k | 1M |
|---|---|---|---|---|---|
| Ethereum | USDC→USDT | CoW +0.89, LI.FI −0.12 | +0.30, −0.02 | +0.30, −0.13 | +0.30, −0.33 |
| Ethereum | USDT→USDC | +0.65, −1.20 | +0.28, −0.60 | +0.25, −0.38 | +0.23, −0.36 |
| BNB Chain | USDC→USDT | −0.33, −0.36 | −0.34, −0.43 | −0.04, −0.21 | +0.16, −0.23 |
| BNB Chain | USDT→USDC | +0.24, −0.32 | +0.24, −0.36 | −0.56, −1.20 | −0.26, −0.64 |
| Polygon | USDC→USDT | +0.30, +0.00 | +0.28, −0.00 | +0.09, −0.12 | −4.21, −4.41 |
| Polygon | USDT→USDC | +0.31, −0.08 | +0.23, −0.11 | +0.20, −0.10 | not quoted |

What the table shows:
- **Against CoW** (solver auction), the local quote paid more in 17 of 23 cases: every Ethereum case, 6 of 7 on Polygon and 3 of 8 on BSC. Medians: +0.30 bp (Ethereum), +0.23 bp (Polygon), −0.15 bp (BSC).
- **Against LI.FI** (meta-aggregator), it was 0.0–0.6 bp behind in most cases. Medians: −0.35 bp (Ethereum), −0.10 bp (Polygon), −0.36 bp (BSC).
  - LI.FI's winning tools were mostly RFQ or private-liquidity fillers: nordstern in every BSC case and 6 of 8 Ethereum cases, plus OKX, bitget, Fly, Sushi and KyberSwap on Polygon. That flow is invisible to on-chain simulation.
  - On BSC the local quote also misses about $3M of indexed liquidity on unsupported venues (table above).
- **Polygon at 1M.** Direct USDC/USDT pools can't absorb a million near market price. USDC→USDT trails by 4.2–4.4 bp. USDT→USDC can't be filled within a 20% price move, while CoW filled it at 996,058 by routing through other tokens. Two-hop routes are missing (see below).
- **Net of gas**, against each aggregator's own gas estimate, the picture is the same. Gas was too cheap on all three chains to move a split by more than a few hundredths of a bp, except at 1k on Ethereum (+2.1 and +2.3 bp vs CoW, whose fee is larger than its gas).

### PYUSD/USDG (Ethereum, 2026-10-18)

**0x and 1inch via LlamaSwap.** The LlamaSwap page was driven in the built-in browser at 0.3% slippage, and its `dexAggregatorQuote` responses were read from `window.fetch` (exact `amountReturned`). The local quote came from `serve` on a warm mirror within a second of each capture. Δ is local minus aggregator in bp, before network fees. Full table in `artifacts/bench-pyusd-usdg-llamaswap.md`.

| Pair | 1k | 10k | 100k |
|---|---|---|---|
| PYUSD→USDG vs 0x | 0.0000 (identical) | 0.0000 (identical) | +0.0000 (+0.00001 USDG) |
| USDG→PYUSD vs 0x | −0.0019 | +0.0058 | −0.0216 |
| either vs 1inch | +1.00 | +1.00 | +1.00 |

**CoW and LI.FI.** `bench --pair PYUSD,USDG`, with LI.FI's 0.25% fee added back; tables in `artifacts/bench-pyusd-usdg.md`. Gross Δ:

| Pair | Aggregator | 1k | 10k | 100k |
|---|---|---|---|---|
| PYUSD→USDG | CoW | −0.24 | +0.25 | +0.30 |
| PYUSD→USDG | LI.FI | −0.18 | −0.19 | −0.03 |
| USDG→PYUSD | CoW | −0.25 | +0.31 | −0.12 |
| USDG→PYUSD | LI.FI | −0.28 | −0.31 | −0.31 |

Net of gas, the local quote beat CoW in 5 of 6 cases (+0.32 to +1.31 bp).

**Routes.** The local route was PYUSD →(Uniswap v4 0.0005%)→ USDS →(Sky PSM)→ USDC → USDG, the last hop split between Maverick, the hooked Uniswap v4 pool and (in reverse) Curve. 0x returned the same output to the atomic unit at 1k and 10k PYUSD→USDG, so it most likely routes the same way. LI.FI's edge came from RFQ fillers (nordstern, OKX, bitget). 1inch through LlamaSwap was a constant 1.00 bp lower.

**Before three-hop routes and the PSM existed,** the best path was two hops through USDC (Curve PayPool, then Curve/v4/Maverick). It trailed CoW by 0.1–1.3 bp and LI.FI by 0.5–1.4 bp. Adding the PSM and three hops moved PYUSD→USDG at 100k from 99,988.30 to 99,993.54 (+0.52 bp) and USDG→PYUSD from 99,983.32 to 99,995.05 (+1.17 bp).

**PYUSD/USDG latency.** Warm local quotes took 15–140 ms; three-hop candidate generation costs about 2,000 simulations against roughly 450 for two hops. A cold one-shot `quote` takes 30–100 s on the free RPC, nearly all lazy loading.

### USDC/USDT latency

**Latency per quote**, on a synced mirror (server time):

| | Local | CoW | LI.FI |
|---|---|---|---|
| Ethereum | 10–22 ms | 0.6–2.1 s | 0.8–1.0 s |
| Polygon | 8–26 ms | 1.4–4.0 s | 0.5–0.8 s |
| BSC | 0.23–3.3 s | 1.5–2.8 s | 0.6–0.9 s |

- **Sync** to the latest block takes 1.4–2.6 s; it's about 0.2 s when there is no new block.
- **BSC is slower** because its 0.45 s blocks change pool state between quotes, so simulations miss the cache and load new slots.
- **Cold first quotes** (lazy-loading every pool) take 3.5–26 s, depending on how hard the free endpoint is throttling.

**Aggregators that couldn't be compared from this machine:**
- **ParaSwap (Velora):** its keyless tier answered about 10 requests (including RFQ `Native` routes), then returned 429 for over an hour. The adapter is in place (`--aggs paraswap,paraswap-norfq`) for a rerun.
- **KyberSwap:** "Access from your country is currently restricted"; not worked around.
- **Odos:** every endpoint returned Cloudflare error 1033.
- **OpenOcean:** serves a Cloudflare bot challenge.
- **1inch and 0x:** need API keys, so they were skipped.

## Quote server

`serve` runs one chain as a long-lived service on 127.0.0.1:8795 (`GET /quote?input=USDC&output=USDT&amount=…[&maxSplits=…]`, `GET /health`).

**Startup.** It warms the mirror at 1M, 100k and 1k in both directions before taking requests: USDC/USDT, plus each pair in `--warm-pairs "PYUSD,USDG;USDC,USDS"`. On Ethereum with PYUSD/USDG this took about 2 minutes.

**Sync.** A thread follows websocket `newHeads`. PublicNode's socket sometimes holds heads back for a minute or more, so a read that waits longer than two block times syncs anyway. On socket errors it polls, and every 5 minutes it re-reads everything.

**Caching.** A per-pool cache reuses simulations until an account in the pool's group, or one of the token balances the pool reads, actually changes. Time-dependent pools are re-simulated every block: Fluid, Curve, Balancer, hooked v4 and Infinity pools, and Algebra. A simulation whose state could not be read is not cached, and the quote it belongs to fails (503) instead of routing around the pool.

**Health checks.** Requests are refused (503) while the mirror is more than 8 blocks behind the head or the last sync is older than 30 s. RPC requests are capped at 2 in flight, because bursts drew 429s that stalled sync.

**Quote checks.** A quote is also refused (503) when:
- a sync wrote while it ran, twice in a row (its legs would mix blocks);
- a state read it needed failed;
- its rate is more than `--max-jump-bps` (default 200) from the median of the pair's quotes of a similar size (½× to 2×, same `maxSplits`) in the last 10 minutes.

The last check also triggers a full re-read. If the deviation holds after that re-read, the quote is served and becomes the pair's new reference. `/health` counts these refusals (`refusedMoved`, `refusedRead`, `refusedOutlier`), plus balance-witness re-reads (`witnessRereads`), values dropped during syncs and retried log queries.

**2026-10-19 misquote.** A dry run got 61,498 PYUSD → 69,998 USDG and 39,999 USDG → 48,502 PYUSD for about 50 s (blocks 26223246–26223250).
- **Trigger:** after a websocket reset, a pinned read was refused as archive. The old mirror then read at `latest` for the rest of the process, so the reads of one sync went to nodes up to 7 blocks apart.
- **The swap:** at block 26223246 someone sold 8,505.396098 USDC into DODO PMM USDC/USDT (`0x04571c32`). Both quotes exceeded the market by that amount, less fees.
- **The state:** the mirror held DODO's USDC balance from after that swap and its reserve from before it. Two things kept the pair apart. A pool was only re-read when its own log, or a log of a group that happened to share an account with it, was seen. And the simulation cache ignored token balances.
- **Live reproduction:** `cargo test --release live_ -- --ignored --nocapture` rebuilds that state from a recent DODO swap. On block 26223558 (16,927.70 USDC sold into DODO) it added +16,925.64 USDG to 61,498 PYUSD → USDG and +16,926.62 PYUSD to 39,999 USDG → PYUSD, through a slice of under 0.1% routed into DODO. The fixed sync, with DODO's log hidden, re-read DODO through the balance witness and matched a full re-read exactly. The guard refused the mixed-state quote at +2,752 bp.

**Soak test, Ethereum.** 5 minutes at about 5 quotes per second, random sizes 1k–1M in random directions:
- all 1,485 requests returned 200;
- server time p50 16 ms, p90 24 ms, p99 84 ms, max 234 ms;
- 18 syncs of about 2.2 s, a mirror at most 2 blocks behind the head, and no unhealthy sample.

## Windows build

The crate builds for Windows with Rust's GNU toolchain, with no Visual Studio, MSVC or license acceptance. `scripts/build-windows.ps1` checks the setup and builds `target\release\evm-quoter.exe`. On the VPS, pull arbbotstable and run it from `quoters\evm`.

**One-time setup on the VPS** (PowerShell):
```powershell
Invoke-WebRequest https://win.rustup.rs/x86_64 -OutFile rustup-init.exe
.\rustup-init.exe -y --default-host x86_64-pc-windows-gnu --default-toolchain 1.90.0 --profile minimal
winget install MSYS2.MSYS2
C:\msys64\usr\bin\bash.exe -lc "pacman -S --needed --noconfirm mingw-w64-x86_64-gcc"
```

**Then, in the unzipped folder:**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
.\target\release\evm-quoter.exe serve --chain eth --port 8795 --warm-pairs "PYUSD,USDG"
```

- **Why gcc is needed.** revm's C crypto backends (`c-kzg`, `blst`, `secp256k1`) are turned off and replaced by revm's pure-Rust fallbacks (k256, arkworks). After the switch, all 312 Ethereum simulations still matched the node exactly. `ring`, rustls' crypto, is then the only dependency with C code, which is why MSYS2's gcc is needed.
- **Not built or run on Windows here.** This Mac has no Windows target installed, and you chose to build on the VPS. Paths are relative and `/`-separated, which Windows accepts. The server binds 127.0.0.1 by default.
- **Port 8788** is taken by another local quote server on the Mac; evm-quoter uses 8795.

## Gaps

- **RFQ market makers.** Their flow fills off chain (LI.FI's nordstern, OKX's PMM, ParaSwap's `Native`). On-chain simulation can't see it, and it's most of LI.FI's 0.1–0.6 bp edge.
- **Routes longer than three hops,** and intermediates outside the chain's token list (WETH, DAI, USDe). On Polygon, USDT→USDC at 1M still can't be filled through USDC/USDT pools alone, because Polygon has no other graph tokens yet.
- **Ekubo** (Ethereum USDG/USDC $1.17M, USDG/USDT $129k): see Discovery.
- **More venues:**
  - BSC: PancakeSwap v2, PancakeSwap StableSwap, Nomiswap, Thena, Topaz, Wombat;
  - Ethereum: Ekubo, Balancer v3 boosted pools (ERC-4626 buffers), Curve metapools (`exchange_underlying`), Maverick;
  - everywhere: Uniswap v2 forks.
- **More chains.** Each needs a `Chain` entry in `src/chains.rs`, which holds tokens, factories, managers, index names and its RPC. Arbitrum and Base are the obvious next ones.
- **Snapshot consistency.** Reads are pinned on every chain; the `latest` fallback is gone. On BSC the free node's state window (~128 blocks, under a minute) can be shorter than a throttled full re-read. Quotes are then refused (503) until a sync completes inside the window, instead of being served from slots a block apart. Not re-measured on BSC since this change. Pinning is by number, not hash, because Polygon's PublicNode refuses `eth_getLogs` by block hash. A reorg can therefore still mix forks for a block.
- **Sync coverage.** Accounts are refreshed when they or a member of their group log, or when a token balance their pool reads moves. State that changes with no log and no balance move (an oracle-only update) is still caught only by the 5-minute full re-read. The hooked Infinity pool on BSC still differs from the node.
- **Gas model.**
  - Leg gas is the pool interaction measured inside the helper.
  - The route's base cost is a flat 60k gas.
  - A real executor adds approvals, transfers between legs and calldata.
  - No transaction is built or sent, and quotes are not checked on chain.
- **Free-endpoint limits** shape the timings above. On PublicNode:
  - Ethereum `eth_getLogs` returns 403 with more than 8 addresses;
  - BSC serves `eth_getProof` only at the head;
  - batches of 100 items draw 429s under load.

  A paid RPC (or a local node) would shorten cold starts and BSC quotes.

## Layout

| Path | Role |
|---|---|
| `src/mirror.rs` | lazy state, batched/proof reads, log-driven sync, pool groups, versions |
| `src/evm.rs` | one revm transaction against a mirror view |
| `src/engine.rs` | helper funding and approvals, simulation, simulation cache, native price |
| `src/venues.rs` | pool kinds, helper calldata, discovery |
| `src/router.rs` | multi-hop candidates, gas-netted pruning, route DP and hop-level search |
| `src/optimizer.rs` | search parameters |
| `src/quote.rs` | setup and one quote |
| `src/verify.rs` | node comparison |
| `src/aggregators.rs`, `src/bench.rs` | ParaSwap, CoW, LI.FI clients; benchmark |
| `src/server.rs` | websocket sync and HTTP service |
| `src/enginebench.rs`, `bench-ts/` | revm and ethereumjs timing |
| `helper/` | helper contract source and solc-js build |
