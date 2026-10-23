import "dotenv/config";
import { timingSafeEqual } from "node:crypto";
import { type IncomingMessage, type ServerResponse, createServer } from "node:http";
import { resolve } from "node:path";
import { NATIVE_MINT } from "@solana/spl-token";
import { type AddressLookupTableAccount, PublicKey } from "@solana/web3.js";
import { connect, loadConfig, redact } from "./config.js";
import { type TokenInfo, loadIntermediateToken, loadToken } from "./tokens.js";
import { discoverRoutePools } from "./quote/discovery.js";
import { type QuoteMarket, quoteExactIn, quoteSearchOptions } from "./quote/engine.js";
import { LiveQuoteMarket } from "./quote/live.js";
import { TransactionLimitError, type TxVersion, buildSwap, instructionJson, simulateSwapLocally, simulateSwapOnRpc } from "./quote/swap.js";

const argv = process.argv.slice(2);
if (argv.includes("--help")) {
  console.log(`Usage: npm run quote-server -- [--pairs USDC/USDT,USDG/USDC] [--via SOL] [--host 127.0.0.1] [--port 8787]

Long-running local quote service. One consistent RPC snapshot, then accounts stream over websockets
into LiteSVM, where the deployed DEX programs price every quote. No aggregator API is called.

GET /quote?inputMint=...&outputMint=...&amount=ATOMIC[&slippageBps=5][&maxSplits=4][&onlyDirectRoutes=true]
GET /swap-instructions?(the /quote parameters)&taker=WALLET[&txVersion=auto|v0|v1][&lookupTables=A,B]
    [&computeUnitPrice=MICROLAMPORTS][&simulate=local|rpc|both|none]
    The taker's own swap: compute budget, idempotent token-account creation and one instruction per leg with
    its minimum output, plus the unsigned transaction. Only the taker's public key is used; nothing is signed.
GET /health   200 when the mirrored state is current, else 503 with reasons

Environment (flags override): QUOTE_PAIRS (comma-separated; one process and one mirror serve them all; QUOTE_PAIR
and --pair still work), QUOTE_VIA (default "none": direct pools only; "SOL" adds two-hop routes through SOL at ~3x the
accounts; a served stablecoin such as "USDC" routes pairs like PYUSD/USDG through pools the other pairs already mirror),
QUOTE_SERVER_HOST, QUOTE_SERVER_PORT, QUOTE_SERVER_TOKEN (required when the host is not loopback; send
"Authorization: Bearer TOKEN"), QUOTE_STEPS (10), QUOTE_PRECISION_PPM (100), QUOTE_MAX_SPLITS (4), QUOTE_MAX_QUOTES (4096),
QUOTE_MAX_LAG_SLOTS (8), QUOTE_MAX_CLOCK_AGE_MS (3000), QUOTE_POLL_MS (1000, accounts too large to stream),
QUOTE_RESYNC_MS (60000, full HTTP re-read), QUOTE_REDISCOVER_MINUTES (15). RPC: SOLANA_RPC_URL (+ backups),
websockets from SOLANA_WS_URL or HELIUS_RPC_URL, program-account scans from HELIUS_RPC_URL.`);
  process.exit(0);
}

const flag = (name: string) => { const i = argv.indexOf(name); return i >= 0 ? argv[i + 1] : undefined; };
const env = process.env;
const integer = (value: string, name: string, min: number, max = Number.MAX_SAFE_INTEGER) => {
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n < min || n > max) throw new Error(`${name} must be an integer from ${min} to ${max}`);
  return n;
};
const log = (event: string, fields: Record<string, unknown> = {}) =>
  console.log(JSON.stringify({ time: new Date().toISOString(), event, ...fields }));
/** Pools kept per intermediate-token leg from the DEX indexes, by TVL/liquidity (as in npm run quote). */
const LEG_INDEX_LIMIT = 8;
/** A market unhealthy this long is rebuilt from a fresh snapshot. */
const REBUILD_AFTER_UNHEALTHY_MS = 30_000;
/** After a failed rediscovery or rebuild, wait this long before trying again (each costs program-account scans). */
const RETRY_MS = 60_000;
/** Retries of a pair whose discovery had a failed source, 2, 4 and 8 s apart. */
const DISCOVERY_RETRIES = 3;

const pairSymbols = (flag("--pairs") ?? flag("--pair") ?? env.QUOTE_PAIRS ?? env.QUOTE_PAIR ?? "USDC/USDT").toUpperCase().split(",")
  .map(pair => pair.trim().split("/")).filter(pair => pair.length === 2);
if (!pairSymbols.length || pairSymbols.some(([a, b]) => !a || !b || a === b)) throw new Error("--pairs takes pairs of two different tokens, e.g. USDC/USDT,USDG/USDC");
const viaSymbols = (flag("--via") ?? env.QUOTE_VIA ?? "none").toUpperCase().split(",").map(s => s.trim()).filter(s => s && s !== "NONE");
const host = flag("--host") ?? env.QUOTE_SERVER_HOST ?? "127.0.0.1";
const port = integer(flag("--port") ?? env.QUOTE_SERVER_PORT ?? "8787", "QUOTE_SERVER_PORT", 1, 65_535);
const token = env.QUOTE_SERVER_TOKEN ?? "";
if (!/^(127\.|localhost$|::1$)/.test(host) && token.length < 16) {
  throw new Error("Set QUOTE_SERVER_TOKEN (16+ characters) to listen beyond loopback");
}
// Coarser than npm run quote's defaults (20 steps, 1 ppm): on identical snapshots these changed outputs by under 0.005 bp.
const search = quoteSearchOptions({ steps: integer(env.QUOTE_STEPS ?? "10", "QUOTE_STEPS", 1, 100),
  maxSplits: integer(env.QUOTE_MAX_SPLITS ?? "4", "QUOTE_MAX_SPLITS", 1, 8),
  maxQuotes: integer(env.QUOTE_MAX_QUOTES ?? "4096", "QUOTE_MAX_QUOTES", 1, 20_000),
  precisionPpm: integer(env.QUOTE_PRECISION_PPM ?? "100", "QUOTE_PRECISION_PPM", 1, 10_000) });
const limits = { maxLagSlots: integer(env.QUOTE_MAX_LAG_SLOTS ?? "8", "QUOTE_MAX_LAG_SLOTS", 0),
  maxClockAgeMs: integer(env.QUOTE_MAX_CLOCK_AGE_MS ?? "3000", "QUOTE_MAX_CLOCK_AGE_MS", 100) };
const pollMs = integer(env.QUOTE_POLL_MS ?? "1000", "QUOTE_POLL_MS", 200);
const resyncMs = integer(env.QUOTE_RESYNC_MS ?? "60000", "QUOTE_RESYNC_MS", 5_000);
const rediscoverMs = integer(env.QUOTE_REDISCOVER_MINUTES ?? "15", "QUOTE_REDISCOVER_MINUTES", 1) * 60_000;

const config = loadConfig({ SOLANA_RPC_URL: env.SOLANA_RPC_URL, HELIUS_RPC_URL: env.HELIUS_RPC_URL, SOLANA_WS_URL: env.SOLANA_WS_URL,
  PAIRS: pairSymbols.map(pair => pair.join("/")).join(",") });
// Replaced when a stalled market is rebuilt (see build).
let connection = connect(config);
// The clock gets its own websocket so account notifications cannot delay it (see Mirror).
let clockConnection = connect(config);
const scan = config.heliusUrl ? connect({ ...config, rpcUrl: config.heliusUrl }) : connection;
const symbols = [...new Set([...pairSymbols.flat(), ...viaSymbols])];
const loaded = new Map(await Promise.all(symbols.map(async (symbol): Promise<[string, TokenInfo]> => [symbol, symbol === "SOL"
  ? { ...await loadIntermediateToken(connection, NATIVE_MINT.toBase58()), symbol } : await loadToken(connection, symbol)])));
const pairs = pairSymbols.map(([a, b]) => [loaded.get(a)!, loaded.get(b)!] as [TokenInfo, TokenInfo]);
const via = viaSymbols.map(symbol => loaded.get(symbol)!);
const [[input, output]] = pairs;
const programCache = resolve("artifacts/programs");

/** Pools of every served pair (and their legs through `via`), deduplicated. Pairs are discovered one after another:
 * each runs several program-account scans, and Helius rate-limits many at once. */
async function discover() {
  const pools = new Map<string, Awaited<ReturnType<typeof discoverRoutePools>>["pools"][number]>(), warnings: string[] = [];
  for (const [a, b] of pairs) {
    // A failed source (usually a rate-limited scan) is retried with backoff; pools found by any attempt are kept.
    for (let attempt = 0; ; attempt++) {
      const found = await discoverRoutePools(a, b, via, { connection, scan }, LEG_INDEX_LIMIT);
      for (const pool of found.pools) pools.set(String(pool.address), pool);
      const failed = found.warnings.filter(w => /discovery failed/i.test(w));
      if (!failed.length || attempt === DISCOVERY_RETRIES) { warnings.push(...found.warnings); break; }
      await new Promise(resolve => setTimeout(resolve, 2_000 * 2 ** attempt));
    }
  }
  return { pools: [...pools.values()], warnings };
}

/** `fresh` opens new websockets first: after a sleep or a network change the old ones can stay open without delivering
 * notifications, and a market rebuilt on them stalls again. */
async function build(fresh = false): Promise<{ market: LiveQuoteMarket; warnings: string[] }> {
  if (fresh) { connection = connect(config); clockConnection = connect(config); }
  const market = new LiveQuoteMarket(connection, input, output, programCache, via, { clockConnection, pollMs, pairs: pairs.slice(1) });
  try {
    const found = await discover();
    // Only the programs of venues these pairs use: each deployed program costs memory in LiteSVM.
    await market.loadPrograms(new Set(found.pools.map(pool => pool.venue)));
    await market.start(found.pools);
    return { market, warnings: [...found.warnings, ...market.warnings].map(w => redact(w)) };
  } catch (error) { market.close(); throw error; }
}

let started = performance.now();
let { market, warnings } = await build();
let builtAt = Date.now(), rebuilds = 0, unhealthySince = 0, maintaining = false, lastResync = Date.now(), retryAt = 0;
// Started with a source missing: rediscover after RETRY_MS instead of the full interval.
if (warnings.some(w => /discovery failed/i.test(w))) builtAt = Date.now() - rediscoverMs + RETRY_MS;
log("ready", { pairs: pairs.map(([a, b]) => `${a.symbol}/${b.symbol}`), via: via.map(t => t.symbol), pools: market.pools.length,
  accounts: market.mirror.trackedCount, subscriptions: market.mirror.subscriptionCount, ms: Math.round(performance.now() - started), warnings });

const latencies: number[] = [];
let requests = 0, failures = 0;
const send = (res: ServerResponse, status: number, body: unknown) => {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
};
const authorized = (req: IncomingMessage) => {
  if (!token) return true;
  const given = Buffer.from(String(req.headers.authorization ?? "").replace(/^Bearer\s+/i, "") || String(req.headers["x-api-key"] ?? ""));
  const want = Buffer.from(token);
  return given.length === want.length && timingSafeEqual(given, want);
};
const percentile = (values: number[], p: number) => values.length ? [...values].sort((a, b) => a - b)[Math.floor((values.length - 1) * p)] : null;
type Reply = { status: number; body: unknown };

/** The parameters /quote and /swap-instructions share, or a 400 reply. */
function request(url: URL): Reply | { from: TokenInfo; to: TokenInfo; amount: bigint; slippageBps: number; maxSplits: number; routeVia: TokenInfo[] } {
  const tokenOf = (mint: string | null) => pairs.flat().find(t => String(t.mint) === mint);
  const from = tokenOf(url.searchParams.get("inputMint")), to = tokenOf(url.searchParams.get("outputMint"));
  if (!from || !to || !pairs.some(([a, b]) => (a === from && b === to) || (a === to && b === from))) {
    return { status: 400, body: { error: "inputMint/outputMint must be a served pair, either way", pairs: pairs.map(pair => pair.map(t => `${t.symbol} ${t.mint}`)) } };
  }
  const amountText = url.searchParams.get("amount") ?? "";
  if (!/^[1-9]\d{0,19}$/.test(amountText) || BigInt(amountText) > 18_446_744_073_709_551_615n) {
    return { status: 400, body: { error: "amount must be a positive integer in atomic units" } };
  }
  return { from, to, amount: BigInt(amountText), slippageBps: integer(url.searchParams.get("slippageBps") ?? "5", "slippageBps", 0, 10_000),
    maxSplits: integer(url.searchParams.get("maxSplits") ?? String(search.maxSplits), "maxSplits", 1, 8),
    routeVia: url.searchParams.get("onlyDirectRoutes") === "true" ? [] : via };
}

function quote(url: URL): Reply {
  const parsed = request(url);
  if ("status" in parsed) return parsed;
  const { from, to, amount, slippageBps, maxSplits, routeVia } = parsed;
  const health = market.health(limits);
  if (!health.ok) return { status: 503, body: { error: "mirrored state is not current", reasons: health.reasons } };
  const { hits, misses } = market;
  const slot = market.prepare();
  const result = quoteExactIn(market, from, to, amount, { ...search, slippageBps, maxSplits, via: routeVia });
  if (!result.quote) return { status: 422, body: { error: "no route can fill the entire input", contextSlot: slot } };
  const q = result.quote;
  return { status: 200, body: { inputMint: result.inputMint, inAmount: q.inAmount, outputMint: result.outputMint, outAmount: q.outAmount,
    otherAmountThreshold: q.otherAmountThreshold, swapMode: "ExactIn", slippageBps,
    priceImpactPct: result.priceImpactBps === null ? null : String(result.priceImpactBps / 10_000), priceImpactBps: result.priceImpactBps,
    routePlan: q.routePlan.map(leg => ({ venue: leg.venue, label: leg.label, marketKey: leg.pool, inputMint: leg.inputMint, outputMint: leg.outputMint,
      inAmount: leg.inAmount, outAmount: leg.outAmount, percent: leg.hopShareBps / 100, inputSharePct: leg.shareBps / 100,
      path: leg.path, hop: leg.hop })),
    contextSlot: slot, chainSlot: health.chainSlot, engine: result.engine, simulations: result.search.evaluated,
    cache: { hits: market.hits - hits, misses: market.misses - misses }, timeTaken: result.search.durationMs / 1000 } };
}

let blockhash: { value: string; lastValidBlockHeight: number; at: number } | undefined;
const tables = new Map<string, { table: AddressLookupTableAccount; at: number }>();
/** A recent blockhash for the returned transaction (refreshed every 15 s); clients may replace it before signing. */
async function recentBlockhash() {
  if (!blockhash || Date.now() - blockhash.at > 15_000) {
    const latest = await connection.getLatestBlockhash("confirmed");
    blockhash = { value: latest.blockhash, lastValidBlockHeight: latest.lastValidBlockHeight, at: Date.now() };
  }
  return blockhash;
}
/** The client's address lookup tables (v0 only), cached for a minute. */
async function lookupTables(keys: string[]) {
  return Promise.all(keys.map(async key => {
    const cached = tables.get(key);
    if (cached && Date.now() - cached.at < 60_000) return cached.table;
    const table = (await connection.getAddressLookupTable(new PublicKey(key))).value;
    if (!table) throw new Error(`lookup table ${key} not found`);
    tables.set(key, { table, at: Date.now() });
    return table;
  }));
}

/**
 * The taker's own transaction for the best route that fits one transaction: the route search with up to maxSplits legs,
 * then fewer while the transaction exceeds its byte or 64-account-lock limit. GoonFi V2 pays its Jupiter-routed price
 * only as a transaction's sole swap, so a route that puts GoonFi beside other legs is compared with the best route
 * without GoonFi, built the same way, and the one paying the taker more is returned.
 */
async function swapInstructions(url: URL): Promise<Reply> {
  const parsed = request(url);
  if ("status" in parsed) return parsed;
  const { from, to, amount, slippageBps, maxSplits, routeVia } = parsed;
  let taker: PublicKey;
  try { taker = new PublicKey(url.searchParams.get("taker") ?? url.searchParams.get("userPublicKey") ?? ""); }
  catch { return { status: 400, body: { error: "taker must be the signing wallet's public key" } }; }
  if (!PublicKey.isOnCurve(taker.toBytes())) return { status: 400, body: { error: "taker must be a wallet (on-curve) public key" } };
  const version = (url.searchParams.get("txVersion") ?? "auto") as TxVersion;
  if (!["auto", "v0", "v1"].includes(version)) return { status: 400, body: { error: "txVersion must be auto, v0 or v1" } };
  const simulate = url.searchParams.get("simulate") ?? "local";
  if (!["none", "local", "rpc", "both"].includes(simulate)) return { status: 400, body: { error: "simulate must be none, local, rpc or both" } };
  const computeUnitPrice = BigInt(integer(url.searchParams.get("computeUnitPrice") ?? "0", "computeUnitPrice", 0, 100_000_000));
  const [hash, luts] = await Promise.all([recentBlockhash(), lookupTables((url.searchParams.get("lookupTables") ?? "").split(",").filter(Boolean))]);
  // From here to the build nothing awaits, so streamed account updates cannot interleave with the search.
  const health = market.health(limits);
  if (!health.ok) return { status: 503, body: { error: "mirrored state is not current", reasons: health.reasons } };
  const begin = performance.now(), slot = market.prepare(), live = market;
  const attempt = (pools: QuoteMarket["pools"], splits: number) => {
    const result = quoteExactIn({ pools, quote: (pool, token, size) => live.quote(pool, token, size) }, from, to, amount,
      { ...search, slippageBps, maxSplits: splits, via: routeVia, diagnostics: false });
    return result.quote ? { result, build: buildSwap(live, result.quote, to, { taker, slippageBps, recentBlockhash: hash.value, version,
      tables: luts, computeUnitPrice }) } : undefined;
  };
  const fitting = (pools: QuoteMarket["pools"]) => {
    let limit: TransactionLimitError | undefined;
    for (let splits = maxSplits; splits >= 1; splits--) {
      try { return { chosen: attempt(pools, splits), limit }; }
      catch (error) { if (!(error instanceof TransactionLimitError)) throw error; limit = error; }
    }
    return { chosen: undefined, limit };
  };
  let { chosen, limit } = fitting(market.pools);
  if (!chosen) return { status: 422, body: { error: limit ? `no route fits one transaction: ${limit.message}` : "no route can fill the entire input", contextSlot: slot } };
  let alternative: bigint | null = null;
  if (chosen.build.legs.length > 1 && chosen.build.legs.some(leg => leg.venue === "goonfi")) {
    const other = fitting(market.pools.filter(pool => pool.venue !== "goonfi")).chosen;
    alternative = other ? other.build.outAmount : null;
    if (other && other.build.outAmount > chosen.build.outAmount) chosen = other;
  }
  const { result, build } = chosen, searchMs = performance.now() - begin;
  const local = simulate === "local" || simulate === "both" ? simulateSwapLocally(live, build, from, to, amount, taker) : undefined;
  const rpc = simulate === "rpc" || simulate === "both" ? await simulateSwapOnRpc(connection, build, to, taker) : undefined;
  const simulation = (s: typeof local) => s && { ok: s.ok, outAmount: s.outAmount.toString(), computeUnits: s.computeUnits, error: s.error, logs: s.logs };
  return { status: 200, body: { inputMint: String(from.mint), inAmount: amount.toString(), outputMint: String(to.mint),
    outAmount: build.outAmount.toString(), minOutAmount: build.minOutAmount.toString(), quotedOutAmount: result.quote!.outAmount,
    withoutGoonFiOutAmount: alternative?.toString() ?? null, slippageBps, taker: taker.toBase58(), bundleSafe: build.bundleSafe,
    legs: build.legs, intermediateDust: build.intermediateDust, computeUnitLimit: build.computeUnitLimit, computeUnitPrice: computeUnitPrice.toString(),
    computeBudgetInstructions: build.computeBudget.map(instructionJson), setupInstructions: build.setup.map(instructionJson),
    swapInstructions: build.swaps.map(instructionJson), addressLookupTableAddresses: luts.map(t => t.key.toBase58()),
    transaction: { version: build.version, base64: Buffer.from(build.transaction.serialize()).toString("base64"), bytes: build.bytes,
      accountLocks: build.accountLocks, recentBlockhash: hash.value, lastValidBlockHeight: hash.lastValidBlockHeight },
    simulation: { local: simulation(local) ?? null, rpc: simulation(rpc) ?? null },
    contextSlot: slot, chainSlot: health.chainSlot, timeTaken: searchMs / 1000 } };
}

const server = createServer(async (req, res) => {
  const begin = performance.now();
  try {
    const url = new URL(req.url ?? "/", "http://localhost");
    if (req.method !== "GET") return send(res, 405, { error: "GET only" });
    if (!authorized(req)) return send(res, 401, { error: "missing or wrong token" });
    if (url.pathname === "/health") {
      const health = market.health(limits);
      return send(res, health.ok ? 200 : 503, { ...health, requests, failures, rebuilds, builtAt: new Date(builtAt).toISOString(),
        p50Ms: percentile(latencies, 0.5), p99Ms: percentile(latencies, 0.99), rssMb: Math.round(process.memoryUsage().rss / 1e6) });
    }
    if (url.pathname !== "/quote" && url.pathname !== "/swap-instructions") return send(res, 404, { error: "use /quote, /swap-instructions or /health" });
    requests++;
    const { status, body } = url.pathname === "/quote" ? quote(url) : await swapInstructions(url);
    if (status !== 200) failures++;
    latencies.push(performance.now() - begin);
    if (latencies.length > 1_000) latencies.shift();
    send(res, status, body);
  } catch (error) {
    failures++;
    send(res, 400, { error: redact((error as Error).message) });
  }
});
server.keepAliveTimeout = 65_000;
server.listen(port, host, () => log("listening", { host, port, auth: Boolean(token) }));

// Maintenance: a full HTTP re-read every QUOTE_RESYNC_MS, rediscovery every QUOTE_REDISCOVER_MINUTES (a new market is
// built and swapped in only when the pool set changed), and a rebuild when the market stays unhealthy.
const maintenance = setInterval(async () => {
  if (maintaining) return;
  maintaining = true;
  try {
    const health = market.health(limits);
    unhealthySince = health.ok ? 0 : unhealthySince || Date.now();
    const stuck = unhealthySince > 0 && Date.now() - unhealthySince > REBUILD_AFTER_UNHEALTHY_MS;
    if ((stuck || Date.now() - builtAt > rediscoverMs) && Date.now() >= retryAt) {
      const found = stuck ? undefined : await discover();
      // A source that failed (a refused or rate-limited scan) would drop its pools: keep the current market, retry later.
      if (found?.warnings.some(w => /discovery failed/i.test(w))) {
        retryAt = Date.now() + RETRY_MS;
        throw new Error(`rediscovery incomplete: ${found.warnings.filter(w => /discovery failed/i.test(w)).map(w => redact(w)).join("; ")}`);
      }
      const current = market.addresses;
      const changed = !found || found.pools.length !== current.size || found.pools.some(p => !current.has(String(p.address)));
      if (changed) {
        started = performance.now();
        const next = await build(stuck), previous = market;
        ({ market, warnings } = next);
        previous.close();
        rebuilds++;
        unhealthySince = 0;
        log("rebuilt", { reason: stuck ? `unhealthy: ${health.reasons.join("; ")}` : "pool set changed", pools: market.pools.length,
          accounts: market.mirror.trackedCount, ms: Math.round(performance.now() - started), warnings });
      }
      builtAt = Date.now();
    } else if (Date.now() - lastResync > resyncMs) {
      lastResync = Date.now();
      await market.resync();
    }
  } catch (error) {
    retryAt = Date.now() + RETRY_MS;
    log("maintenance-error", { error: redact((error as Error).message), retryInMs: RETRY_MS });
  } finally { maintaining = false; }
}, 5_000);

const stats = setInterval(() => {
  const h = market.health(limits);
  log("stats", { requests, failures, p50Ms: percentile(latencies, 0.5), p99Ms: percentile(latencies, 0.99), ok: h.ok, reasons: h.reasons,
    lagSlots: h.streamLagSlots, pools: h.pools, syncing: h.syncing, cache: h.cache, rssMb: Math.round(process.memoryUsage().rss / 1e6) });
}, 60_000);

const shutdown = (signal: string) => {
  log("stopping", { signal });
  clearInterval(maintenance); clearInterval(stats);
  server.close();
  market.close();
  setTimeout(() => process.exit(0), 200).unref();
};
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("unhandledRejection", reason => log("unhandled-rejection", { error: redact(String((reason as Error)?.message ?? reason)) }));
