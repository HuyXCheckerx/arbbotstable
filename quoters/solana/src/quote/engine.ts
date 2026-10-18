import { U64_MAX, ui } from "../amounts.js";
import { type LocalQuote, base58 } from "../market/mirror.js";
import type { TokenInfo } from "../tokens.js";
import type { QuotePool } from "./venues.js";

export interface QuoteMarket {
  pools: Pick<QuotePool, "address" | "venue" | "label" | "mintA" | "mintB">[];
  /** Simulate `amount` of `input` into `pool`; the output token is the pool's other mint. */
  quote(pool: QuoteMarket["pools"][number], input: TokenInfo, amount: bigint): LocalQuote;
}
export interface QuoteSearchOptions { steps?: number;
  /** Most pools splitting one token amount: the input (direct pools and first hops), or one intermediate's (second hops). */
  maxSplits?: number; maxQuotes?: number; slippageBps?: number; refine?: boolean;
  /** Intermediate tokens for two-hop routes (input -> intermediate -> output); none quotes direct pools only. */
  via?: TokenInfo[];
  /** Two-hop routes the split search may use: the best N by output at the full input plus the best N at one grid unit. */
  maxTwoHopRoutes?: number;
  /** Drop routes that provably cannot carry input before the grid search (see "Dominance pruning" in quoteExactIn); default true. */
  prune?: boolean;
  /** Simulate and report every direct pool at the full amount (`singlePoolQuotes`), pruned or not; default true. */
  diagnostics?: boolean;
  /** Smallest amount the local searches move, in parts per million of the input; default 1. */
  precisionPpm?: number }
/** One simulated leg. A path is one direct pool, or every pool through one intermediate token. Legs of the same `path` run in
 * `hop` order, and several legs with the same `path` and `hop` split that hop: hop-1 legs spend the input, and hop-2 legs'
 * inputs sum exactly to the hop-1 legs' outputs. `shareBps` is the share of the input the path carries; `hopShareBps` is the
 * leg's share of its hop's input (Jupiter's `percent`). */
export interface QuoteAllocation { pool: string; venue: string; label: string; inputMint: string; outputMint: string;
  inAmount: string; outAmount: string; shareBps: number; hopShareBps: number; computeUnits: number; path: number; hop: number }
export interface SwapQuote { inAmount: string; outAmount: string; inputAmount: string; outputAmount: string;
  minimumOutputAmount: string; otherAmountThreshold: string; effectivePrice: string; routePlan: QuoteAllocation[] }
type Pool = QuoteMarket["pools"][number];
/** A direct pool, or two pools joined by an intermediate token. Each hop is its own simulation, fed the previous hop's output. */
interface Route { key: string; label: string; hops: { pool: Pool; input: TokenInfo }[]; pools: Set<string> }
interface Leg { pool: Pool; input: TokenInfo; amount: bigint; quote: LocalQuote }
interface RouteQuote { ok: boolean; amountOut: bigint; legs: Leg[]; error?: string }
interface Allocation { route: Route; amount: bigint; quote: RouteQuote }
interface Path { output: bigint; allocations: Allocation[]; chunks: number; remainder: boolean }
/** Legs grouped by the token they spend: the input (direct pools and first hops, by pool address) and each intermediate (its
 * second hops, by intermediate mint then pool address). An intermediate's second-hop inputs sum to its first hops' outputs. */
interface Flow { output: bigint; spend: Map<string, Leg>; second: Map<string, Map<string, Leg>> }

/** Pools per hop kept, at each ranking size, before every pairing of the shortlists is simulated end to end. */
const HOP_SHORTLIST = 3;
/** Rate margin (parts per million, 100 = 1 bp) below the dominance bound before a route is pruned (see quoteExactIn). */
const PRUNE_MARGIN_PPM = 50;
/** Routes with the best probe rates whose marginal rate at the full amount sets the dominance bound. */
const PRUNE_LEADERS = 3;
const COVERAGE = "Orca Whirlpool, Raydium CLMM, PancakeSwap, Manifest, GoonFi V2, Deriverse, JupLend AMM, BisonFi and Kipseli pools";

export function quoteSearchOptions(options: QuoteSearchOptions = {}) {
  const steps = options.steps ?? 20, maxSplits = options.maxSplits ?? 4;
  const maxQuotes = options.maxQuotes ?? 4096, slippageBps = options.slippageBps ?? 5, maxTwoHopRoutes = options.maxTwoHopRoutes ?? 4;
  if (!Number.isInteger(steps) || steps < 1 || steps > 100 || !Number.isInteger(maxSplits) || maxSplits < 1 || maxSplits > 8
      || !Number.isInteger(maxQuotes) || maxQuotes < 1 || maxQuotes > 20_000 || !Number.isInteger(slippageBps)
      || slippageBps < 0 || slippageBps > 10_000 || !Number.isInteger(maxTwoHopRoutes) || maxTwoHopRoutes < 1
      || maxTwoHopRoutes > 32) throw new Error("Invalid quote search bounds");
  const precisionPpm = options.precisionPpm ?? 1;
  if (!Number.isInteger(precisionPpm) || precisionPpm < 1 || precisionPpm > 10_000) throw new Error("Invalid quote search bounds");
  return { steps, maxSplits, maxQuotes, slippageBps, refine: options.refine ?? true, via: options.via ?? [], maxTwoHopRoutes,
    prune: options.prune ?? true, diagnostics: options.diagnostics ?? true, precisionPpm };
}

/** Venue errors meaning the pool ran out of liquidity (or of supplied tick arrays) before the input was filled.
 * A larger exact input walks the same path further and fails the same way, so it is not simulated.
 * "insufficient funds" is the token program refusing a vault transfer (GoonFi V2 when its inventory runs out). */
const CAPACITY_ERROR = /TickArraySequenceInvalidIndex|LiquidityInsufficient|InsufficientLiquidity|NotEnoughTickArrayAccount|partial fill|Error: insufficient funds/;
const descending = (a: bigint, b: bigint) => a < b ? 1 : a > b ? -1 : 0;

/** Exact-input aggregation over direct pools and, with `via`, two-hop routes through intermediate tokens.
 * Each pool is simulated once at its total allocation; a two-hop route simulates its second pool on the first pool's
 * simulated output. The grid and route search never combine routes sharing a pool, so no pool's liquidity is counted twice.
 * Dynamic programming optimizes a bounded allocation grid; it does not assume linear AMM prices.
 * A local search then moves sub-grid amounts between routes while the simulated total improves. With `via`, a second local
 * search lets a two-hop path split within a hop: several first hops may feed one second hop, and the reverse.
 * All quote calls are synchronous so account updates cannot interleave with the search.
 * `bestSinglePool` is the best single route: one pool, or one two-hop path.
 */
export function quoteExactIn(market: QuoteMarket, input: TokenInfo, output: TokenInfo, amount: bigint,
  options: QuoteSearchOptions = {}) {
  const { steps, maxSplits, maxQuotes, slippageBps, refine, via, maxTwoHopRoutes, prune, diagnostics, precisionPpm } = quoteSearchOptions(options);
  if (amount <= 0n || amount > U64_MAX || input.mint.equals(output.mint)) throw new Error("Invalid exact-input swap");
  const started = performance.now();
  const poolsOf = (a: TokenInfo, b: TokenInfo) => {
    const found = [...new Map(market.pools.filter(p => (p.mintA.equals(a.mint) && p.mintB.equals(b.mint))
      || (p.mintB.equals(a.mint) && p.mintA.equals(b.mint))).map(p => [String(p.address), p])).values()];
    if (found.length > 64) throw new Error("Quote engine supports at most 64 pools per pair");
    return found;
  };
  const pools = poolsOf(input, output);
  const count = amount < BigInt(steps) ? Number(amount) : steps;
  const unit = amount / BigInt(count), remainder = amount % BigInt(count);
  const cache = new Map<string, LocalQuote>();
  // Smallest input each pool (per input side) is known to be unable to fill (see CAPACITY_ERROR).
  const capacity = new Map<string, bigint>();
  let evaluated = 0, truncated = false, skipped = 0;
  const sample = (pool: Pool, from: TokenInfo, size: bigint): LocalQuote | undefined => {
    const side = `${base58(pool.address)}|${base58(from.mint)}`, key = `${side}|${size}`;
    if (cache.has(key)) return cache.get(key);
    const ceiling = capacity.get(side);
    if (ceiling !== undefined && size >= ceiling) {
      skipped++;
      return { ok: false, amountIn: 0n, amountOut: 0n, computeUnits: 0, micros: 0, error: `exceeds pool capacity (fails at ${ceiling})` };
    }
    if (evaluated >= maxQuotes) { truncated = true; return undefined; }
    evaluated++;
    let q: LocalQuote;
    try { q = market.quote(pool, from, size); }
    catch (error) { q = { ok: false, amountIn: 0n, amountOut: 0n, computeUnits: 0, micros: 0, error: (error as Error).message }; }
    if (q.ok && (q.amountIn !== size || q.amountOut <= 0n || q.amountOut > U64_MAX)) q = { ...q, ok: false, error: "Invalid or partial exact-input fill" };
    if (!q.ok && CAPACITY_ERROR.test(q.error ?? "") && (ceiling === undefined || size < ceiling)) capacity.set(side, size);
    cache.set(key, q);
    return q;
  };
  /** Undefined when the simulation budget ran out. */
  const evaluate = (route: Route, size: bigint): RouteQuote | undefined => {
    const legs: Leg[] = [];
    let amountIn = size;
    for (const hop of route.hops) {
      const q = sample(hop.pool, hop.input, amountIn);
      if (!q) return undefined;
      legs.push({ pool: hop.pool, input: hop.input, amount: amountIn, quote: q });
      if (!q.ok) return { ok: false, amountOut: 0n, legs, error: q.error };
      amountIn = q.amountOut;
    }
    return { ok: true, amountOut: amountIn, legs };
  };
  const overlap = (a: Route, b: Route) => [...b.pools].some(key => a.pools.has(key));
  const directRoutes: Route[] = pools.map(pool => ({ key: String(pool.address), label: pool.label, hops: [{ pool, input }],
    pools: new Set([String(pool.address)]) }));
  let bestSingle: Path | undefined;
  const single = (route: Route) => {
    const q = evaluate(route, amount);
    if (q?.ok && (!bestSingle || q.amountOut > bestSingle.output)) bestSingle = { output: q.amountOut,
      allocations: [{ route, amount, quote: q }], chunks: count, remainder: remainder > 0n };
    return q;
  };
  // Dominance pruning. Output is concave in input for every route (prices only worsen with size), so moving x of input
  // from route r to a route b gains at least x·(m_b(amount) − m_r(0)), where m is the marginal rate. A route whose best
  // rate is below some route's marginal rate at the full amount therefore never carries input in an optimum, and is never
  // the best single route either. m_r(0) is read as the rate at a small probe (at least one input token), m_b(amount) as
  // the slope over the last 0.1% of the amount, for the PRUNE_LEADERS routes with the best probe rates (and, with
  // diagnostics, the best route at the full amount); PRUNE_MARGIN_PPM absorbs the error of both and atomic rounding.
  // The probe is 1/10,000 of the amount, at least one input token or 1% of the amount; pruning is skipped when it is too
  // small for one atom of rounding to stay within half the margin. Two-hop routes are bounded the same way by the
  // product of their hops' best probe rates.
  const token = 10n ** BigInt(input.decimals), smallest = amount / 100n < token ? amount / 100n : token;
  const pruneProbe = amount / 10_000n > smallest ? amount / 10_000n : smallest;
  const rate = (q: LocalQuote | RouteQuote | undefined, size: bigint) => q?.ok ? Number(q.amountOut) / Number(size) : 0;
  const delta = amount / 1_000n;
  const marginalAtFull = (route: Route) => {
    const full = evaluate(route, amount), below = full?.ok ? evaluate(route, amount - delta) : undefined;
    return full?.ok && below?.ok ? Number(full.amountOut - below.amountOut) / Number(delta) : undefined;
  };
  const probeRates = new Map(prune && pruneProbe >= 2_000_000n / BigInt(PRUNE_MARGIN_PPM) ? directRoutes.map(route => [route, rate(evaluate(route, pruneProbe), pruneProbe)]) : []);
  const singlePoolQuotes = (diagnostics ? directRoutes : []).map(route => {
    const pool = route.hops[0].pool, q = single(route);
    return { pool: String(pool.address), venue: pool.venue, label: pool.label, ok: q?.ok ?? false,
      outAmount: q?.ok ? q.amountOut.toString() : undefined, outputAmount: q?.ok ? ui(q.amountOut, output.decimals) : undefined,
      error: q?.error ?? (!q ? "quote budget exhausted" : undefined) };
  });
  let threshold: number | undefined;
  if (prune && pruneProbe >= 2_000_000n / BigInt(PRUNE_MARGIN_PPM)) {
    const leaders = new Set([...directRoutes].sort((a, b) => probeRates.get(b)! - probeRates.get(a)!).slice(0, PRUNE_LEADERS));
    if (bestSingle) leaders.add(bestSingle.allocations[0].route);
    for (const route of leaders) {
      const m = marginalAtFull(route);
      if (m !== undefined && (threshold === undefined || m * (1 - PRUNE_MARGIN_PPM / 1e6) > threshold)) threshold = m * (1 - PRUNE_MARGIN_PPM / 1e6);
    }
  }
  let prunedRoutes = 0, prunedIntermediates = 0;
  const liveRoutes = threshold === undefined ? directRoutes : directRoutes.filter(route => {
    const keep = probeRates.get(route)! >= threshold!;
    if (!keep) prunedRoutes++;
    return keep;
  });
  if (!diagnostics) for (const route of liveRoutes) single(route);
  // Two-hop candidates: shortlist each hop's pools by simulated output at the full input and at one grid unit (the second
  // hop on the first hop's best output at that size), then simulate every pairing of the shortlists end to end.
  const intermediates = [...new Map(via.filter(m => !m.mint.equals(input.mint) && !m.mint.equals(output.mint))
    .map(m => [String(m.mint), m])).values()];
  const candidates: { route: Route; via: TokenInfo; full?: RouteQuote; small?: RouteQuote }[] = [];
  // Each intermediate leg pool at the full input (first hop) or at the best first hop's output (second hop).
  const hopQuotes: { via: string; hop: number; pool: string; venue: string; label: string; inAmount: string; ok: boolean;
    outAmount?: string; error?: string }[] = [];
  // Each intermediate's shortlists, which the hop-level split search draws its first and second hops from.
  const hopPools = new Map<string, { token: TokenInfo; first: Pool[]; second: Pool[] }>();
  for (const mid of intermediates) {
    let first = poolsOf(input, mid), second = poolsOf(mid, output);
    if (!first.length || !second.length) continue;
    if (threshold !== undefined) {
      const firstRates = first.map(pool => ({ pool, q: sample(pool, input, pruneProbe) })).filter(x => x.q?.ok);
      const lead = firstRates.reduce<typeof firstRates[number] | undefined>((a, b) => !a || b.q!.amountOut > a.q!.amountOut ? b : a, undefined);
      const secondRates = lead ? second.map(pool => ({ pool, q: sample(pool, mid, lead.q!.amountOut) })).filter(x => x.q?.ok) : [];
      const best1 = lead ? rate(lead.q, pruneProbe) : 0;
      const best2 = secondRates.reduce((m, x) => Math.max(m, rate(x.q, lead!.q!.amountOut)), 0);
      first = firstRates.filter(x => rate(x.q, pruneProbe) * best2 >= threshold!).map(x => x.pool);
      second = secondRates.filter(x => best1 * rate(x.q, lead!.q!.amountOut) >= threshold!).map(x => x.pool);
      prunedRoutes += (firstRates.length - first.length) + (secondRates.length - second.length);
      if (!first.length || !second.length) { prunedIntermediates++; continue; }
    }
    const ranked = (hop: number, list: Pool[], from: TokenInfo, size: bigint, record: boolean) => {
      const all = list.map(pool => ({ pool, q: sample(pool, from, size) }));
      if (record) for (const { pool, q } of all) {
        hopQuotes.push({ via: mid.symbol, hop, pool: String(pool.address), venue: pool.venue, label: pool.label, inAmount: size.toString(),
          ok: q?.ok ?? false, outAmount: q?.ok ? q.amountOut.toString() : undefined, error: q?.error ?? (!q ? "quote budget exhausted" : undefined) });
      }
      return all.filter(x => x.q?.ok).sort((a, b) => descending(a.q!.amountOut, b.q!.amountOut));
    };
    const shortlist = [new Map<string, Pool>(), new Map<string, Pool>()];
    for (const size of new Set([amount, unit])) {
      const firstRanked = ranked(1, first, input, size, size === amount);
      if (!firstRanked.length) continue;
      for (const { pool } of firstRanked.slice(0, HOP_SHORTLIST)) shortlist[0].set(String(pool.address), pool);
      for (const { pool } of ranked(2, second, mid, firstRanked[0].q!.amountOut, size === amount).slice(0, HOP_SHORTLIST)) {
        shortlist[1].set(String(pool.address), pool);
      }
    }
    if (shortlist[0].size && shortlist[1].size) {
      hopPools.set(String(mid.mint), { token: mid, first: [...shortlist[0].values()], second: [...shortlist[1].values()] });
    }
    for (const a of shortlist[0].values()) for (const b of shortlist[1].values()) {
      const route: Route = { key: `${a.address}>${b.address}`, label: `${a.label} → ${b.label}`,
        hops: [{ pool: a, input }, { pool: b, input: mid }], pools: new Set([String(a.address), String(b.address)]) };
      const full = evaluate(route, amount);
      candidates.push({ route, via: mid, full, small: unit === amount ? full : evaluate(route, unit) });
    }
  }
  const top = (pick: (c: typeof candidates[number]) => RouteQuote | undefined) => candidates.filter(c => pick(c)?.ok)
    .sort((a, b) => descending(pick(a)!.amountOut, pick(b)!.amountOut)).slice(0, maxTwoHopRoutes);
  const selected = new Set([...top(c => c.full), ...top(c => c.small)]);
  const twoHopRoutes = candidates.filter(c => selected.has(c)).map(c => c.route);
  for (const route of twoHopRoutes) single(route);
  const twoHopQuotes = candidates.map(c => ({ route: c.route.label, pools: c.route.hops.map(h => String(h.pool.address)), via: c.via.symbol,
    selected: selected.has(c), ok: c.full?.ok ?? false,
    intermediateAmount: c.full?.ok ? c.full.legs[0].quote.amountOut.toString() : undefined,
    outAmount: c.full?.ok ? c.full.amountOut.toString() : undefined, outputAmount: c.full?.ok ? ui(c.full.amountOut, output.decimals) : undefined,
    error: c.full?.error ?? (!c.full ? "quote budget exhausted" : undefined) }));
  const routes = [...liveRoutes, ...twoHopRoutes];
  let paths = new Map<string, Path>([["0/0/false", { output: 0n, allocations: [], chunks: 0, remainder: false }]]);
  if (maxSplits > 1) for (const route of routes) {
    const samples: { chunks: number; remainder: boolean; size: bigint; q: RouteQuote }[] = [];
    for (let chunks = 0; chunks <= count; chunks++) for (const tail of remainder > 0n ? [false, true] : [false]) {
      const size = unit * BigInt(chunks) + (tail ? remainder : 0n);
      if (size === 0n) continue;
      const q = evaluate(route, size);
      if (q?.ok) samples.push({ chunks, remainder: tail, size, q });
    }
    const next = new Map(paths); // Skipping a route is always permitted.
    for (const path of paths.values()) {
      if (path.allocations.length >= maxSplits || path.allocations.some(a => overlap(a.route, route))) continue;
      for (const s of samples) {
        if (path.chunks + s.chunks > count || (path.remainder && s.remainder)) continue;
        const candidate: Path = { chunks: path.chunks + s.chunks, remainder: path.remainder || s.remainder,
          output: path.output + s.q.amountOut, allocations: [...path.allocations, { route, amount: s.size, quote: s.q }] };
        if (candidate.output > U64_MAX) continue;
        const key = `${candidate.chunks}/${candidate.allocations.length}/${candidate.remainder}`;
        if (!next.has(key) || candidate.output > next.get(key)!.output) next.set(key, candidate);
      }
    }
    paths = next;
    if (truncated) break;
  }
  let best = bestSingle;
  for (const path of paths.values()) if (path.chunks === count && path.remainder === (remainder > 0n)
      && (!best || path.output > best.output)) best = path;
  // Sub-grid local search: move `step` of input from one leg to another route (an existing leg, or a new one while
  // maxSplits allows and it shares no pool with the other legs) when the simulated total rises, halving `step` from half a
  // grid unit down to ~1 ppm of the input. Every accepted leg is still simulated at its total allocation.
  const gridOutput = best?.output;
  let moves = 0;
  if (refine && best && maxSplits > 1 && routes.length > 1) {
    const legs = new Map(best.allocations.map(a => [a.route.key, a]));
    let total = best.output;
    const floor = amount * BigInt(precisionPpm) / 1_000_000n || 1n;
    for (let step = unit / 2n; step >= floor && !truncated && moves < 1_000;) {
      let move: { from: Allocation; rest?: Allocation; to: Allocation; gain: bigint } | undefined;
      for (const from of legs.values()) {
        if (from.amount < step) continue;
        const restAmount = from.amount - step, restQuote = restAmount ? evaluate(from.route, restAmount) : undefined;
        if (restAmount && !restQuote?.ok) continue;
        const loss = from.quote.amountOut - (restQuote?.amountOut ?? 0n);
        for (const route of routes) {
          const leg = legs.get(route.key);
          if (leg === from || (!leg && restAmount && legs.size >= maxSplits)) continue;
          if (!leg && [...legs.values()].some(other => (other !== from || restAmount > 0n) && overlap(other.route, route))) continue;
          const size = (leg?.amount ?? 0n) + step, q = evaluate(route, size);
          if (!q?.ok) continue;
          const gain = q.amountOut - (leg?.quote.amountOut ?? 0n) - loss;
          if (gain > 0n && total + gain <= U64_MAX && (!move || gain > move.gain)) move = { from, gain,
            rest: restAmount ? { route: from.route, amount: restAmount, quote: restQuote! } : undefined, to: { route, amount: size, quote: q } };
        }
      }
      if (!move) { step /= 2n; continue; }
      if (move.rest) legs.set(move.from.route.key, move.rest);
      else legs.delete(move.from.route.key);
      legs.set(move.to.route.key, move.to);
      total += move.gain;
      moves++;
    }
    best = { output: total, allocations: [...legs.values()], chunks: count, remainder: remainder > 0n };
  }
  const otherMint = (leg: Leg) => leg.pool.mintA.equals(leg.input.mint) ? leg.pool.mintB : leg.pool.mintA;
  const toFlow = (path: Path | undefined): Flow | undefined => {
    if (!path) return undefined;
    const flow: Flow = { output: path.output, spend: new Map(), second: new Map() };
    for (const { quote: { legs: [first, next] } } of path.allocations) {
      flow.spend.set(String(first.pool.address), first);
      if (!next) continue;
      const mid = String(next.input.mint);
      if (!flow.second.has(mid)) flow.second.set(mid, new Map());
      flow.second.get(mid)!.set(String(next.pool.address), next);
    }
    return flow;
  };
  // Hop-level split search: legs are grouped by the token they spend (see Flow), so a path through an intermediate can split
  // within a hop. A move shifts up to `step` of the input between pools spending it (direct pools and shortlisted first hops),
  // or the intermediate amount `step` buys between that intermediate's shortlisted second hops; moving a whole leg closes it.
  // A first hop's output change passes to one second-hop leg of its intermediate (the best of its current legs, or a
  // shortlisted pool when it has none), so second-hop inputs always sum exactly to the first hops' simulated outputs.
  // Steps halve from one grid unit to ~1 ppm of the input; every accepted leg is still simulated at its total allocation.
  // `refine: false` and `maxSplits: 1` skip it.
  const flow = toFlow(best);
  let hopSplit: { moves: number; gainAtomic: string; evaluated: number } | null = null;
  if (refine && flow && maxSplits > 1 && hopPools.size) {
    const { spend, second } = flow, start = flow.output, evaluatedBefore = evaluated;
    for (const mid of hopPools.keys()) if (!second.has(mid)) second.set(mid, new Map());
    type Changed = [before: Leg | undefined, after: Leg | undefined][];
    type Move = { gain: bigint; changes: [legs: Map<string, Leg>, pool: string, leg: Leg | undefined][] };
    const out = (leg?: Leg) => leg?.quote.amountOut ?? 0n;
    const inputs = (legs: Iterable<Leg>) => { let sum = 0n; for (const leg of legs) sum += leg.amount; return sum; };
    const midOf = (leg: Leg) => { const mint = otherMint(leg); return mint.equals(output.mint) ? undefined : String(mint); };
    const legAt = (pool: Pool, from: TokenInfo, size: bigint): Leg | undefined => {
      const quote = sample(pool, from, size);
      return quote?.ok ? { pool, input: from, amount: size, quote } : undefined;
    };
    // The best way for intermediate `mid`'s second hops to take `delta` more (or less) of it; nothing left closes them all.
    const absorb = (mid: string, delta: bigint): Move | undefined => {
      const legs = second.get(mid)!, { token, second: shortlist } = hopPools.get(mid)!, size = inputs(legs.values()) + delta;
      if (size < 0n) return undefined;
      if (size === 0n) return { gain: -[...legs.values()].reduce((sum, leg) => sum + out(leg), 0n),
        changes: [...legs.keys()].map(key => [legs, key, undefined]) };
      let choice: Move | undefined;
      for (const pool of legs.size ? [...legs.values()].map(leg => leg.pool) : shortlist) {
        const key = String(pool.address), current = legs.get(key), amountIn = (current?.amount ?? 0n) + delta;
        const next = amountIn > 0n ? legAt(pool, token, amountIn) : undefined;
        if (amountIn < 0n || (amountIn > 0n && !next)) continue;
        const gain = out(next) - out(current);
        if (!choice || gain > choice.gain) choice = { gain, changes: [[legs, key, next]] };
      }
      return choice;
    };
    // Direct pools' output changes are gains; first hops' output changes pass to their intermediate's second hops.
    const settleInput = (changed: Changed): Move | undefined => {
      const settled: Move = { gain: 0n, changes: [] }, deltas = new Map<string, bigint>();
      for (const [before, after] of changed) {
        const mid = midOf((after ?? before)!), delta = out(after) - out(before);
        if (mid === undefined) settled.gain += delta;
        else deltas.set(mid, (deltas.get(mid) ?? 0n) + delta);
      }
      for (const [mid, delta] of deltas) {
        const absorbed = delta ? absorb(mid, delta) : undefined;
        if (delta && !absorbed) return undefined;
        if (absorbed) { settled.gain += absorbed.gain; settled.changes.push(...absorbed.changes); }
      }
      return settled;
    };
    const settleOutput = (changed: Changed): Move =>
      ({ gain: changed.reduce((sum, [before, after]) => sum + out(after) - out(before), 0n), changes: [] });
    let total = start;
    // The best move of up to `size` of one token from a leg of `legs` (the legs spending it) to another candidate pool.
    const shift = (legs: Map<string, Leg>, token: TokenInfo, candidates: Pool[], size: bigint,
      settle: (changed: Changed) => Move | undefined) => {
      let choice: Move | undefined;
      for (const from of [...legs.values()]) {
        const moved = from.amount < size ? from.amount : size, rest = from.amount - moved;
        const restLeg = rest ? legAt(from.pool, token, rest) : undefined;
        if (rest && !restLeg) continue;
        for (const pool of candidates) {
          const key = String(pool.address), to = legs.get(key);
          if (to === from || (!to && rest && legs.size >= maxSplits)) continue;
          const toLeg = legAt(pool, token, (to?.amount ?? 0n) + moved), settled = toLeg && settle([[from, restLeg], [to, toLeg]]);
          if (settled && settled.gain > 0n && total + settled.gain <= U64_MAX && (!choice || settled.gain > choice.gain)) choice = {
            gain: settled.gain, changes: [[legs, String(from.pool.address), restLeg], [legs, key, toLeg!], ...settled.changes] };
        }
      }
      return choice;
    };
    const spendPools = [...new Map([...liveRoutes.map(r => r.hops[0].pool), ...[...hopPools.values()].flatMap(h => h.first)]
      .map(p => [String(p.address), p])).values()];
    const floor = amount * BigInt(precisionPpm) / 1_000_000n || 1n;
    let hopMoves = 0;
    for (let step = unit; step >= floor && !truncated && hopMoves < 1_000;) {
      let move = shift(spend, input, spendPools, step, settleInput);
      for (const [mid, legs] of second) {
        // The intermediate amount `step` of input currently buys.
        const firstIn = inputs([...spend.values()].filter(leg => midOf(leg) === mid));
        const size = firstIn ? step * inputs(legs.values()) / firstIn : 0n;
        const candidate = size ? shift(legs, hopPools.get(mid)!.token, hopPools.get(mid)!.second, size, settleOutput) : undefined;
        if (candidate && (!move || candidate.gain > move.gain)) move = candidate;
      }
      if (!move) { step /= 2n; continue; }
      for (const [legs, key, leg] of move.changes) {
        if (leg) legs.set(key, leg);
        else legs.delete(key);
      }
      total += move.gain;
      hopMoves++;
    }
    flow.output = total;
    hopSplit = { moves: hopMoves, gainAtomic: (total - start).toString(), evaluated: evaluated - evaluatedBefore };
  }
  // Price impact against the best rate any route pays for 1/10,000 of the input. Exact input must be spent in full,
  // so an input larger than the available liquidity is still "filled", at a price this makes visible.
  const probe = amount / 10_000n || 1n;
  let probeOut = 0n, probeRoute: Route | undefined;
  for (const route of routes) {
    const q = evaluate(route, probe);
    if (q?.ok && q.amountOut > probeOut) { probeOut = q.amountOut; probeRoute = route; }
  }
  const priceImpactBps = flow && probeOut > 0n
    ? Number(10_000_000n - flow.output * probe * 10_000_000n / (amount * probeOut)) / 1000 : null;
  const format = (flow: Flow | undefined): SwapQuote | undefined => {
    if (!flow) return undefined;
    // One path per direct leg, and one per intermediate: its first hops, then its second hops.
    const paths: Leg[][][] = [], byMid = new Map<string, Leg[][]>();
    for (const leg of flow.spend.values()) {
      const mid = otherMint(leg);
      if (mid.equals(output.mint)) { paths.push([[leg]]); continue; }
      let path = byMid.get(String(mid));
      if (!path) { path = [[], [...flow.second.get(String(mid))!.values()]]; byMid.set(String(mid), path); paths.push(path); }
      path[0].push(leg);
    }
    const inputs = (legs: Leg[]) => legs.reduce((sum, leg) => sum + leg.amount, 0n);
    const minimum = flow.output * BigInt(10_000 - slippageBps) / 10_000n;
    return { inAmount: amount.toString(), outAmount: flow.output.toString(), inputAmount: ui(amount, input.decimals),
      outputAmount: ui(flow.output, output.decimals), otherAmountThreshold: minimum.toString(), minimumOutputAmount: ui(minimum, output.decimals),
      effectivePrice: ui(flow.output * 10n ** BigInt(input.decimals) * 10n ** 12n / (amount * 10n ** BigInt(output.decimals)), 12),
      routePlan: paths.flatMap((hops, index) => hops.flatMap((legs, hop) => legs.map(leg => ({ pool: String(leg.pool.address),
        venue: leg.pool.venue, label: leg.pool.label, inputMint: String(leg.input.mint), outputMint: String(otherMint(leg)),
        inAmount: leg.amount.toString(), outAmount: leg.quote.amountOut.toString(), shareBps: Number(inputs(hops[0]) * 10_000n / amount),
        hopShareBps: Number(leg.amount * 10_000n / inputs(legs)), computeUnits: leg.quote.computeUnits, path: index, hop: hop + 1 })))) };
  };
  const symbols = intermediates.map(t => t.symbol).join(", ");
  return { engine: intermediates.length ? "local-two-hop-v2" : "local-direct-v1", swapMode: "ExactIn", inputMint: String(input.mint),
    outputMint: String(output.mint), inputSymbol: input.symbol, outputSymbol: output.symbol, inputDecimals: input.decimals,
    outputDecimals: output.decimals, inAmount: amount.toString(), slippageBps, quote: format(flow) ?? null,
    bestSinglePool: format(toFlow(bestSingle)) ?? null,
    splitImprovementAtomic: flow && bestSingle ? (flow.output - bestSingle.output).toString() : null,
    priceImpactBps, priceReference: probeRoute ? { pool: probeRoute.key, label: probeRoute.label,
      inAmount: probe.toString(), outAmount: probeOut.toString() } : null,
    singlePoolQuotes, twoHopQuotes, hopQuotes, search: { steps: count, maxSplits, maxQuotes, evaluated, truncated, poolCount: pools.length,
      twoHop: intermediates.length ? { via: intermediates.map(t => t.symbol), candidates: candidates.length,
        routes: twoHopRoutes.length, maxTwoHopRoutes } : null,
      skippedOverCapacity: skipped, pruned: threshold === undefined ? null : { routes: prunedRoutes, intermediates: prunedIntermediates },
      refinement: { enabled: refine, moves,
        gainAtomic: flow && gridOutput !== undefined ? (flow.output - gridOutput).toString() : null, hopSplit },
      durationMs: +(performance.now() - started).toFixed(3),
      algorithm: (intermediates.length ? "two-hop route shortlist + " : "")
        + (refine ? "bounded allocation-grid dynamic programming + sub-grid local search" : "bounded allocation-grid dynamic programming")
        + (hopSplit ? " + hop-level split local search" : "") },
    includesPoolFees: true, includesNetworkFees: false, transactionVerified: false,
    coverage: intermediates.length ? `direct and two-hop (via ${symbols}) routes over ${COVERAGE}; no RFQ or aggregator quotes`
      : `direct ${COVERAGE}; no multi-hop, RFQ or aggregator quotes` };
}
