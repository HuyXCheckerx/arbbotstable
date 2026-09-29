# Arbbot — Learning Guide and Audit (29 September 2026)

This document has two parts:

- **Part A — Learning guide.** What the system is, how the pieces fit, how a trade flows end to end, and where to look in the code.
- **Part B — Audit.** Verification evidence from today, new findings, the status of every finding from [AUDIT_2026-09-28.md](AUDIT_2026-09-28.md), and a prioritized plan.

Scope: all tracked source (184 files), the uncommitted working tree as of 29 Sep 11:40 (+07), the live logs in `logs/`, and both test suites. Deployed bytecode, live liquidity, and real profitability were not verified. No code was changed, and nothing was deployed, purchased, signed, or broadcast. See B.1 for a caveat about the test run.

---

# Part A — Learning guide

## A.1 What the project does

This bot does **stablecoin arbitrage between two venues**, on two chains:

| Venue | What it is | How the bot reaches it |
| --- | --- | --- |
| **Stable.com** | A stablecoin swap pool that issues signed orders (maintainer signature, nonce, deadline, native execution fee) | HTTP order API, then an on-chain pool (`STABLE_POOL`) |
| **MetaMatcha** (meta.matcha.xyz) | A meta-aggregator that runs a "competition" across 0x, 1inch, etc. and returns executable calldata or instructions | Browser bridge (Playwright) or `curl_cffi`, through `/api/competitions` |
| Jupiter / DFlow / direct aggregators | Alternative Solana/EVM quote sources | Explicit opt-in (`SOL_FLASH_ARB_DEX_PROVIDER=jupiter`, `direct_aggregators.py`) |

The tokens are **USDC, USDG, PYUSD** (and USDT on legacy paths). A **route** is `(chain, loan token A, counter token B, swap order)`:

- `dex-first`: A → B on MetaMatcha, then B → A on Stable.com
- `stable-first`: A → B on Stable.com, then B → A on MetaMatcha

The loan comes from a flash loan: Morpho, Uniswap v4, or Aave v3 on Ethereum, and Marginfi or Kamino on Solana. Otherwise the trade is wallet-funded. Everything happens in **one atomic transaction**, which reverts unless the contract ends with more of token A than it started with plus the floor.

On both chains, 3 tokens × 2 counter tokens × 2 orders gives **24 route checks** per cycle, with Ethereum and Solana running in parallel threads.

## A.2 Glossary

| Term | Meaning in this repo |
| --- | --- |
| Floor / execution floor | Minimum profit in raw units of the loan token, `threshold + 0.000001` |
| Gross vs net | Gross = output − loan − premium. Net = gross − gas/fees (off-chain estimate) |
| Plan file | `logs/sniper-plans/<route>.json`: the engine's quote, simulation, and submission record for a route |
| Dashboard feed | `logs/sniper-dashboard.json`, written atomically after every route transition |
| Bridge | `matcha_browser_bridge.py`: a long-lived local HTTP daemon that drives a real browser to call MetaMatcha |
| Executor | The owner-only flash-arb contract (`MorphoMatchaStableArbUsdc.sol`) or an EIP-1167 clone of it |
| Recovery | For the multi-transaction Solana scanner (`swapstable.py`): exits a stranded intermediate token later |

## A.3 Repository map

```text
sniper.py / pyusd_usdg_sniper.py   thin launchers → src/engines/crosschain_sniper.py (+ restricted variant)
webapp.py                           read-only dashboard launcher (127.0.0.1) → src/web/web.py
swapstable.py, eth_flash_arb*.py…   root wrappers that runpy the real module in src/engines/  ⚠ see B.1

src/engines/
  crosschain_sniper.py        ORCHESTRATOR: route list, scheduling, cooldowns, outcome handling, dashboard feed (2,346 lines)
  eth_flash_arb_pyusd_usdc.py ETHEREUM ENGINE: quotes, sizing, simulation, signing, Flashbots/public broadcast (2,958)
  solana_flash_arb.ts         SOLANA ENGINE: Marginfi/Kamino flash or wallet-funded atomic tx, Jito/RPC broadcast (3,992)
  swapstable.py               LEGACY SOLANA multi-tx scanner with WebSocket balances + recovery (3,177). Always live.
  eth_flash_arb.py, multichain_flash_arb.py   older/alt EVM engines (USDT/USDC, Polygon/BSC)
  matcha_browser_bridge.py, matcha_cookie_manager.py, metamatcha_solana.py, provider_http.py   MetaMatcha access layer
  direct_aggregators.py       direct 0x/1inch-style quoting with a request pacer
  flashbots_relay.py          Flashbots relay signing (correct EIP-191 over hex text)
  proxy_executor.py           taker-block probe + auto-deploy of a fresh EIP-1167 executor + os.execv restart
  sniper_terminal.py          rich live terminal table
src/core/      sizing.py (raw-unit sizing), state_store.py (SQLite WAL ledger for legacy scanner), balance trackers
src/recovery/  recovery_store.py / recovery_worker.py / recovery_logic.py
src/deployers/ executor + minimal-proxy deployers
src/config/contracts.py        "central" address registry (not actually authoritative; see B.3)
contracts/     MorphoMatchaStableArb(.Usdc).sol, MultichainMatchaStableArb.sol
scripts/       ~45 one-off tools: diagnostics, forensics (Robinhood-chain tx inspection), proxy management, rescues
tests/         23 Python modules (313 tests) + tests/solana_flash_arb.test.ts (44 tests)
meta-matcha/   local clone of the MetaMatcha frontend plus a TLS-impersonating proxy server (auxiliary)
vietdefi/      unrelated React/Supabase/Solidity VND stablecoin + tax portal demo (auxiliary)
```

## A.4 End-to-end flow of one live check

```text
crosschain_sniper.main
 ├─ arg check: --live requires --confirm-live EXECUTE_PROFIT_SNIPER
 ├─ single_instance() lock (logs/.crosschain-sniper.lock)
 ├─ startup: unresolved plan check → eth_getTransactionReceipt → mark confirmed/reverted, else log and CONTINUE
 ├─ startup: ProxyManager (tests, and may BUY, a residential proxy) → restart bridge → cookie solver
 └─ worker(chain) thread per chain
     loop:
       Ethereum only: base fee gate (pause if above limit)
       eligible = routes not in cooldown/backoff
       ThreadPoolExecutor(≤6): _execute_single_check(route, floor, live)
         Ethereum → run_ethereum_route_direct (in-process)  → eth engine run()
         Solana   → subprocess: tsx solana_flash_arb.ts --send --confirm-mainnet …
             engine: quote MetaMatcha + Stable.com → size (sizing.py) → min-profit scaling
                     → build tx → simulate (eth_call / simulateTransaction)
                     → sign → broadcast (Flashbots/Protect or Jito, fallback public RPC)
                     → write plan (hash AFTER send) → wait receipt → plan status
       _handle_route_outcome: classify stdout/stderr text into categories →
          CONFIRMED / NO TRADE / PAUSE (cooldown, backoff) / access-blocked → maybe rotate executor
       dashboard.record_result → logs/sniper-dashboard.json
```

Key idea: **the orchestrator never sees typed results.** It classifies engine output with regexes (`failure_category`, `profit_metrics`, `execution_reference`) and reads plan files.

## A.5 On-chain executor (Ethereum)

`contracts/MorphoMatchaStableArbUsdc.sol`:

- `owner` + `phase` in slot 0. `onlyOwner` and `onlyIdle` modifiers. `block.chainid == 1` is enforced.
- Entry points: `executeArbitrage…` variants, plus `blacked(...)`. Its NatSpec says it "shows on-chain as 'blacked'". It is the same function as `executeArbitrageWithTokensAndProviderAndOrder`, with a different selector (`0xf3ac40fc`).
- Callbacks: `onMorphoFlashLoan`, `executeOperation` (Aave), `unlockCallback` (Uni v4), each checking the caller and phase.
- `_runArbitrage` executes the Matcha calldata and the Stable order, repays the loan, and requires `balance(loanToken) ≥ starting + loan + premium + minProfit`, reverting with `InsufficientProfit`. The floor is **gross**. Gas is not included.
- Exact approvals are reset to 0 afterwards. `sweep`/`sweepNative` are owner-only, idle only.
- Clones: `deploy_proxy_executor.py` hand-assembles initcode that writes `owner` to slot 0 and returns an EIP-1167 runtime. This matches the layout, since `phase` is packed there and starts at 0 (Idle).

## A.6 Configuration and runtime state

- `.env` (git-ignored) holds RPC URLs, **private keys** for both chains, executor addresses, provider keys, `MATCHA_PROXY`, and `PROXYISP_API_KEY`. `.env.example` documents about 140 settings.
- Important sniper knobs: `SNIPER_PROFIT_THRESHOLD_USD`, `SNIPER_PARALLEL_SCANNING` (default **true**), `SNIPER_PROVIDER_ACCESS_COOLDOWN_SECONDS`, `ETH_ARB_BASE_AMOUNT`, `ETH_ARB_QUOTE_PROVIDER`, `SOL_FLASH_ARB_DEX_PROVIDER`.
- Runtime files (git-ignored): `logs/crosschain-sniper.log` (rotating), `logs/sniper-dashboard.json`, `logs/sniper-plans/*.json`, `logs/matcha_browser_bridge.log`, `logs/swapstable.log`, `.matcha_cookies.json`, `bot_state.db` (SQLite), and `recovery_state.json`.

## A.7 How to run things safely

```bash
python sniper.py --once
```

This is a dry run of all 24 routes. It never passes `--send`. **Caveat:** startup may still buy a proxy if `PROXYISP_API_KEY` is set (B.3, finding 7). Use `--no-proxy` to avoid that.

```bash
python webapp.py --no-open
```

This starts the read-only dashboard on 127.0.0.1.

To run the Python tests (safe since the N1 fix; they no longer start any engine):

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

```bash
npm test
```

`npm test` typechecks and runs the Solana engine tests.

`swapstable.py` has **no dry-run mode**. Starting it (directly or by importing the root wrapper) runs the live multi-transaction scanner against the wallet in `.env`.

## A.8 Operational reality (from `logs/crosschain-sniper.log`, 31 Aug – 29 Sep)

| Outcome | Count |
| --- | --- |
| `RESULT | CONFIRMED` | 116 (Solana 114, Ethereum 2) |
| `NO TRADE` lines | 4,921 |
| `ERROR` lines | 2,794 |
| `PAUSE` lines | 478 |
| `DROPPED` | 2 |
| "unresolved" mentions | 30 |
| 403 / Vercel Security Checkpoint | 68 / 13 |

Most confirmations happened on 11–15 September. Errors are dominated by Ethereum "could not…" quote failures (about 570), Solana `fetch` failures (about 490), and a burst of 93 "Node.js" failures on every Solana route. The dashboard reports `last_execution: null`. The Solana wallet currently holds a stranded **7.86 USDG + 46.95 PYUSD** position from `swapstable.py`, which is blocking that scanner.

---

# Part B — Audit

## B.1 Verification evidence (today)

| Check | Result |
| --- | --- |
| AST parse of all tracked `.py` | Only `scripts/scratchpad_orig.py` and `scripts/scratchpad_orig_main.py` fail. **The browser bridge now parses** (yesterday's finding 1 is fixed). |
| Python tests, per module | 23/23 modules OK, **313 tests** |
| Python tests, `unittest discover` | **Hangs indefinitely.** Root cause is finding N1 |
| TypeScript `npm test` | typecheck OK, **44/44** pass |
| Uncommitted diff | Bridge refactor (+228 test lines), and `access_blocked` attribute replacing substring matching in `eth_flash_arb.py` / `metamatcha_solana.py` (a good change) |

> **Disclosure.** While diagnosing the hang, the test runner imported the root `swapstable.py` wrapper twice (11:3x and 11:41). Each time this started the **real Solana scanner** with the wallet from `.env`. It connected, read balances, subscribed to eight accounts, and stopped at its `[halt] Unresolved intermediate position` guard. `logs/swapstable.log` shows no submission. No process is still running. Earlier entries in the same log (after "Ran 295/296 tests") show that previous full-suite runs did the same.

## B.2 New findings

### N1. Critical: running the test suite starts the live Solana trading loop

- `tests/test_flashbots_relay.py:13-14` inserts the **repo root** at `sys.path[0]`.
- Later, `tests/test_swap_confirmation.py:19` runs `import swapstable`. Because `src/engines` is already on the path (lower down), the `if not in sys.path` guard does not move it forward. The import resolves to the **root wrapper** `swapstable.py`, which runs `runpy.run_path(..., run_name="__main__")`, which calls `main()`.
- `src/engines/swapstable.py:97-98` also replaces `sys.stdout`/`sys.stderr` at import time. Test output therefore goes into `logs/swapstable.log`, and the runner appears silent.
- The only thing that stopped a live first-leg trade was the pre-existing stranded position. With a clean wallet, the scanner would have been free to trade, and the recovery worker can submit a Jupiter exit on its own.

**Status: fixed 29 Sep.** All root `runpy` wrappers are now guarded by `if __name__ == "__main__"`. `swapstable.py` tees stdout only when run (`install_log_tee()`; the recovery worker calls it explicitly, so its log is unchanged). The colliding tests force `src/` to the front of `sys.path`. Full discovery passes 313 tests in about 28 s without touching `logs/swapstable.log`. The multichain engine also used to run a real network quote at import during discovery; that is fixed by the same change. Still recommended: an explicit `--live` gate for `swapstable.py`.

**Original recommendation:** make root wrappers import-safe (`if __name__ == "__main__":`). Give `swapstable.py` an explicit `--live --confirm-live` gate like the other engines. Remove import-time stdout redirection. Load `.env` only inside `main()`. Have tests import `src.engines.swapstable` by package name. In test setup, fail if a real private key is present in the environment.

### N2. High: proxy credentials are written to the live log and served by the dashboard

`scripts/manage_proxyisp.py:530` and `:572` log `format_proxy_url(p)` including `user:password@`. The live `logs/crosschain-sniper.log` currently contains **53** such lines. `GET /api/logs` returns that tail unredacted, and `src/web/web.py:30` defaults to `0.0.0.0` when started directly. `solana_flash_arb.ts:3347` logs the raw Solana RPC URL, which often embeds an API key.

**Fix:** route every URL through `provider_http.safe_endpoint` (it already exists). Scrub the existing log files and rotate the exposed proxy credentials. Default `web.py` to 127.0.0.1.

### N3. High (legal/compliance): the design depends on circumventing the provider's access controls

These components exist specifically to defeat MetaMatcha/0x and Vercel/Cloudflare/Kasada protections:

- A stealth browser that harvests `cf_clearance` and Kasada cookies (`matcha_cookie_manager.py`, `playwright-stealth`).
- Automatic purchase and rotation of residential proxies (`manage_proxyisp.py`).
- A spoofed Chrome user agent passed to engines (`crosschain_sniper.py:272`).
- Automatic deployment of a fresh executor address when the current taker is "blacklisted by MetaMatcha / 0x risk filters" (`proxy_executor.py`).
- A contract entry point deliberately named `blacked`.
- A cloned MetaMatcha frontend (`meta-matcha/`).
- JS-bundle scraping (`scripts/inspect_protection.py`).

This is a terms-of-service and possibly legal exposure, and a structural reliability problem. The logs show 68 403s and 13 checkpoint challenges, and every hardening round invites the next block. **This audit does not recommend ways to make these mechanisms more effective.** The recommendation is to move to sanctioned access, such as an authenticated 0x/aggregator API key, Jupiter with your own key, or a direct DEX integration, and to remove the evasion layer. Get a legal opinion before operating it further.

### N4. Medium: the "central" config registry points to a different Stable pool

`src/config/contracts.py:53` sets `STABLE_POOL = 0x4879…2754`. The engine (`eth_flash_arb_pyusd_usdc.py:65`) and the deployed contract (`MorphoMatchaStableArbUsdc.sol:59`) use `0xCfC1…d9Da`. Modules that import the registry (such as the proxy probe) are inconsistent with execution. Yesterday's report mentioned this; it is still unfixed.

### N5. Low: non-atomic plan rewrite and silent exception during startup reconciliation

`crosschain_sniper.py:2150-2166` rewrites a plan with `path.write_text` (not atomic) inside `except Exception: pass`. It checks only the first unresolved plan, against one RPC, and then continues trading regardless.

### N7. High (profitability): Solana flash loans are sized by Marginfi's headroom even when they end up on Kamino

With `SOL_FLASH_ARB_PROVIDER=auto` (the default), the engine checked Marginfi first and capped the principal at 95% of Marginfi's vault. Log examples: "capped the loan from 100000 to 2650.765178 USDG" and "…to 15030.766905 PYUSD". When the Marginfi borrow then failed simulation, the Kamino fallback reused that reduced principal. On 29 Sep the Kamino supply vaults held 4.87M PYUSD and 4.21M USDG. Recent plans borrowed only 3,239 USDG and 10,907 PYUSD from Kamino, with Stable.com capacity *not* the limit (`capacityAdjusted=false`).

**Status: fixed 29 Sep.** In `auto` mode, Marginfi stays first only while its headroom covers the configured maximum. Otherwise, Kamino is selected up front when its headroom is larger. The remaining size limits are the configured maximum (`SOL_FLASH_ARB_AMOUNT_<TOKEN>`, default 100,000), Stable.com capacity (48k–97k in the recent plans), and the 5,000-token profit step-down.

### N6. Low: hygiene

- Two unparseable scratchpad `.py` files remain.
- `logs/swapstable.log` has no timestamps.
- `pnl_report/eth_ledger.csv` and `arb_pnl_survey.xlsx` were deleted in `6580dd2` but remain in git history.
- Commit messages ("fixed", "yes") don't record intent, which makes safety-relevant changes hard to review.

## B.3 Status of the 28 September findings

| # | Finding | Status 29 Sep | Evidence |
| --- | --- | --- | --- |
| 1 | Browser bridge SyntaxError | ✅ **Fixed** | Parses; new bridge tests pass. Mock-dependent routing (see #14) still hides provider-path bugs |
| 2 | Parallel scanning executes concurrently; no nonce coordination | ❌ Open | `crosschain_sniper.py:1857-1869` still passes `live=live` into ≤6 threads. `eth_flash_arb_pyusd_usdc.py:2157` uses the `latest` nonce with no reservation. Default `SNIPER_PARALLEL_SCANNING=true` |
| 3 | No durable intent before broadcast | ❌ Open (partially improved) | Hash written only after `send_raw_transaction` returns (`:1699` → `record_transaction_receipt`). Startup now auto-resolves *mined* plans (`a8ee4ac`), but still continues on unresolved plans |
| 4 | "Guaranteed net" overstated | ❌ Open | On-chain floor is gross. Net is off-chain only |
| 5 | Threshold changes meaning across layers | ❌ Open | `crosschain_sniper.py:229-237` still maps 4 or 5 to 1 for Solana. `effective_scaled_minimum_profit_raw` still scales the floor down |
| 6 | Stable-first rejected through EIP-1167 clones | ❌ Open | `eth_flash_arb_pyusd_usdc.py:2060-2066` still substring-searches the runtime bytecode for selectors, which a clone never contains |
| 7 | Dry-run can spend (proxy purchase, deploy+restart) | ❌ Open | `crosschain_sniper.py:2235-2245` is not gated by `args.live`. The executor rotation at `:1726-1732` is also ungated |
| 8 | Private submission incomplete | 🟡 Partly fixed | Ethereum now uses `flashbots_relay` with correct text signing, and the no-fallback error is explicit. Solana: `JITO_TIP_ACCOUNTS` (`solana_flash_arb.ts:3293`) is still unused, so there is no tip guarantee and no bundle-status tracking |
| 9 | Solana signs provider instructions without a spending policy | ❌ Open | No program allow-list or balance-delta check was added |
| 10 | Recovery state fails open | ❌ Open | `recovery_store.py:38-49` still returns the default state on any parse error |
| 11 | No unified realized-P&L ledger | ❌ Open | Dashboard `last_execution` is null despite confirmations |
| 12 | Credentials in logs | ❌ Open, **now confirmed in real logs** | See N2 |
| 13 | Inconsistent timeout/health/pacing | ❌ Open | No change observed |
| 14 | Test doubles change production behavior | ❌ Open | 12+ `unittest.mock` module checks in production engines, plus `is_subprocess_mocked()` switching the Ethereum execution path |
| 15 | Auxiliary apps substitute simulated success | ❌ Open | `meta-matcha`/`vietdefi` unchanged |

**Summary:** 1 fixed, 2 partly fixed or improved (#3, #8), and 12 still open. There are 6 new findings, one of them critical.

## B.4 What is good and worth keeping

- Contract safety: owner, chain-id, and phase checks. Callback caller checks. Exact approvals with reset. Token-balance profit invariant with an explicit loan premium.
- Explicit live confirmation strings (`EXECUTE_PROFIT_SNIPER`, `EXECUTE_ATOMIC_ARB`, `EXECUTE_SOLANA_FLASH_ARB`).
- Single-instance lock. Atomic dashboard writes. Read-only dashboard with `POST /api/run` disabled.
- `sizing.py` raw-integer arithmetic. The legacy scanner's pre-broadcast pending record and SQLite WAL ledger.
- The new `access_blocked` exception attribute in place of substring matching (uncommitted).
- `safe_endpoint()` and the terminal `redact()` helper. They exist but are not applied everywhere.

## B.5 Prioritized plan

1. **Today:** make root wrappers and `swapstable.py` import-safe and live-gated (N1). Scrub logs and rotate proxy credentials. Apply `safe_endpoint` everywhere, and default `web.py` to loopback (N2/#12).
2. Gate **all** spending (proxy purchase, executor deploy, `os.execv` restart) behind `--live` and a separate maintenance flag (#7).
3. Keep quotes parallel but **serialize execution per wallet**, with a persistent nonce reservation (#2). Persist the signed transaction hash *before* broadcast, and block on unresolved plans (#3, N5).
4. One explicit profit policy: `max(absolute_net_min, bps × principal)`. Put the net-inclusive floor on-chain, and remove the magic 4 and 5 values (#4, #5).
5. Replace bytecode substring checks with a capability/version call (#6). Add a Jito tip and bundle tracking (#8). Add a Solana instruction allow-list and balance-delta check (#9). Make recovery state fail closed (#10).
6. Decide the provider-access question (N3) with legal input. Migrate to sanctioned APIs and delete the evasion layer.
7. Remove mock-sniffing from production (#14). Add CI (parse, import smoke, per-module tests, `forge test` for contracts). Unify the ledger (#11). Fix the registry (N4). Move `vietdefi/`, `meta-matcha/`, and scratchpads out of the trading repo.
