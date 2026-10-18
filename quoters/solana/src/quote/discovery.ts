import { createHash } from "node:crypto";
import { getBase58Decoder } from "@solana/kit";
import { type AccountInfo, type Connection, PublicKey } from "@solana/web3.js";
import { RAYDIUM_CLMM } from "../constants.js";
import type { TokenInfo } from "../tokens.js";
import { DERIVERSE, PANCAKESWAP, type QuotePool, type QuoteVenue, VENUE_LAYOUTS, bisonfiModel, deriverseInstrAccount, deriverseModel,
  deriverseTokenState, goonfiModel, juplendModel, kipseliModel, manifestModel, pancakeswapModel, quoteRaydiumModel,
  quoteWhirlpoolModel } from "./venues.js";

export interface PoolCandidate { venue: QuoteVenue; address: PublicKey }
const EXT_DISC = createHash("sha256").update("account:TickArrayBitmapExtension").digest().subarray(0, 8);
const MAX_CANDIDATES = 100;
const clmm = (venue: QuoteVenue) => venue === "raydium-clmm" || venue === "pancakeswap";
/** Venues found by program-account scans (no public pool index); Deriverse instruments are derived instead. */
const SCANNED: QuoteVenue[] = ["pancakeswap", "manifest", "goonfi", "juplend", "bisonfi", "kipseli"];
const SOURCE_NAMES: Record<QuoteVenue, string> = { whirlpool: "Orca", "raydium-clmm": "Raydium", pancakeswap: "PancakeSwap",
  manifest: "Manifest", goonfi: "GoonFi V2", deriverse: "Deriverse", juplend: "JupLend AMM", bisonfi: "BisonFi", kipseli: "Kipseli" };

/**
 * Public DEX indexes (Orca, Raydium) and, given `chain`, program-account scans and PDA derivation supply pool addresses
 * only; every pricing account is fetched independently from RPC. `chain.scan` needs getProgramAccounts (e.g. Helius).
 * `indexLimit` keeps only that many of each index's pools, by TVL (Orca) or liquidity (Raydium), as for intermediate-token
 * legs, whose pairs list dozens of dust pools.
 */
export async function discoverQuotePools(input: TokenInfo, output: TokenInfo, request: typeof fetch = fetch,
  chain?: { connection: Connection; scan?: Connection }, options: { indexLimit?: number } = {}) {
  const limit = options.indexLimit;
  if (limit !== undefined && (!Number.isInteger(limit) || limit < 1 || limit > 100)) throw new Error("Invalid pool index limit");
  const get = async (url: URL) => {
    const response = await request(url, { signal: AbortSignal.timeout(20_000) });
    if (!response.ok) throw new Error(`pool index returned HTTP ${response.status}`);
    return response.json();
  };
  const sources: { name: string; run: () => Promise<{ pools: PoolCandidate[]; truncated?: boolean }> }[] = [
    { name: "Orca", run: async () => {
      const url = new URL("https://api.orca.so/v2/solana/pools");
      url.search = new URLSearchParams({ tokensBothOf: `${input.mint},${output.mint}`, size: "100" }).toString();
      const j = await get(url);
      if (!Array.isArray(j.data)) throw new Error("Malformed Orca pool index");
      const listed: { address: string; tvlUsdc?: string | number }[] = limit
        ? [...j.data].sort((a, b) => Number(b.tvlUsdc ?? 0) - Number(a.tvlUsdc ?? 0)).slice(0, limit) : j.data;
      return { pools: listed.map(p => ({ venue: "whirlpool" as const, address: new PublicKey(p.address) })),
        truncated: !limit && !!j.meta?.cursor?.next };
    } },
    { name: "Raydium", run: async () => {
      const url = new URL("https://api-v3.raydium.io/pools/info/mint");
      url.search = new URLSearchParams({ mint1: String(input.mint), mint2: String(output.mint), poolType: "concentrated",
        poolSortField: "liquidity", sortType: "desc", pageSize: String(limit ?? 100), page: "1" }).toString();
      const j = await get(url);
      if (j.success !== true || !Array.isArray(j.data?.data)) throw new Error("Malformed Raydium pool index");
      return { pools: j.data.data.map((p: { id: string }) => ({ venue: "raydium-clmm" as const, address: new PublicKey(p.id) })),
        truncated: !limit && (j.data.hasNextPage === true || j.data.count > j.data.data.length) };
    } },
  ];
  if (chain) {
    const scan = chain.scan ?? chain.connection;
    for (const venue of SCANNED) sources.push({ name: SOURCE_NAMES[venue], run: async () => ({ pools: await scanPools(scan, venue, input, output) }) });
    sources.push({ name: "Deriverse", run: async () => ({ pools: await deriverseInstruments(chain.connection, input, output) }) });
  }
  const results = await Promise.allSettled(sources.map(source => source.run()));
  const pools: PoolCandidate[] = [], warnings: string[] = [];
  results.forEach((result, index) => {
    const venue = sources[index].name;
    if (result.status === "rejected") warnings.push(`${venue} discovery failed: ${(result.reason as Error).message}`);
    else { pools.push(...result.value.pools); if (result.value.truncated) warnings.push(`${venue} index returned a partial pool list`); }
  });
  const unique = [...new Map(pools.map(p => [String(p.address), p])).values()];
  if (unique.length > MAX_CANDIDATES) throw new Error(`More than ${MAX_CANDIDATES} pools discovered; use --pools to select a bounded pool universe`);
  if (!unique.length) throw new Error(`No direct pools discovered for ${input.symbol}/${output.symbol}. ${warnings.join("; ")}`);
  return { pools: unique, warnings };
}

/** Accounts of `venue` whose layout names the pair at its mint offsets, in both orders. Only addresses are transferred. */
async function scanPools(scan: Connection, venue: QuoteVenue, input: TokenInfo, output: TokenInfo): Promise<PoolCandidate[]> {
  const layout = VENUE_LAYOUTS[venue], base58 = getBase58Decoder();
  const found = await Promise.all([[input, output], [output, input]].map(([a, b]) => scan.getProgramAccounts(layout.program, {
    dataSlice: { offset: 0, length: 0 }, filters: [...(layout.size ? [{ dataSize: layout.size }] : []),
      { memcmp: { offset: 0, bytes: base58.decode(layout.discriminator) } },
      { memcmp: { offset: layout.mintA, bytes: a.mint.toBase58() } }, { memcmp: { offset: layout.mintB, bytes: b.mint.toBase58() } }] })));
  return found.flat().map(({ pubkey }) => ({ venue, address: pubkey }));
}

/** Deriverse spot instruments are PDAs of the two registered token ids (asset, currency); no scan is needed. */
async function deriverseInstruments(connection: Connection, input: TokenInfo, output: TokenInfo): Promise<PoolCandidate[]> {
  const states = await connection.getMultipleAccountsInfo([deriverseTokenState(input.mint), deriverseTokenState(output.mint)]);
  const ids = states.map((state, i) => {
    const mint = i ? output.mint : input.mint;
    if (!state || !state.owner.equals(DERIVERSE) || state.data.length !== 88 || state.data.readUInt32LE(0) !== 4
        || !state.data.subarray(8, 40).equals(mint.toBuffer())) return undefined;
    return state.data.readUInt32LE(72);
  });
  if (ids[0] === undefined || ids[1] === undefined) return [];
  // Either token can be the instrument's asset; keep the ordering that exists.
  const instruments = [deriverseInstrAccount(7, ids[0], ids[1]), deriverseInstrAccount(7, ids[1], ids[0])];
  const accounts = await connection.getMultipleAccountsInfo(instruments);
  return instruments.filter((_, i) => accounts[i]?.owner.equals(DERIVERSE)).map(address => ({ venue: "deriverse" as const, address }));
}

/** The first pair `account` validates for; otherwise the last pair's validation error. */
function validateForPairs(candidate: PoolCandidate, account: AccountInfo<Buffer> | null, pairs: [TokenInfo, TokenInfo][]): Buffer {
  for (const [i, [a, b]] of pairs.entries()) {
    try { return validatePoolAccount(candidate, account, a, b); }
    catch (error) { if (i === pairs.length - 1 || !/requested pair/.test((error as Error).message)) throw error; }
  }
  throw new Error("unreachable");
}

export function validatePoolAccount(candidate: PoolCandidate, account: AccountInfo<Buffer> | null,
  input: TokenInfo, output: TokenInfo): Buffer {
  const layout = VENUE_LAYOUTS[candidate.venue];
  if (!layout) throw new Error(`Unsupported venue ${candidate.venue}`);
  if (!account || account.executable || !account.owner.equals(layout.program)
      || (layout.size !== undefined ? account.data.length !== layout.size : account.data.length < Math.max(layout.mintA, layout.mintB) + 32)
      || !account.data.subarray(0, 8).equals(layout.discriminator)) {
    throw new Error(`Invalid ${candidate.venue} pool account ${candidate.address}`);
  }
  const a = new PublicKey(account.data.subarray(layout.mintA, layout.mintA + 32));
  const b = new PublicKey(account.data.subarray(layout.mintB, layout.mintB + 32));
  if (!((a.equals(input.mint) && b.equals(output.mint)) || (b.equals(input.mint) && a.equals(output.mint)))) {
    throw new Error(`Pool ${candidate.address} does not trade the requested pair`);
  }
  if ((candidate.venue === "whirlpool" || clmm(candidate.venue)) && account.data.readUInt16LE(candidate.venue === "whirlpool" ? 41 : 235) === 0) {
    throw new Error("Pool has invalid tick spacing");
  }
  return account.data;
}

/** Pool model from validated pool data; Raydium-layout pools also need their bitmap extension (if it exists). */
export function buildQuotePool(candidate: PoolCandidate, data: Buffer, extension?: Buffer): QuotePool {
  switch (candidate.venue) {
    case "whirlpool": return quoteWhirlpoolModel(candidate.address, data);
    case "raydium-clmm": return quoteRaydiumModel(candidate.address, data, raydiumArrayStarts(data, candidate.address, extension));
    case "pancakeswap": return pancakeswapModel(candidate.address, data, raydiumArrayStarts(data, candidate.address, extension));
    case "manifest": return manifestModel(candidate.address, data);
    case "goonfi": return goonfiModel(candidate.address, data);
    case "deriverse": return deriverseModel(candidate.address, data);
    case "juplend": return juplendModel(candidate.address, data);
    case "bisonfi": return bisonfiModel(candidate.address, data);
    case "kipseli": return kipseliModel(candidate.address, data);
  }
}

export const raydiumBitmapAddress = (pool: PublicKey, program = RAYDIUM_CLMM) => PublicKey.findProgramAddressSync([
  Buffer.from("pool_tick_array_bitmap_extension"), pool.toBuffer()], program)[0];
/** Bitmap extension of a Raydium-layout pool (Raydium CLMM or PancakeSwap); other venues have none. */
export const bitmapExtensionFor = (candidate: PoolCandidate) => clmm(candidate.venue)
  ? raydiumBitmapAddress(candidate.address, candidate.venue === "pancakeswap" ? PANCAKESWAP : RAYDIUM_CLMM) : undefined;

/** SDK PoolInfoLayout bitmap at byte 904; extension has fourteen 512-bit maps per side.
 * Deriving initialized arrays from these bitmaps avoids getProgramAccounts entirely.
 * Layout reference: raydium-io/raydium-sdk-V2/src/raydium/clmm/layout.ts.
 */
export function raydiumArrayStarts(data: Buffer, pool: PublicKey, extension?: Buffer): number[] {
  if (data.length !== 1544) throw new Error("Invalid Raydium pool length");
  const span = data.readUInt16LE(235) * 60;
  if (!span) throw new Error("Invalid Raydium tick spacing");
  const starts: number[] = [];
  const scan = (bitmap: Buffer, base: number) => {
    for (let bit = 0; bit < bitmap.length * 8; bit++) if (bitmap[bit >> 3] & (1 << (bit & 7))) {
      const start = (base + bit) * span;
      if (start <= 443636 && start + span > -443636) starts.push(start);
    }
  };
  scan(data.subarray(904, 1032), -512);
  if (extension) {
    if (extension.length !== 1832 || !extension.subarray(0, 8).equals(EXT_DISC)
        || !extension.subarray(8, 40).equals(pool.toBuffer())) throw new Error("Invalid Raydium bitmap extension");
    for (let group = 0; group < 14; group++) {
      scan(extension.subarray(40 + group * 64, 104 + group * 64), (group + 1) * 512);
      scan(extension.subarray(936 + group * 64, 1000 + group * 64), -(group + 2) * 512);
    }
  }
  return starts.sort((a, b) => a - b);
}

/** Pools for direct input/output routes and, for each intermediate, both legs (the top `legIndexLimit` index pools per
 * leg; program-account scans are not capped). A failed leg only removes its two-hop routes and becomes a warning. */
export async function discoverRoutePools(input: TokenInfo, output: TokenInfo, via: TokenInfo[], chain: { connection: Connection; scan?: Connection },
  legIndexLimit = 8, request: typeof fetch = fetch): Promise<{ pools: PoolCandidate[]; warnings: string[] }> {
  const legs = via.filter(mid => !mid.mint.equals(input.mint) && !mid.mint.equals(output.mint))
    .flatMap(mid => [[input, mid], [mid, output]] as [TokenInfo, TokenInfo][]);
  const [direct, legResults] = await Promise.all([discoverQuotePools(input, output, request, chain),
    Promise.all(legs.map(([a, b]) => discoverQuotePools(a, b, request, chain, { indexLimit: legIndexLimit }).then(
      found => ({ pools: found.pools, warnings: found.warnings.map(w => `${a.symbol}/${b.symbol} leg: ${w}`) }),
      (error: Error) => ({ pools: [] as PoolCandidate[], warnings: [`${a.symbol}/${b.symbol} leg: ${error.message}`] }))))]);
  return { pools: [...new Map([...direct.pools, ...legResults.flatMap(r => r.pools)].map(p => [String(p.address), p])).values()],
    warnings: [...direct.warnings, ...legResults.flatMap(r => r.warnings)] };
}

/** Each pool must trade one of `pairs`. `skipEmpty` drops valid pools without active liquidity, so their vaults and tick
 * arrays are never fetched. */
export async function hydrateQuotePools(connection: Connection, candidates: PoolCandidate[], pairs: [TokenInfo, TokenInfo][],
  options: { skipEmpty?: boolean } = {}) {
  if (!pairs.length) throw new Error("No token pairs to hydrate");
  const extensions = candidates.flatMap(p => { const address = bitmapExtensionFor(p); return address ? [{ pool: p, address }] : []; });
  const [response, ext] = await Promise.all([
    connection.getMultipleAccountsInfoAndContext(candidates.map(p => p.address), "processed"),
    extensions.length ? connection.getMultipleAccountsInfo(extensions.map(e => e.address), "processed") : [],
  ]);
  const extByPool = new Map(extensions.map((e, i) => [String(e.pool.address), ext[i]]));
  const pools: QuotePool[] = [], warnings: string[] = [], data = new Map<string, Buffer>();
  let empty = 0;
  for (const [i, candidate] of candidates.entries()) {
    try {
      const pool = validateForPairs(candidate, response.value[i], pairs);
      const extension = extByPool.get(String(candidate.address));
      if (extension && !extension.owner.equals(VENUE_LAYOUTS[candidate.venue].program)) throw new Error("Foreign bitmap extension owner");
      const model = buildQuotePool(candidate, pool, extension?.data);
      if (options.skipEmpty && !model.tradable(pool)) { empty++; continue; }
      pools.push(model);
      data.set(String(candidate.address), pool);
      if (extension) data.set(String(bitmapExtensionFor(candidate)), extension.data);
    } catch (error) { warnings.push((error as Error).message); }
  }
  // `data`: the validated pool (and bitmap extension) accounts each model was built from.
  return { pools, warnings, empty, data };
}
