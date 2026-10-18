# Solana local quoter

Local exact-input quotes and signable swap transactions for Solana stablecoin pairs, computed by running the
deployed DEX programs in LiteSVM against account state mirrored from RPC. No aggregator API, no wallet key.

Copied on 2026-10-18 from `solanaarbitrage` (its `src/quote/`, `src/local-quote.ts`, `src/quote-server.ts`, the
market mirror and venue models, and the quote tests). `src/config.ts` keeps only the RPC helpers; the trading bot's
flash-loan, Jito and provider settings are not part of this package. Mentions below of "the bot", `roundtrip` or
`ROUNDTRIP_LOCAL_QUOTES` refer to that repository.

```sh
npm install
cp .env.example .env        # SOLANA_RPC_URL, HELIUS_RPC_URL (for pool discovery scans)
npm run quote -- --input USDG --output USDC --amount 10000
npm run quote-server        # QUOTE_PAIRS=USDG/USDC,PYUSD/USDC,PYUSD/USDG,USDT/USDC
npm test                    # unit tests (the swap test uses the committed artifacts/test/mock_router.so)
```

LiteSVM 1.5.0 ships macOS and Linux binaries only. On a Windows VPS, run it under WSL1 or on Linux; see
"Running the quote server on a Windows VPS" below.

## Local quote engine

`npm run quote` quotes **100,000 USDC → USDT** by default. It covers pools on nine venues: Orca Whirlpool, Raydium CLMM, PancakeSwap, Manifest, GoonFi V2, Deriverse, JupLend AMM, BisonFi and Kipseli. It uses them directly and in two-hop routes through an intermediate token (SOL by default). It verifies each pool's program, account layout and token pair on chain, and fetches the actual pool, vault, order-book, configuration and tick-array accounts over RPC. The deployed DEX programs execute inside LiteSVM with synthetic funds. This path needs no wallet key, executor build or Jupiter/OKX quote. Raydium and PancakeSwap initialized tick arrays are derived from their on-chain pool and extension bitmaps.

| Venue | Discovery | Swap layout source |
|---|---|---|
| Orca Whirlpool | Orca pool index | Orca client |
| Raydium CLMM | Raydium pool index | Raydium SDK |
| PancakeSwap | program-account scan | Raydium CLMM fork: same layout, instructions and PDA seeds |
| Manifest | program-account scan | Manifest source (CKS-Systems/manifest); global orders included when the global accounts exist |
| Deriverse | PDAs of the two registered token ids (no scan) | `@deriverse/kit` 1.0.76 |
| GoonFi V2 | program-account scan | Jupiter-routed swaps observed on chain (no published spec); quoted as a one-step Jupiter v6 `route` (see below) |
| JupLend AMM | program-account scan | swaps observed on chain; liquidity-layer accounts are Jupiter Lend PDAs |
| BisonFi | program-account scan | Jupiter-routed swaps observed on chain (tag 2, 13 accounts) |
| Kipseli | program-account scan | Jupiter-routed swaps observed on chain; quoted as a one-step Jupiter v6 `route` (see below) |

Program-account scans use `HELIUS_RPC_URL` when it is set, otherwise `SOLANA_RPC_URL`. Many RPC plans (Chainstack's included) refuse `getProgramAccounts`; that venue's discovery failure then appears as a warning and the other venues still quote. `--pools` skips discovery entirely.

Observed layouts were checked by simulating them in LiteSVM against Jupiter's quote for the same pool. Manifest, Deriverse and JupLend matched to the atomic unit. GoonFi swaps also read a validator vote account, which Jito bundles cannot lock.

**GoonFi V2** prices by caller and by slot (found by replaying swaps and patching account fields in LiteSVM, 2026-10-17):

- It reads the instructions sysvar and pays swaps whose top-level program is Jupiter more than direct calls: 0.05–0.1 bp at fresh prices, more at stale ones. Its legs are therefore quoted as a one-step Jupiter v6 `route` (`Swap::GoonFiV2 { quote_in }` is variant 151), like Kipseli's. Its price does not depend on the route's slippage or quoted amount. Simulated this way, pool EEUNhH matched Jupiter's GoonFi-only quote (`dexes=GoonFi V2`) to the atomic unit at 1k, 10k and 50k in both directions, in all 12 `npm run quote` runs. In one of them, the Jupiter quote taken just before was itself 0.75 bp lower (see the next points), and the one just after matched. At 100k neither side fills, because its vaults held ~84k USDT and ~64k USDC.
- The price account (`4V7iPf…`) holds bid, ask, a u32 update slot @16, a scale and a millisecond timestamp. Prices hold for 50 slots after an update, then lose ~0.75 bp per slot, and swaps fail with custom error 0x15 once the update is more than 100 slots old.
- A swap gets ~0.75 bp less in the slot of the price update or of the pool's latest trade (the later of @32 and @40), and a direct call ~2 bp less. A transaction built from a snapshot lands later than both, so the quote clock moves past those slots (see below). Before that adjustment, 3 of ~22 live comparisons were 0.75 bp below Jupiter. Jupiter's own quote occasionally shows the same penalty.

BisonFi and Kipseli only reach USDC/USDT through SOL. Their behaviour was found by replaying observed swaps (2026-10-17):

- **Both are oracle pools that read the vote account `J1to1…`**, like GoonFi, so Jito bundles refuse them.
- **BisonFi** called directly priced within 0–3 bp of Jupiter's per-venue quote. The gap is state moving between slots; calling it with Jupiter's program id changes nothing. It returns a zero fill rather than an error in two cases: its oracle is outside `[valid-from, valid-from + 2]` slots (field @72), or the cluster's last-restart-slot sysvar is unset. LiteSVM defaults that sysvar to 0, so the quote market mirrors it from chain. A CPI from a program outside BisonFi's allowlist fails with custom error 0x35.
- **Kipseli** pays swaps whose top-level program is Jupiter about 6 bp more than direct calls. It reads the instructions sysvar, checks a `["ban", user]` PDA for the token authority and the payer, and refuses Jupiter routes with less than ~10 bp slippage tolerance (`NotSupport`). Its price does not depend on that tolerance.
- **Kipseli legs are therefore quoted as a one-step Jupiter v6 `route`** (`Swap::Kipseli` is variant 181), as GoonFi's are. Jupiter's deployed program runs and CPIs into Kipseli. This is a transaction anyone can send, and it matched Jupiter's Kipseli quote to the atomic unit. Kipseli prices decay within ~3 slots of an update and fail (`PriceExpired`) a few slots later.

Two of BisonFi's pools are USDC/USDT, and several others are SOL pairs. Jupiter routes none of them, and they zero-fill or fail when simulated.

Because oracle pools only price close to their update slot, the snapshot re-reads oracle pool accounts last, in one request. It then pins the LiteSVM clock to that request's slot, or to a later BisonFi valid-from slot within `--max-slot-spread`, and shifts the timestamp by 400 ms per slot. Without oracle pools the clock is pinned to the newest account batch. If a GoonFi pool's price update or latest trade is at or after that slot, the clock moves to the slot after it, within `--max-slot-spread`. The report's `snapshot.quoteSlot` is the slot quotes run at.

```sh
npm run quote
npm run quote -- --amount 100000 --json
npm run quote -- --amount 100000 --steps 100 --save artifacts/quote-100000.json
npm run quote -- --max-splits 1                 # one route only (a pool or a two-hop path)
npm run quote -- --input USDT --output USDC     # reverse direction
npm run quote -- --via none                     # direct pools only (faster)
npm run quote -- --amount 1000 --pools whirlpool:4fuUiYxTQ6QCrdSq9ouBYcTM7bqSwYTSyLueGZLTy4T4,kipseli:7yD7gfXv49SFxhx9uTc11dEd7Nj98rGWeYMdZJgheZbG,bisonfi:8FnX3xo2yYw3EUE6w3nQA4GfXGS9wpK6oj3veJpbFzLo,bisonfi:FJnaiidSLXFweWkgbinxEHRykVHsnkzDcYbNDR3RF5LN
```

The reusable engine is `src/quote/engine.ts` (`quoteExactIn`). A route is one direct pool or, with `via`, two pools joined by an intermediate token. A two-hop route simulates its second pool on the first pool's simulated output, so every leg is its own simulation.

Two-hop candidates are bounded. Each hop's pools are ranked by simulated output at the full input and at one grid unit; the second hop is ranked on the best first hop's output. The top 3 per hop at each size are paired, and every pairing is simulated end to end. The best `--max-two-hop-routes` (default 4) at each of the two sizes join the direct pools in the search.

The grid search and its refinement never combine routes that share a pool, so no pool's liquidity counts twice. A last local search then lets a two-hop path split within a hop, as Jupiter does. Several first-hop pools can feed one second-hop pool (Jupiter's BisonFi+Kipseli-only route has been Kipseli 96% + BisonFi 4% → BisonFi FJnaii), or one first hop can feed several second hops. This search groups legs by the token they spend:

- It moves amounts between the pools spending the input: direct pools and the shortlisted first hops.
- It moves an intermediate's amount between that intermediate's shortlisted second hops.
- When a move changes a first hop's simulated output, the difference goes to one second-hop leg of that intermediate. Second-hop inputs therefore always sum exactly to the first hops' outputs.

Steps halve from one grid unit to ~1 ppm of the input, and a move may close a leg. Each pool still appears once in the route plan, simulated once at its total input. In live runs it took 46–167 simulations (`search.refinement.hopSplit`). It does not run with `--max-splits 1`.

The engine samples each route at different total allocations and uses dynamic programming to choose the highest output on a bounded grid. A local search then moves amounts below the grid resolution between routes (halving the step from half a grid unit down to ~1 ppm of the input) while the simulated total rises, and can add a pool that only pays for a small amount. Defaults: 20 steps, at most 4 pools splitting the input (and, separately, each intermediate amount), and at most 4,096 simulations (each hop counts, the hop-level search included). With the refinement, `--steps 100` rarely beats the default grid. Once a pool fails because it ran out of liquidity or tick arrays, larger amounts of that pool are not simulated (`search.skippedOverCapacity`). Each chosen pool appears once, its allocated amount is simulated as one swap, and all allocated inputs sum exactly to the original amount, including indivisible remainders. The result is a local optimum, not a claim of a globally optimal route. Each extra leg costs an extra swap instruction on chain, which this quote does not price; `--max-splits 2` keeps routes short. Failed or partial fills cannot contribute quoted liquidity. Search-budget exhaustion is reported as `search.truncated`.

Mint checks, pool discovery and program downloads run concurrently, and pools without active liquidity are dropped before their vaults and tick arrays are fetched. Deployed program binaries (the Whirlpool program data alone is ~10 MB) are cached in `artifacts/programs`, named by program id and last deploy slot. Every run reads the current deploy slot first, so an upgraded program is downloaded again. `--program-cache off` always downloads. The snapshot is fetched in a single pass (~2.5 MB for USDC/USDT; GoonFi's program state and Deriverse's maps are ~0.7–0.8 MB each). It is refreshed only if a pool price moves into new tick arrays mid-load or the slot spread is too wide. A warm 100,000 USDC quote takes about 3.5 s with `--via none` and 5–9 s with the default `--via SOL`, most of it RPC round trips.

Before the grid search, routes that provably cannot carry input are dropped. Every route's output is concave in its input, since prices only worsen with size. So a route whose best rate is below another route's marginal rate at the full amount is dominated: moving any of its input to the other route pays more. The engine:

- reads each route's best rate as its rate at a small probe (1/10,000 of the amount, at least one token or 1% of the amount);
- reads the marginal rate at the full amount as the slope over the last 0.1%, for the three routes with the best probe rates (and the best route at the full amount);
- prunes with a 0.5 bp margin, and skips pruning when the probe is too small for atomic rounding to stay inside that margin.

Two-hop routes are bounded by the product of their hops' best rates, so an intermediate whose best path cannot compete is skipped before its legs are ranked. On identical snapshots (28 cases, 100 to 1M, both directions), pruning returned exactly the same output as the unpruned search with 2–5× fewer simulations. The result's `search.pruned` counts what was dropped. Options:

- `prune: false` turns pruning off.
- `diagnostics: false` skips the full-amount simulation of pools pruning excludes (they are then missing from `singlePoolQuotes`).
- `precisionPpm` sets the smallest amount the local searches move (default 1 ppm of the input).

A simulation costs 0.1–0.7 ms. LiteSVM's VM time is most of it. Three shared code paths keep the rest small:

- The CLMM models memoize tick-array PDA derivations, which cost more than the swap.
- `Mirror.quote` reuses a compiled transaction per pool and account list and patches only the instruction data.
- `Mirror.quote` copies only the two token accounts it reads from LiteSVM's results; Deriverse alone writes ~800 KB per swap.

The SOL legs add about 80 pools and 500 accounts. To bound that, intermediate-token legs keep only the top 8 Orca/Raydium index pools by TVL/liquidity; program-account scans are not capped.

The JSON report contains atomic mint amounts, human amounts, `quote.outAmount`, a slippage threshold, and pool addresses and allocations. Each `routePlan` leg carries `inputMint`, `outputMint`, `path` and `hop`. A path is one direct pool, or every pool through one intermediate token, and its legs run in `hop` order. Several legs with the same `path` and `hop` split that hop: hop-1 legs spend the input, and the hop-2 legs' `inAmount`s sum exactly to the hop-1 legs' `outAmount`s. `shareBps` is the share of the input the path carries. `hopShareBps` is the leg's share of its hop's input, like Jupiter's `percent`. The CLI prints a split hop as `BisonFi 8FnX3x 62.30% + Kipseli 7yD7gf 37.69% → BisonFi FJnaii`. The engine id for this format is `local-two-hop-v2`. The report also has the best single route (`bestSinglePool`, which may be a two-hop path), split improvement, `twoHopQuotes` (every simulated pairing), `hopQuotes` (each leg pool's own quote), discovery warnings, timing, and the account snapshot's slot range. Network fees are excluded; DEX swap fees and price impact are included by the program simulations. Snapshots are read in RPC batches at `processed` commitment and are **not guaranteed to come from one bank**. `--max-slot-spread` defaults to 32 and rejects older/inconsistent batches. The one-shot snapshot stays fixed during the local search and opens no account subscriptions.

For later comparison with a meta-aggregator, request exactly `100000000000` atomic USDC, compare output amounts before network fees, match slippage when comparing minimum outputs, and capture quotes close together in time. On 2026-10-17 the local quote was within ±0.06 USDT of Jupiter's best route at 10k and 100k in both directions, and above it at 1M, because the split search uses more pool depth. With two-hop routes added, the same comparison stayed within ±0.07 USDT. At that time Jupiter's best routes were direct and its BisonFi+Kipseli route was ~1–2 bp worse at 1k–10k (and ~50 bp worse at 100k), so the default quote's routes were direct too. With weaker direct pools (the `--pools` example above), BisonFi→BisonFi and Kipseli→Raydium paths entered the route plan. After hop-level splits were added the same day, the default quote's routes were still direct, and it stayed within 0.02 USDT of Jupiter at 10k (−0.015 and −0.006) and at 100k (+0.011). At 100k with only the three BisonFi/Kipseli pools of that example, the local quote split the first hop (BisonFi 62% + Kipseli 38% → FJnaii). That matched a 1%-grid brute-force scan of the same snapshot, and pair routes alone paid ~43 USDT less. Jupiter's split of those pools moved between 50/50 and 60/40 within minutes, as Kipseli's price changes every slot. GoonFi legs were then routed through Jupiter, and a benchmark ran the same day: 1k–250k, both directions, 2 rounds, quotes taken while each snapshot loaded. The local quote was at or above Jupiter's best route at every size. Jupiter's median delta ranged from −0.00 to −0.10 bp, a mean of −0.02 bp; in an earlier run before this change it was +0.006 bp, with Jupiter up to 0.04 bp ahead at USDT→USDC 10k–50k. The local quote was best in 17 of 24 rounds. OKX was ahead by at most 0.02 bp at USDT→USDC 10k–50k. DFlow's keyless endpoint was 0.03–0.22 bp behind from 25k up. Its GoonFi allocations now track Jupiter's, for example 100% at 1k and 10k USDC→USDT, and 31.25% at 250k for both. There are no routes longer than two hops, no RFQ liquidity, and no venues beyond the nine above (Jupiter also routes through e.g. Byreal, HumidiFi and SolFi). Combined split transactions are not built or verified by this command; `transactionVerified` is explicitly `false`.

`priceImpactBps` compares the route with the best rate any route pays for 1/10,000 of the input. An exact input is always spent in full, so an amount larger than the pools can absorb is still quoted, by sending the remainder to pools with nothing left near market price. Above 100 bps the command prints a warning.

To skip discovery, pass `--pools VENUE:ADDRESS,...` with VENUE one of `whirlpool`, `raydium-clmm`, `pancakeswap`, `manifest`, `goonfi`, `deriverse`, `juplend`, `bisonfi`, `kipseli`. Pools of an input/intermediate or intermediate/output pair form two-hop routes. The engine still verifies every supplied pool and fetches current state from RPC. Discovery failures and incomplete index pages appear in the report. Exit codes: 0 = quote produced, 1 = initialization/configuration error, 2 = no allocation could fill the entire input.

## Local quote server

`npm run quote-server` runs the local quote engine as a long-lived service, for example on a VPS beside the bot, so routine pricing needs no aggregator API. It takes one consistent RPC snapshot, the same as `npm run quote`. After that, every pool, vault, tick-array and order-book account streams into LiteSVM over websockets. The clock follows the chain, and accounts too large to stream are polled. A quote then costs only the local search. The service never holds a key and never sends a transaction.

```sh
npm run quote-server                                   # USDC/USDT, direct pools, 127.0.0.1:8787
QUOTE_PAIRS=USDG/USDC,PYUSD/USDC,PYUSD/USDG,USDT/USDC QUOTE_VIA=USDC npm run quote-server   # four pairs, one process
curl "http://127.0.0.1:8787/quote?inputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&outputMint=Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB&amount=10000000000"
curl "http://127.0.0.1:8787/swap-instructions?inputMint=...&outputMint=...&amount=500000000&taker=WALLET_PUBLIC_KEY&simulate=both"
curl http://127.0.0.1:8787/health
```

`GET /quote` takes `inputMint`, `outputMint` (a served pair, either direction), `amount` (atomic), and optionally `slippageBps` (5), `maxSplits` (4) and `onlyDirectRoutes=true`. The response has the fields DFlow's and Jupiter's quote responses share:

- `inAmount`, `outAmount`, `otherAmountThreshold`, `slippageBps`, `priceImpactPct`
- `routePlan` legs with `venue`, `label`, `marketKey`, mints, amounts and `percent` (the leg's share of its hop), plus `inputSharePct`, `path` and `hop`
- `contextSlot` (the slot the quote was simulated at), `chainSlot`, `simulations`, `cache` hits and misses, and `timeTaken` in seconds

It answers 503 while the mirrored state is not current, 422 when no route fills the amount, and 400 for bad input. `GET /health` returns 200 or 503 with `reasons`, stream lag, clock age, pool and account counts, cache totals, p50/p99 latency and RSS. A stats line is logged as JSON every minute.

**How it stays fast.** Each pool's simulations are cached by (pool, input mint, amount). An entry stays valid until an account that pool reads actually changes. Websocket writes that leave an account identical no longer count as changes; this matches the Geyser path. Token mints are ignored, because supply changes do not move prices. Time also invalidates some entries:

- GoonFi, BisonFi and Kipseli (which price by slot) re-simulate every slot.
- Manifest, JupLend, Deriverse and adaptive-fee Whirlpools re-simulate at most every 8 slots, because orders expire and interest and volatility fees move with time.
- Other CLMMs are never re-simulated for time alone.

Before each search the clock is set to the chain's slot. It moves past GoonFi's price-update and last-trade slots when needed, as in a snapshot. When a pool account changes, its model is rebuilt. If the pool now needs accounts not yet mirrored, such as new tick arrays, it sits out (`syncing` in `/health`) until they arrive.

**How it stays reliable.**

- **Stale state is never quoted.** Quotes are refused while the account stream lags more than `QUOTE_MAX_LAG_SLOTS` (8), is stalled, or the clock is older than `QUOTE_MAX_CLOCK_AGE_MS` (3 s). A stall is detected by a canary subscription on the same socket; while stalled, the mirror falls back to HTTP polling.
- **Missed updates are corrected.** Every account is re-read over HTTP every `QUOTE_RESYNC_MS` (60 s), because websocket notifications can be dropped silently.
- **New pools are picked up.** Pools are rediscovered every `QUOTE_REDISCOVER_MINUTES` (15). A new market is built and swapped in only when the pool set changed.
- **A stuck market is replaced.** A market unhealthy for 30 s is rebuilt from a fresh snapshot.
- **Process failures are left to the supervisor.** Use systemd or another supervisor, below.

**Defaults.** These are chosen for a small footprint and measured on identical snapshots against the exhaustive search:

- **Direct pools only.** Two-hop routes through SOL changed outputs by at most 0.003 bp from 1k up, and by up to 0.04 bp (0.0004 USDT) at 100 USDC, while tripling the mirrored accounts (`QUOTE_VIA=SOL` turns them on).
- **10 grid steps and a 100 ppm refinement floor.** These cost under 0.005 bp; a 1,000 ppm floor lost up to 0.09 bp.

On 2026-10-17 the service started in 2–8 s with 27 pools, 248 accounts (232 websocket subscriptions, 3–5 polled) and 180–550 MB RSS. Measured against Jupiter's best route, requested at the same time:

- Equal to Jupiter at 1k–10k in both directions.
- Ahead of Jupiter from 50k up: by 0.01–0.04 bp at 50k–100k, 0.04–0.11 bp at 250k–1M USDC→USDT, and 2.2 bp at 1M USDT→USDC.
- Ahead of or equal to Jupiter in 34 of 42 requests.
- OKX was 0.01–0.04 bp ahead at most sizes, and 6.7 bp ahead at 1M USDT→USDC, through venues this engine does not model.

A 12-minute soak at ~3.6 quotes/s ran random sizes from 100 to 1M in random directions:

- All 2,602 quotes were answered with HTTP 200.
- Server time was p50 64 ms, p90 106 ms and p99 240 ms, and roughly the same at every size. 40% of simulations came from the cache.
- Every one of 144 health samples was healthy; the stream lagged at most 2 slots.
- In 96 comparisons with Jupiter at 10k and 100k, the median difference was 0.002 bp in the local quote's favour, and Jupiter was ahead by more than 0.005 bp twice.

A one-shot `npm run quote` takes 4–9 s by comparison, most of it RPC. The soak's worst request, 1.6 s, coincided with a rediscovery that rebuilt the market needlessly. Rediscovery now compares discovered candidates with the candidates the market was built from, so it rebuilds only when that list actually changes.

| Variable | Default | |
|---|---|---|
| `QUOTE_PAIRS` | `USDC/USDT` | comma-separated pairs, each served in both directions; `QUOTE_PAIR` also works |
| `QUOTE_VIA` | `none` | intermediate tokens for two-hop routes, e.g. `SOL`, or a served stablecoin such as `USDC` |
| `QUOTE_SERVER_HOST` / `QUOTE_SERVER_PORT` | `127.0.0.1` / `8787` | |
| `QUOTE_SERVER_TOKEN` | | required (16+ characters) to listen beyond loopback; send `Authorization: Bearer TOKEN` |
| `QUOTE_STEPS` / `QUOTE_PRECISION_PPM` / `QUOTE_MAX_SPLITS` / `QUOTE_MAX_QUOTES` | `10` / `100` / `4` / `4096` | search bounds |
| `QUOTE_MAX_LAG_SLOTS` / `QUOTE_MAX_CLOCK_AGE_MS` | `8` / `3000` | health limits |
| `QUOTE_POLL_MS` / `QUOTE_RESYNC_MS` / `QUOTE_REDISCOVER_MINUTES` | `1000` / `60000` / `15` | polling of large accounts, full re-read, rediscovery |

RPC use:

- `SOLANA_RPC_URL`, with its backups, serves HTTP.
- Websockets come from `SOLANA_WS_URL`, else Helius.
- `HELIUS_RPC_URL` serves the program-account scans at start-up and at each rediscovery. Pairs are discovered one after another, and a pair whose scan failed is retried 2, 4 and 8 s later. Helius rate-limits bursts of scans, and a missing scan drops that venue's pools. A server that still starts with a source missing rediscovers after one minute instead of 15. A rediscovery with a failed source never replaces the running market.
- Steady load is one websocket subscription per account, a `getMultipleAccounts` call for the polled accounts every second, and 3 calls per minute for the resync.

On a VPS, run it under systemd. Keep the bot's `.env` in the working directory, and run `npm install` there once.

```ini
# /etc/systemd/system/quote-server.service
[Unit]
Description=Local Solana quote server
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/solanaarbitrage
ExecStart=/usr/bin/npx tsx src/quote-server.ts
Restart=always
RestartSec=5
MemoryMax=1500M
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl enable --now quote-server
journalctl -u quote-server -f          # JSON lines: ready, stats, rebuilt, maintenance-error
```

**Several pairs, one process.** `QUOTE_PAIRS` serves any number of pairs from one mirror and one LiteSVM, and only the programs of venues the pairs use are loaded. Measured on 2026-10-18 after 20 quotes each:

| | Pools | Accounts | RSS |
|---|---|---|---|
| USDG/USDC alone | 4 | 53 | 306 MB |
| PYUSD/USDC alone | 8 | 87 | 279 MB |
| PYUSD/USDG alone | 1 | 22 | 234 MB |
| USDT/USDC alone | 27 | 248 | 625 MB |
| All four in one process | 36 | 325 | 552 MB |
| All four, `QUOTE_VIA=USDC` | 40 | 350 | 581 MB |

Most of a lone process is fixed cost (Node, LiteSVM, programs), so one process for all pairs is a third of four separate ones. With `QUOTE_VIA=USDC`, PYUSD/USDG also routes through the USDC pools the other pairs already mirror. Its own Manifest market cannot fill 100k; through USDC, 100k PYUSD→USDG fills at 99,999.6 USDG. Two-hop builds leave dust in USDC, a token the wallet already holds (see Swap instructions).

Searches run on Node's one thread, one at a time. Requests arriving during a search wait for it, so latency grows with concurrency. Size the VPS for ~1 GB RAM per process: a rebuild briefly holds two markets. The roundtrip bot can price its DFlow legs on this service with `ROUNDTRIP_LOCAL_QUOTES` (see [Cross-aggregator round trip](#cross-aggregator-round-trip)); its Jupiter and OKX legs are unchanged.

## Swap instructions

`GET /swap-instructions` (or `npm run quote -- --taker PUBLIC_KEY`) turns the best route into the taker's own transaction, ready for the taker to sign and send. The server never holds or asks for a private key, never signs and never sends; it uses only the taker's public key.

```sh
curl "http://127.0.0.1:8787/swap-instructions?inputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&outputMint=2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH&amount=500000000&slippageBps=5&taker=WALLET&simulate=both"
npm run quote -- --amount 500 --input USDC --output USDG --via none --taker WALLET --simulate both
```

Parameters: the `/quote` ones, plus `taker` (an on-curve wallet public key), and optionally:

- `txVersion`: `auto` (default; v0 when it fits, else v1), `v0` or `v1`.
- `lookupTables`: comma-separated address lookup tables, v0 only.
- `computeUnitPrice`: micro-lamports per compute unit (default 0).
- `simulate`: `local` (default), `rpc`, `both` or `none`.

The response has:

- `computeBudgetInstructions`, `setupInstructions` (idempotent creation of the output and any intermediate token accounts) and `swapInstructions` (one per leg), each `{ programId, accounts, data }` like Jupiter's `/swap-instructions`.
- `addressLookupTableAddresses`.
- `transaction`: the unsigned transaction (`base64`, `version`, `bytes`, `accountLocks`, `recentBlockhash`, `lastValidBlockHeight`), with the taker as fee payer and only signer.
- `legs`: each with `inAmount`, `outAmount`, `quotedOutAmount`, `minOutAmount` and `bundleSafe`.
- `outAmount`, `minOutAmount`, `quotedOutAmount`, `withoutGoonFiOutAmount`, `intermediateDust` and `bundleSafe`.
- `simulation.local` / `simulation.rpc`.

The input token account must already hold the input. A v1 transaction carries its compute limit (and a 64 MiB loaded-data limit, which v1 does not default to) in its header, so `computeBudgetInstructions` are for clients composing their own v0 transaction.

How a leg is built:

- **The same venue models the quote simulated.** GoonFi V2 and Kipseli legs are the one-step Jupiter v6 `route` instructions, which anyone can send.
- **Each leg has its own minimum output:** its simulated output less `slippageBps`. For Jupiter-wrapped legs, the route's `quoted_out_amount` is solved against Jupiter's own check, `quoted - floor(quoted * slippage_bps / 10,000)`, so the leg enforces exactly that minimum. A 0 bp leg returning exactly its minimum proved the rounding; the earlier formula was one atom too strict.
- **Bundle safety is flagged.** Legs that lock the validator vote account `J1to1…` (GoonFi, BisonFi, Kipseli) are marked `bundleSafe: false`, because Jito refuses such bundles.
- **Compute is measured.** The compute limit is the simulated compute of all legs together, plus 40,000 per token-account creation, plus 25%.

**Amounts are measured as the taker, in company.** Each leg's output comes from running all the legs together as the taker in the mirror, minus the run without that leg. The taker's token accounts get synthetic balances there (accounts the mirror writes for the taker, never streamed from chain). A generic quote can differ for two reasons found on 2026-10-18:

- **Per wallet.** GoonFi V2 paid one of 16 recent traders 0.1 bp less than every other wallet for the identical swap. Fresh keys and the trading wallet got the full price.
- **In company.** GoonFi V2 and Kipseli pay their Jupiter-routed price only when their Jupiter route is the transaction's only swap. Beside any other leg, GoonFi paid every wallet tested its direct-call price (0.1 bp less), and Kipseli failed a 5 bp minimum. Inside Jupiter's own split routes both stay inside one Jupiter instruction, which separate leg instructions cannot imitate.

So a route that puts GoonFi beside other legs is compared with the best route without GoonFi, built the same way, and the one paying the taker more wins (`withoutGoonFiOutAmount`). `quotedOutAmount` keeps the generic figure.

**Size limits.** A v0 transaction must fit 1,232 bytes (more legs fit with `lookupTables`); v1 fits 4,096 bytes without lookup tables. Either way at most 64 accounts can be locked. When a route does not fit, the endpoint searches again with fewer splits until it does. Measured:

- A single Manifest leg is a v0 transaction of ~735 bytes and 18 accounts.
- A four-leg USDC/USDT split is v1, 1.6–2.1 KB, 40–55 accounts.
- Limited to v0, the best fitting route kept two legs. That paid 0.03 bp less at 100k and 5.2 bp less at 1M.

**Two-hop routes** (`QUOTE_VIA`). Leg 2's input is unknown until leg 1 fills. Hop 2 is fixed at exactly what hop 1's minimum outputs guarantee, split in the quoted proportions and re-measured at that size. Whatever hop 1 returns above its minimum stays in the taker's intermediate token account, up to the slippage (`intermediateDust`). With 5 bp slippage through SOL that cost ~5 bp of output, left as wrapped SOL; through USDC it stays as USDC. The repo's executor program could instead resize leg 2 to the actual balance. The endpoint does not use it, because it would tie every client to that one deployed program. For two-hop routes, a low slippage keeps the dust small.

**Verification** (2026-10-18). Every build was simulated whole, in LiteSVM with synthetic funding and over RPC `simulateTransaction` (no signature check, replaced blockhash) against a real wallet's current balances. The RPC wallets were the trading wallet where its balance covered the input, otherwise a recent trader on the same pools.

| Route | LiteSVM | RPC, real balances |
|---|---|---|
| USDC↔USDG, Manifest, 500 and 10k | equal to the build | equal; 500 USDC→USDG on the trading wallet |
| USDC↔PYUSD, Manifest, 1k and 100k | equal | 1k USDC→PYUSD equal; PYUSD inputs not checked (no public wallet held enough) |
| PYUSD↔USDG, Manifest, 1k | equal | not checked (no wallet held enough) |
| USDC↔USDT splits, 10k–1M, up to 4 legs | equal | within 0.001–0.007 bp |
| Two-hop through SOL (direct pools removed), 500 and 10k | equal | equal on the trading wallet (500 USDC→USDT) |
| Multi-pair server, 500 USDC→USDG, trading wallet | equal | equal |

LiteSVM tests in `test/quote-swap.test.ts` cover single legs, splits, size and lock limits, and two-hop dust. They use the repo's mock router (`npm run build:test-router`) as a venue.

## Running the quote server on a Windows VPS

`litesvm` 1.5.0 publishes native binaries for macOS and Linux (x64 and arm64, glibc and musl) only. There is no `win32` build, so the quote server cannot run in Windows' own Node.js. The options, in order:

**1. WSL1 (no virtualization needed).** WSL1 translates Linux system calls to Windows, so Linux x64 binaries run directly. Unlike WSL2, it needs no nested virtualization, which many VPS hosts do not offer. What was checked from macOS:

- The `litesvm-linux-x64-gnu` binary needs at most GLIBC 2.17, so Ubuntu 18.04 and later can load it. Node.js 22 itself needs Ubuntu 20.04 or later.
- Its imports are ordinary: `mmap`, `mprotect` and `madvise` for the VM's JIT pages, pthreads, `eventfd`, `getrandom`.
- It uses none of `io_uring`, `memfd`, `userfaultfd` or `perf_event`, which WSL1 lacks or implements partly. libuv only turns on `io_uring` on kernels from 5.10, and WSL1 reports 4.4.

Nothing was run under WSL1 itself; there is no Windows machine here. Docker is installed on this Mac but its daemon was not running, and a container would not reproduce WSL1's syscall layer anyway. To verify on the VPS:

```powershell
wsl --install -d Ubuntu-22.04 --version 1        # or: wsl --set-default-version 1 before installing
```

```sh
# inside the WSL1 distro, in its own filesystem (~), not under /mnt/c: file access through /mnt/c is slow
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs git
git clone <this repository> ~/solanaarbitrage && cd ~/solanaarbitrage && npm install   # installs litesvm-linux-x64-gnu
cp /mnt/c/path/to/.env .                          # RPC URLs only; no keypair is needed
node -e "require('litesvm'); console.log('litesvm loads')"
npx tsx --test test/quote-swap.test.ts test/quote-live.test.ts test/quote-engine.test.ts   # needs npm run build:test-router for quote-swap
QUOTE_PAIRS=USDG/USDC,PYUSD/USDC,PYUSD/USDG,USDT/USDC QUOTE_VIA=USDC npm run quote-server
```

Then, from Windows (WSL1 shares the Windows network stack, so `127.0.0.1:8787` is the same port):

```powershell
curl.exe http://127.0.0.1:8787/health
```

WSL1 has no systemd, so keep the server alive with a restart loop started by a scheduled task at boot:

```sh
# ~/run-quote-server.sh
cd ~/solanaarbitrage
while true; do QUOTE_PAIRS=USDG/USDC,PYUSD/USDC,PYUSD/USDG,USDT/USDC QUOTE_VIA=USDC npx tsx src/quote-server.ts >> quote-server.log 2>&1; sleep 5; done
```

```powershell
schtasks /Create /TN QuoteServer /SC ONSTART /RU YOUR_WINDOWS_USER /RP * /TR "wsl.exe -d Ubuntu-22.04 -- bash -lc ~/run-quote-server.sh"
```

WSL distributions belong to a Windows user, so the task must run as that user, not SYSTEM. This was not tested on Windows; adjust it if the task does not start at boot.

**`taskkill /F /IM node.exe`.** arbbotstable's `hard_restart.cmd` runs this command. WSL1 processes are "pico" processes named `node`, with no `.exe` image. Whether `taskkill /IM node.exe` matches them is not verified here. Check on the VPS with `tasklist | findstr /I node` while the server runs. The restart loop above brings the server back either way. A server that was killed takes 5–60 s to be ready again, longer when Helius rate-limits discovery.

**2. A native Windows build.** The binding is napi-rs, and the loader accepts a custom binary through `NAPI_RS_NATIVE_LIBRARY_PATH`. Building `litesvm` for `x86_64-pc-windows-msvc` needs either the MSVC build tools (license acceptance) or cross-compiling with `cargo-xwin` (downloads the Windows SDK under Microsoft's license). Solana's VM JIT is written for Unix memory mapping, so a Windows build would likely have to fall back to the interpreter, several times slower. This was not attempted; nothing was installed and no license was accepted.

**3. A small Linux VPS** (2 vCPU, 2 GB) runs the server under the systemd unit above with no caveats. arbbotstable on Windows can call it over HTTPS (`QUOTE_SERVER_HOST=0.0.0.0` with `QUOTE_SERVER_TOKEN`, behind a firewall or tunnel). Each request then crosses the network.

