import "dotenv/config";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { PublicKey } from "@solana/web3.js";
import { NATIVE_MINT } from "@solana/spl-token";
import { atomic, ui } from "./amounts.js";
import { connect, loadConfig, redact } from "./config.js";
import { type TokenInfo, knownToken, loadIntermediateToken, loadToken } from "./tokens.js";
import { discoverRoutePools, type PoolCandidate } from "./quote/discovery.js";
import { quoteExactIn, quoteSearchOptions } from "./quote/engine.js";
import { SnapshotQuoteMarket } from "./quote/market.js";
import { type TxVersion, buildSwap, instructionJson, simulateSwapLocally, simulateSwapOnRpc } from "./quote/swap.js";
import { QUOTE_VENUES } from "./quote/venues.js";

/** Index pools kept per intermediate-token leg (by TVL/liquidity); those pairs list dozens of dust pools. */
const LEG_INDEX_LIMIT = 8;

const argv = process.argv.slice(2);
if (argv.includes("--help")) {
  console.log(`Usage: npm run quote -- [--amount 100000] [--input USDC] [--output USDT] [--via SOL] [--json]

Locally quote an exact-input swap from fresh RPC pool accounts and deployed DEX programs.
Pools on Orca Whirlpool, Raydium CLMM, PancakeSwap, Manifest, GoonFi V2, Deriverse, JupLend AMM,
BisonFi and Kipseli, used directly and in two-hop routes through an intermediate token; compares
the best single route with a bounded split across routes, refined below the grid resolution by
local search. No aggregator quote API, keypair, executor build, signing or network sends.

--steps 20              Allocation grid resolution (1-100).
--max-splits 4          Maximum pools splitting the input, or one intermediate's amount within
                        a two-hop path (1-8); 1 disables splitting.
--max-quotes 4096       Maximum local pool simulations (1-20000); each hop of a route counts.
--via SOL               Intermediate tokens for two-hop routes, comma-separated (SOL or a
                        supported stablecoin); "none" quotes direct pools only.
--max-two-hop-routes 4  Two-hop routes kept for the split: the best N at the full input
                        plus the best N at one grid unit (1-32).
--slippage-bps 5        Displayed minimum output; network fees are excluded.
--max-slot-spread 32    Reject account snapshots exceeding this slot range.
--pools VENUE:ADDRESS,...  Explicit universe; VENUE = ${QUOTE_VENUES.join(", ")}.
                        Skips discovery; all addresses are verified on chain. Pools of
                        input/intermediate and intermediate/output pairs form two-hop routes.
--program-cache DIR     Deployed program binaries, reused while their deploy slot is unchanged
                        (default artifacts/programs; "off" downloads them every run).
--save PATH             JSON report (default artifacts/local-quote.json).
--json                  Print the complete JSON report to stdout.
--taker PUBKEY          Also build the taker's own unsigned swap transaction for the route (see the README
                        section "Swap instructions"); only the public key is used, nothing is signed or sent.
--tx-version auto       auto (v0 when it fits 1,232 bytes, else v1), v0 or v1.
--simulate local        Simulate the built transaction whole: local (LiteSVM, synthetic funding), rpc (RPC
                        simulateTransaction against the taker's real balances), both, or none.

Defaults: 100000 USDC -> USDT. Uses SOLANA_RPC_URL and configured RPC backups.
Orca and Raydium indexes (top ${LEG_INDEX_LIMIT} by TVL for intermediate legs), program-account scans
(HELIUS_RPC_URL when set; PancakeSwap, Manifest, GoonFi, JupLend, BisonFi, Kipseli) and Deriverse PDAs
only discover addresses; all quote calculations run locally. GoonFi and Kipseli legs execute through
Jupiter's on-chain route instruction, which both price better than direct calls. GoonFi, BisonFi and Kipseli
read a validator vote account, so Jito bundles refuse them.
Split and two-hop outputs are snapshot estimates; a combined transaction has not been verified.`);
} else {
  let market: SnapshotQuoteMarket | undefined;
  try {
    const values = new Map<string, string>();
    const supported = new Set(["--amount", "--input", "--output", "--steps", "--max-splits", "--max-quotes", "--slippage-bps",
      "--max-slot-spread", "--pools", "--program-cache", "--save", "--via", "--max-two-hop-routes", "--taker", "--tx-version", "--simulate"]);
    for (let i = 0; i < argv.length; i++) {
      if (argv[i] === "--json") continue;
      if (!supported.has(argv[i])) throw new Error(`Unknown argument ${argv[i]}`);
      if (!argv[i + 1] || argv[i + 1].startsWith("--")) throw new Error(`Missing value for ${argv[i]}`);
      const name = argv[i], value = argv[++i];
      values.set(name, value);
    }
    const inputSymbol = (values.get("--input") ?? "USDC").toUpperCase();
    const outputSymbol = (values.get("--output") ?? "USDT").toUpperCase();
    const amount = atomic(values.get("--amount") ?? "100000", knownToken(inputSymbol).decimals);
    knownToken(outputSymbol);
    if (amount === 0n || inputSymbol === outputSymbol) throw new Error("Quote requires a positive amount and two distinct tokens");
    const searchOptions = quoteSearchOptions({ steps: Number(values.get("--steps") ?? "20"),
      maxSplits: Number(values.get("--max-splits") ?? "4"), maxQuotes: Number(values.get("--max-quotes") ?? "4096"),
      slippageBps: Number(values.get("--slippage-bps") ?? "5"), maxTwoHopRoutes: Number(values.get("--max-two-hop-routes") ?? "4") });
    const viaSymbols = [...new Set((values.get("--via") ?? "SOL").toUpperCase().split(",").map(v => v.trim()).filter(Boolean))]
      .filter(symbol => symbol !== "NONE");
    for (const symbol of viaSymbols) {
      if (symbol !== "SOL") knownToken(symbol);
      if (symbol === inputSymbol || symbol === outputSymbol) throw new Error(`--via ${symbol} is the input or output token`);
    }
    const maxSlotSpread = Number(values.get("--max-slot-spread") ?? "32");
    if (!Number.isSafeInteger(maxSlotSpread) || maxSlotSpread < 0) throw new Error("Invalid maximum snapshot slot spread");
    // Quote configuration is independent of the trading bot's PAIRS, balances, sizes and providers.
    const config = loadConfig({ SOLANA_RPC_URL: process.env.SOLANA_RPC_URL, HELIUS_RPC_URL: process.env.HELIUS_RPC_URL,
      SOLANA_WS_URL: process.env.SOLANA_WS_URL, PAIRS: `${inputSymbol}/${outputSymbol}` });
    const connection = connect(config);
    const started = performance.now();
    let explicit: PoolCandidate[] | undefined;
    if (values.has("--pools")) {
      explicit = values.get("--pools")!.split(",").map(entry => {
        const [venue, address, extra] = entry.trim().split(":");
        if (!(QUOTE_VENUES as readonly string[]).includes(venue) || !address || extra) throw new Error("--pools expects VENUE:ADDRESS entries");
        return { venue: venue as PoolCandidate["venue"], address: new PublicKey(address) };
      });
      explicit = [...new Map(explicit.map(p => [String(p.address), p])).values()];
      if (explicit.length > 64) throw new Error("At most 64 explicit pools are supported");
    }
    const programCache = values.get("--program-cache") ?? "artifacts/programs";
    // Wrapped SOL is verified like any intermediate mint (SPL mint, no freeze authority).
    const viaToken = async (symbol: string): Promise<TokenInfo> => symbol === "SOL"
      ? { ...await loadIntermediateToken(connection, NATIVE_MINT.toBase58()), symbol } : loadToken(connection, symbol);
    const via = await Promise.all(viaSymbols.map(viaToken));
    market = new SnapshotQuoteMarket(connection, knownToken(inputSymbol), knownToken(outputSymbol),
      programCache === "off" ? undefined : resolve(programCache), via);
    if (!argv.includes("--json")) console.error(`${explicit ? "Verifying" : "Discovering"} pools and loading deployed DEX programs from RPC...`);
    // Program-account scans need an RPC plan that serves getProgramAccounts; Helius does, many paid-tier-gated RPCs do not.
    const scan = config.heliusUrl ? connect({ ...config, rpcUrl: config.heliusUrl }) : connection;
    // Mint checks, pool discovery and program downloads are independent; run them concurrently.
    const [input, output, discovered] = await Promise.all([loadToken(connection, inputSymbol), loadToken(connection, outputSymbol),
      explicit ? { pools: explicit, warnings: [] }
        : discoverRoutePools(knownToken(inputSymbol), knownToken(outputSymbol), via, { connection, scan }, LEG_INDEX_LIMIT),
      market.loadPrograms(explicit ? explicit.map(p => p.venue) : QUOTE_VENUES)]);
    const candidates = discovered.pools;
    const discoveryWarnings: string[] = discovered.warnings;
    const prepared = performance.now();
    await market.start(candidates, maxSlotSpread);
    const txVersion = (values.get("--tx-version") ?? "auto") as TxVersion, simulate = values.get("--simulate") ?? "local";
    if (!["auto", "v0", "v1"].includes(txVersion) || !["none", "local", "rpc", "both"].includes(simulate)) {
      throw new Error("--tx-version takes auto, v0 or v1; --simulate takes local, rpc, both or none");
    }
    const buildFor = async (taker: PublicKey, quote: NonNullable<ReturnType<typeof quoteExactIn>["quote"]>) => {
      const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash("confirmed");
      const build = buildSwap(market!, quote, output, { taker, slippageBps: searchOptions.slippageBps, recentBlockhash: blockhash, version: txVersion });
      const local = simulate === "local" || simulate === "both" ? simulateSwapLocally(market!, build, input, output, amount, taker) : undefined;
      const rpc = simulate === "rpc" || simulate === "both" ? await simulateSwapOnRpc(connection, build, output, taker) : undefined;
      const summary = (sim: typeof local) => sim && { ok: sim.ok, outAmount: sim.outAmount.toString(), computeUnits: sim.computeUnits, error: sim.error };
      return { taker: taker.toBase58(), outAmount: build.outAmount.toString(), minOutAmount: build.minOutAmount.toString(), bundleSafe: build.bundleSafe,
        legs: build.legs, intermediateDust: build.intermediateDust, computeUnitLimit: build.computeUnitLimit,
        computeBudgetInstructions: build.computeBudget.map(instructionJson), setupInstructions: build.setup.map(instructionJson),
        swapInstructions: build.swaps.map(instructionJson), transaction: { version: build.version, bytes: build.bytes, accountLocks: build.accountLocks,
          recentBlockhash: blockhash, lastValidBlockHeight, base64: Buffer.from(build.transaction.serialize()).toString("base64") },
        simulation: { local: summary(local) ?? null, rpc: summary(rpc) ?? null } };
    };
    const loaded = performance.now();
    const result = quoteExactIn(market, input, output, amount, { ...searchOptions, via });
    const swap = values.has("--taker") && result.quote ? await buildFor(new PublicKey(values.get("--taker")!), result.quote) : undefined;
    const report = { ...result, swap, generatedAt: new Date().toISOString(), snapshot: market.snapshot,
      discovery: { source: explicit ? "explicit addresses" : "DEX pool indexes, program-account scans and PDAs", discoveredPools: candidates.length,
        intermediates: via.map(t => t.symbol), legIndexLimit: explicit ? null : LEG_INDEX_LIMIT,
        byVenue: Object.fromEntries(QUOTE_VENUES.map(venue => [venue, { discovered: candidates.filter(p => p.venue === venue).length,
          tradable: market!.pools.filter(p => p.venue === venue).length }])),
        emptyPools: market.emptyPools, tradablePools: market.pools.length,
        warnings: [...discoveryWarnings, ...market.warnings].map(message => redact(message)) },
      timings: { discoveryAndProgramsMs: +(prepared - started).toFixed(3), snapshotMs: +(loaded - prepared).toFixed(3),
        searchMs: result.search.durationMs },
      totalMs: +(performance.now() - started).toFixed(3) };
    for (const q of [...report.singlePoolQuotes, ...report.twoHopQuotes]) if (q.error) q.error = redact(q.error);
    const path = resolve(values.get("--save") ?? "artifacts/local-quote.json");
    await mkdir(resolve(path, ".."), { recursive: true });
    await writeFile(path, JSON.stringify(report, null, 2) + "\n");
    if (argv.includes("--json")) console.log(JSON.stringify(report, null, 2));
    else if (result.quote) {
      console.log(`${result.quote.inputAmount} ${input.symbol} -> ${result.quote.outputAmount} ${output.symbol}`);
      const paths: (typeof result.quote.routePlan)[] = [];
      for (const leg of result.quote.routePlan) (paths[leg.path] ??= []).push(leg);
      console.log(`Best single route: ${result.bestSinglePool?.outputAmount ?? "unavailable"}; split: ${paths.length} route(s)`
        + (result.splitImprovementAtomic ? `, +${ui(BigInt(result.splitImprovementAtomic), output.decimals)} ${output.symbol}` : ""));
      console.log(`Minimum output at ${result.slippageBps} bps: ${result.quote.minimumOutputAmount} ${output.symbol} (network fees excluded)`);
      if (result.priceImpactBps !== null) console.log(`Price impact: ${result.priceImpactBps.toFixed(2)} bps vs the best rate for ${ui(BigInt(result.priceReference!.inAmount), input.decimals)} ${input.symbol} (${result.priceReference!.label})`);
      if ((result.priceImpactBps ?? 0) > 100) console.error(`Warning: the input exceeds the liquidity these pools can absorb near market price; the route spends it anyway.`);
      const token = (mint: string) => market!.tokens.find(t => String(t.mint) === mint)!;
      const sum = (legs: typeof paths[number], field: "inAmount" | "outAmount") => legs.reduce((s, leg) => s + BigInt(leg[field]), 0n);
      for (const path of paths) {
        // A split hop prints as "A 96.00% + B 4.00%", each pool's share of that hop's input.
        const hops: (typeof path)[] = [];
        for (const leg of path) (hops[leg.hop - 1] ??= []).push(leg);
        console.log(`  ${hops.map(legs => legs.map(leg => legs.length > 1 ? `${leg.label} ${(leg.hopShareBps / 100).toFixed(2)}%` : leg.label)
          .join(" + ")).join(" → ")}: ${ui(sum(hops[0], "inAmount"), input.decimals)} ${input.symbol}`
          + hops.map(legs => ` -> ${ui(sum(legs, "outAmount"), token(legs[0].outputMint).decimals)} ${token(legs[0].outputMint).symbol}`).join(""));
      }
      console.log(`Slots ${market.snapshot!.accountSlotMin}-${market.snapshot!.accountSlotMax} (quoted at ${market.snapshot!.quoteSlot}); ${result.search.evaluated} simulations in ${result.search.durationMs} ms`
        + `; ${market.pools.length} of ${candidates.length} pools tradable; total ${Math.round(report.totalMs)} ms`);
      console.log(`Snapshot estimate; ${swap ? "the taker's transaction is simulated below" : "combined transaction unverified"}. Report: ${path}`);
      for (const warning of report.discovery.warnings) console.error(`Discovery warning: ${warning}`);
    } else console.error(`No pool allocation can fill the entire input. Diagnostics saved to ${path}`);
    if (swap) {
      const local = swap.simulation.local, rpc = swap.simulation.rpc;
      console.log(`Swap for ${swap.taker}: v${swap.transaction.version}, ${swap.transaction.bytes} bytes, ${swap.transaction.accountLocks} accounts, `
        + `${swap.legs.length} leg(s), out ${ui(BigInt(swap.outAmount), output.decimals)} (minimum ${ui(BigInt(swap.minOutAmount), output.decimals)}) ${output.symbol}`
        + `${swap.bundleSafe ? "" : "; locks a validator vote account, so Jito bundles refuse it"}`);
      for (const [name, sim] of [["LiteSVM", local], ["RPC", rpc]] as const) if (sim) {
        console.log(`  ${name} simulation: ${sim.ok ? `ok, ${ui(BigInt(sim.outAmount), output.decimals)} ${output.symbol}, ${sim.computeUnits} CU` : `failed: ${sim.error}`}`);
      }
    }
    if (!result.quote) process.exitCode = 2;
  } catch (error) {
    console.error(`Local quote failed: ${redact((error as Error).message)}`);
    process.exitCode = 1;
  } finally { market?.close(); }
}
