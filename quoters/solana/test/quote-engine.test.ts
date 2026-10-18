import test from "node:test";
import assert from "node:assert/strict";
import { Keypair, PublicKey } from "@solana/web3.js";
import { quoteExactIn, type QuoteMarket } from "../src/quote/engine.js";
import { knownToken } from "../src/tokens.js";
import type { LocalQuote } from "../src/market/mirror.js";

const usdc = knownToken("USDC"), usdt = knownToken("USDT");
const sol = { symbol: "SOL", mint: new PublicKey("So11111111111111111111111111111111111111112"), decimals: 9, program: usdc.program };
const pool = () => ({ address: Keypair.generate().publicKey, venue: "whirlpool" as const, label: "test pool", mintA: usdc.mint, mintB: usdt.mint });
const fill = (amount: bigint, out: bigint): LocalQuote => ({ ok: true, amountIn: amount, amountOut: out, computeUnits: 90_000, micros: 1 });

test("100000 USDC quote splits nonlinear pools using their total allocations", () => {
  const a = pool(), b = pool(), calls: bigint[] = [];
  const principal = 100_000_000_000n, reserve = 1_000_000_000_000n;
  const market: QuoteMarket = { pools: [a, b], quote: (_p, _i, size) => {
    calls.push(size); return fill(size, reserve * size / (reserve + size));
  } };
  const result = quoteExactIn(market, usdc, usdt, principal, { refine: false });
  assert.equal(result.inAmount, "100000000000");
  assert.equal(result.quote!.inputAmount, "100000.000000");
  assert.equal(result.quote!.outAmount, (2n * (reserve * (principal / 2n) / (reserve + principal / 2n))).toString());
  assert.equal(result.quote!.routePlan.length, 2);
  assert.deepEqual(result.quote!.routePlan.map(l => l.inAmount), ["50000000000", "50000000000"]);
  assert.ok(BigInt(result.splitImprovementAtomic!) > 0n);
  assert.ok(calls.every(size => size > 0n && size <= principal));
  assert.equal(calls.length, 44, "40 grid samples (full-pool samples reused), one price probe per pool (also the pruning probe), "
    + "and each pool's slope over the last 0.1% for the pruning bound");
  assert.equal(result.priceReference!.inAmount, (principal / 10_000n).toString());
  assert.ok(result.priceImpactBps! > 0 && result.priceImpactBps! < 1_000);
  const refined = quoteExactIn(market, usdc, usdt, principal);
  assert.equal(refined.quote!.outAmount, result.quote!.outAmount, "a symmetric optimum is already on the grid");
  assert.equal(refined.search.refinement.moves, 0);
  assert.equal(result.transactionVerified, false);
  assert.equal(result.includesNetworkFees, false);
});

test("non-divisible inputs preserve every atomic unit, with maxSplits enforced", () => {
  const a = pool(), b = pool(), c = pool();
  const result = quoteExactIn({ pools: [a, b, c], quote: (_p, _i, amount) => fill(amount, 1000n * amount / (100n + amount)) },
    usdc, usdt, 101n, { steps: 20, maxSplits: 2 });
  const allocations = result.quote!.routePlan;
  assert.equal(allocations.reduce((sum, l) => sum + BigInt(l.inAmount), 0n), 101n);
  assert.equal(allocations.length, 2);
  let optimal = 0n;
  for (let first = 0n; first <= 101n; first++) {
    const out = 1000n * first / (100n + first) + 1000n * (101n - first) / (201n - first);
    if (out > optimal) optimal = out;
  }
  assert.equal(BigInt(result.quote!.outAmount), optimal);
});

test("one pool, duplicate addresses and --max-splits 1 never manufacture split liquidity", () => {
  const a = pool();
  const market: QuoteMarket = { pools: [a, a], quote: (_p, _i, size) => fill(size, size * 99n / 100n) };
  const result = quoteExactIn(market, usdc, usdt, 1_000_000n, { maxSplits: 1 });
  assert.equal(result.search.poolCount, 1);
  assert.equal(result.search.evaluated, 2, "the full amount and the price probe");
  assert.equal(result.quote!.routePlan.length, 1);
  assert.equal(result.splitImprovementAtomic, "0");
  assert.equal(result.priceImpactBps, 0, "a linear pool has no price impact");
});

test("a large input can be filled by splitting even when no single pool can fill it", () => {
  const market: QuoteMarket = { pools: [pool(), pool()], quote: (_p, _i, size) => size > 50n
    ? { ...fill(50n, 50n), ok: false, error: "partial fill" } : fill(size, size) };
  const result = quoteExactIn(market, usdc, usdt, 100n);
  assert.equal(result.bestSinglePool, null);
  assert.equal(result.quote!.outAmount, "100");
  assert.equal(result.quote!.routePlan.length, 2);
});

test("partial fills, thrown errors and exhausted budgets never become complete quotes", () => {
  const a = pool(), b = pool();
  const partial = quoteExactIn({ pools: [a], quote: (_p, _i, amount) => fill(amount - 1n, amount) }, usdc, usdt, 100n);
  assert.equal(partial.quote, null);
  assert.match(partial.singlePoolQuotes[0].error!, /partial/);
  const failed = quoteExactIn({ pools: [a], quote: () => { throw new Error("bad tick array"); } }, usdc, usdt, 100n);
  assert.equal(failed.quote, null);
  assert.equal(failed.singlePoolQuotes[0].error, "bad tick array");
  const limited = quoteExactIn({ pools: [a, b], quote: (_p, _i, amount) => fill(amount, amount) }, usdc, usdt, 100n, { maxQuotes: 1 });
  assert.equal(limited.search.evaluated, 1);
  assert.equal(limited.search.truncated, true);
  assert.equal(limited.quote!.outAmount, "100", "a fully quoted single pool remains a usable estimate");
});

test("amount normalization, decimals and slippage are exact and no parity is assumed", () => {
  const input = { ...usdc, decimals: 9 };
  const result = quoteExactIn({ pools: [pool()], quote: (_p, _i, amount) => fill(amount, 997_123n) }, input, usdt,
    1_000_000_000n, { maxSplits: 1, slippageBps: 7 });
  assert.equal(result.quote!.effectivePrice, "0.997123000000");
  assert.equal(result.quote!.otherAmountThreshold, (997_123n * 9993n / 10000n).toString());
  assert.equal(result.quote!.inputAmount, "1.000000000");
  assert.equal(result.quote!.outputAmount, "0.997123");
});

test("empty universes, mismatched mints, tiny amounts and invalid bounds", () => {
  const a = pool(); a.mintA = Keypair.generate().publicKey;
  const market: QuoteMarket = { pools: [a], quote: (_p, _i, amount) => fill(amount, amount) };
  assert.equal(quoteExactIn(market, usdc, usdt, 1n).quote, null);
  market.pools = [pool()];
  assert.equal(quoteExactIn(market, usdc, usdt, 1n).quote!.inAmount, "1");
  for (const options of [{ steps: 0 }, { steps: 101 }, { maxSplits: 0 }, { maxQuotes: Infinity }, { slippageBps: -1 }]) {
    assert.throws(() => quoteExactIn(market, usdc, usdt, 100n, options), /bounds/);
  }
  assert.throws(() => quoteExactIn(market, usdc, usdt, 0n), /Invalid exact-input/);
  assert.throws(() => quoteExactIn(market, usdc, usdc, 100n), /Invalid exact-input/);
});

test("aggregation cannot overflow the receiving SPL token account", () => {
  const max = (1n << 64n) - 1n;
  const result = quoteExactIn({ pools: [pool(), pool()], quote: (_p, _i, size) => fill(size, max) }, usdc, usdt, 100n);
  assert.equal(result.quote!.outAmount, max.toString());
  assert.equal(result.quote!.routePlan.length, 1);
});

test("sub-grid refinement reaches the brute-force optimum that a coarse grid misses", () => {
  const a = pool(), b = pool();
  const curve = (reserve: bigint) => (size: bigint) => reserve * size / (reserve + size);
  const curves = new Map([[String(a.address), curve(3_000n)], [String(b.address), curve(1_200n)]]);
  const market: QuoteMarket = { pools: [a, b], quote: (p, _i, size) => fill(size, curves.get(String(p.address))!(size)) };
  let optimal = 0n;
  for (let first = 0n; first <= 1_000n; first++) {
    const out = curve(3_000n)(first) + curve(1_200n)(1_000n - first);
    if (out > optimal) optimal = out;
  }
  const grid = quoteExactIn(market, usdc, usdt, 1_000n, { steps: 4, refine: false });
  const refined = quoteExactIn(market, usdc, usdt, 1_000n, { steps: 4 });
  assert.ok(BigInt(grid.quote!.outAmount) < optimal, "the optimum is between grid points");
  assert.equal(BigInt(refined.quote!.outAmount), optimal);
  assert.equal(BigInt(refined.search.refinement.gainAtomic!), optimal - BigInt(grid.quote!.outAmount));
  assert.equal(refined.quote!.routePlan.reduce((sum, l) => sum + BigInt(l.inAmount), 0n), 1_000n);
  for (const leg of refined.quote!.routePlan) {
    assert.equal(leg.outAmount, curves.get(leg.pool)!(BigInt(leg.inAmount)).toString(), "every leg is a simulation at its total");
  }
});

test("refinement adds a small pool only within maxSplits", () => {
  const big = pool(), small = pool(), third = pool();
  // `small` pays a better rate but only for its first 30 units; the grid unit (250) is far too coarse to use it.
  const quote = (p: ReturnType<typeof pool>, size: bigint) => p === small
    ? (size > 30n ? { ...fill(size, 0n), ok: false, error: "LiquidityInsufficient" } : fill(size, size * 2n))
    : fill(size, 100_000n * size / (100_000n + size));
  const market: QuoteMarket = { pools: [big, small, third], quote: (p, _i, size) => quote(p as ReturnType<typeof pool>, size) };
  const refined = quoteExactIn(market, usdc, usdt, 1_000n, { steps: 4 });
  const legs = new Map(refined.quote!.routePlan.map(l => [l.pool, BigInt(l.inAmount)]));
  assert.equal(legs.get(String(small.address)), 30n);
  assert.ok(BigInt(refined.quote!.outAmount) > BigInt(quoteExactIn(market, usdc, usdt, 1_000n, { steps: 4, refine: false }).quote!.outAmount));
  const capped = quoteExactIn(market, usdc, usdt, 1_000n, { steps: 4, maxSplits: 2 });
  assert.ok(capped.quote!.routePlan.length <= 2);
  assert.equal(capped.quote!.routePlan.reduce((sum, l) => sum + BigInt(l.inAmount), 0n), 1_000n);
});

test("capacity failures stop larger simulations of that pool, other failures do not", () => {
  const thin = pool(), dusty = pool(), deep = pool(), sizes = new Map<string, bigint[]>();
  const market: QuoteMarket = { pools: [thin, dusty, deep], quote: (p, _i, size) => {
    const key = String(p.address);
    sizes.set(key, [...(sizes.get(key) ?? []), size]);
    if (p === thin && size > 20n) return { ...fill(size, 0n), ok: false, error: "Program log: Error Code: TickArraySequenceInvalidIndex" };
    if (p === dusty && size < 30n) return { ...fill(size, 0n), ok: false, error: "Program log: Error Code: AmountOutBelowMinimum" };
    return fill(size, size * 99n / 100n);
  } };
  const result = quoteExactIn(market, usdc, usdt, 100n, { steps: 10, refine: false });
  const thinSizes = sizes.get(String(thin.address))!;
  assert.deepEqual(thinSizes, [100n, 10n, 20n, 30n, 1n], "nothing above the first capacity failure (then the 1-unit price probe)");
  assert.ok(sizes.get(String(dusty.address))!.includes(90n), "a too-small failure says nothing about larger inputs");
  assert.equal(result.search.skippedOverCapacity, 6, "40..90; the full-size sample is already cached");
  assert.equal(result.search.evaluated, [...sizes.values()].reduce((n, s) => n + s.length, 0));
  assert.equal(result.quote!.outAmount, "99");
});

test("an input beyond the pools' liquidity is reported with its price impact", () => {
  // Spending the whole input forces a near-dead pool to absorb what the deep pool cannot.
  const deep = pool(), dead = pool();
  const market: QuoteMarket = { pools: [deep, dead], quote: (p, _i, size) => p === deep
    ? (size > 600_000n ? { ...fill(size, 0n), ok: false, error: "partial fill" } : fill(size, size))
    : fill(size, size / 1_000n + 1n) };
  const result = quoteExactIn(market, usdc, usdt, 1_000_000n);
  assert.equal(result.quote!.outAmount, "600401");
  assert.equal(result.priceReference!.pool, String(deep.address));
  assert.equal(result.priceReference!.inAmount, "100");
  assert.equal(result.priceImpactBps, 3995.99);
});

// Two-hop markets: `curves` maps pool address -> output for an input size (direction from the pool's mints).
const pairPool = (venue: "bisonfi" | "kipseli" | "whirlpool", a: PublicKey, b: PublicKey) =>
  ({ address: Keypair.generate().publicKey, venue, label: `${venue} ${a.toBase58().slice(0, 4)}/${b.toBase58().slice(0, 4)}`, mintA: a, mintB: b });
function twoHopMarket(curves: Map<ReturnType<typeof pairPool>, (size: bigint) => bigint>) {
  const calls: { pool: string; input: string; size: bigint }[] = [];
  const market: QuoteMarket = { pools: [...curves.keys()], quote: (p, input, size) => {
    calls.push({ pool: String(p.address), input: String(input.mint), size });
    return fill(size, curves.get(p as ReturnType<typeof pairPool>)!(size));
  } };
  return { market, calls };
}

test("a two-hop route feeds its first hop's simulated output into the second hop and wins when it pays more", () => {
  const direct = pairPool("whirlpool", usdc.mint, usdt.mint), first = pairPool("kipseli", sol.mint, usdc.mint), second = pairPool("bisonfi", sol.mint, usdt.mint);
  // 10 bps worse direct; via SOL: 1 USDC atom -> 10 lamports -> 1 USDT atom less 1 bp.
  const { market, calls } = twoHopMarket(new Map([[direct, (s: bigint) => s * 9_990n / 10_000n], [first, (s: bigint) => s * 10n],
    [second, (s: bigint) => s * 9_999n / 100_000n]]));
  const directOnly = quoteExactIn(market, usdc, usdt, 1_000_000n);
  assert.equal(directOnly.engine, "local-direct-v1");
  assert.deepEqual(directOnly.quote!.routePlan.map(l => l.pool), [String(direct.address)]);
  assert.equal(directOnly.twoHopQuotes.length, 0);
  assert.equal(directOnly.search.twoHop, null);
  calls.length = 0;
  const result = quoteExactIn(market, usdc, usdt, 1_000_000n, { via: [sol] });
  assert.equal(result.engine, "local-two-hop-v2");
  assert.equal(result.quote!.outAmount, "999900");
  const [hop1, hop2] = result.quote!.routePlan;
  assert.equal(result.quote!.routePlan.length, 2);
  assert.deepEqual([hop1.pool, hop1.inputMint, hop1.outputMint, hop1.inAmount, hop1.outAmount, hop1.path, hop1.hop, hop1.shareBps],
    [String(first.address), String(usdc.mint), String(sol.mint), "1000000", "10000000", 0, 1, 10_000]);
  assert.deepEqual([hop2.pool, hop2.inputMint, hop2.outputMint, hop2.inAmount, hop2.outAmount, hop2.path, hop2.hop, hop2.shareBps],
    [String(second.address), String(sol.mint), String(usdt.mint), "10000000", "999900", 0, 2, 10_000]);
  assert.equal(result.bestSinglePool!.outAmount, "999900", "the best single route may be a two-hop path");
  // Every second-hop simulation is sized by a first-hop simulation's output.
  const firstOutputs = new Set(calls.filter(c => c.pool === String(first.address)).map(c => c.size * 10n));
  const secondSizes = calls.filter(c => c.pool === String(second.address));
  assert.ok(secondSizes.length > 0 && secondSizes.every(c => c.input === String(sol.mint) && firstOutputs.has(c.size)));
  assert.equal(result.twoHopQuotes[0].intermediateAmount, "10000000");
  assert.ok(result.hopQuotes.some(q => q.hop === 1 && q.pool === String(first.address) && q.outAmount === "10000000"));
  assert.match(result.coverage, /two-hop \(via SOL\)/);
  assert.throws(() => quoteExactIn(market, usdc, usdt, 100n, { via: [sol], maxTwoHopRoutes: 0 }), /bounds/);
});

const curve = (reserve: bigint, scale: bigint) => (s: bigint) => scale * reserve * s / (reserve + s);
const total = (legs: { inAmount: string; outAmount: string }[], field: "inAmount" | "outAmount") =>
  legs.reduce((sum, l) => sum + BigInt(l[field]), 0n);

test("first hops share a second-hop pool only in the hop-level search, and two-hop paths split with direct pools", () => {
  const direct = pairPool("whirlpool", usdc.mint, usdt.mint);
  const firstA = pairPool("kipseli", sol.mint, usdc.mint), firstB = pairPool("bisonfi", sol.mint, usdc.mint);
  const second = pairPool("bisonfi", sol.mint, usdt.mint);
  // Both first hops are shallow, so splitting between them pays more once they can share the second hop.
  const { market } = twoHopMarket(new Map([[direct, curve(2_000n, 1n)], [firstA, curve(1_000n, 10n)], [firstB, curve(1_000n, 10n)],
    [second, (s: bigint) => s / 10n]]));
  const grid = quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol], refine: false });
  assert.equal(grid.quote!.routePlan.filter(l => l.hop === 1 && l.outputMint === String(sol.mint)).length, 1,
    "the grid combines no routes sharing a pool");
  assert.equal(grid.search.refinement.hopSplit, null);
  const result = quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol] });
  const plan = result.quote!.routePlan;
  // Equal marginal rates: the direct pool takes 500, each first hop 250.
  assert.equal(result.quote!.outAmount, "800");
  assert.ok(BigInt(result.quote!.outAmount) > BigInt(grid.quote!.outAmount));
  assert.deepEqual(plan.filter(l => l.pool === String(second.address)).map(l => l.inAmount), ["4000"], "one second-hop leg carries both first hops");
  assert.equal(new Set(plan.map(l => l.pool)).size, plan.length, "no pool is simulated twice");
  assert.ok(plan.some(l => l.pool === String(direct.address)) && plan.some(l => l.hop === 2), "direct and two-hop legs in one split");
  assert.equal(total(plan.filter(l => l.hop === 1), "inAmount"), 1_000n, "the whole input is spent");
  for (const path of new Set(plan.filter(l => l.hop === 2).map(l => l.path))) {
    assert.equal(total(plan.filter(l => l.path === path && l.hop === 2), "inAmount"), total(plan.filter(l => l.path === path && l.hop === 1), "outAmount"));
  }
  assert.equal(result.search.twoHop!.candidates, 2);
  assert.equal(BigInt(result.quote!.outAmount), total(plan.filter(l => l.outputMint === String(usdt.mint)), "outAmount"));
  assert.equal(BigInt(result.search.refinement.hopSplit!.gainAtomic), 800n - BigInt(grid.quote!.outAmount));
});

test("a hop-level split beats the best pair-only route", () => {
  const firstA = pairPool("kipseli", sol.mint, usdc.mint), firstB = pairPool("bisonfi", sol.mint, usdc.mint);
  const deep = pairPool("bisonfi", sol.mint, usdt.mint), worse = pairPool("whirlpool", sol.mint, usdt.mint);
  // Two shallow first hops; the deep second hop pays 10% more than the other at every size.
  const a = curve(1_000n, 10n), b = curve(1_500n, 10n), viaDeep = (m: bigint) => m / 10n, viaWorse = (m: bigint) => m * 9n / 100n;
  const curves = new Map([[firstA, a], [firstB, b], [deep, viaDeep], [worse, viaWorse]]);
  const { market, calls } = twoHopMarket(curves);
  // Pair routes cannot share a pool: each first hop feeds its own second hop. A hop-level split lets both feed `deep`.
  let pairOnly = 0n, optimum = 0n;
  for (let x = 0n; x <= 1_000n; x++) {
    for (const out of [viaDeep(a(x)) + viaWorse(b(1_000n - x)), viaWorse(a(x)) + viaDeep(b(1_000n - x))]) if (out > pairOnly) pairOnly = out;
    if (viaDeep(a(x) + b(1_000n - x)) > optimum) optimum = viaDeep(a(x) + b(1_000n - x));
  }
  assert.ok(BigInt(quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol], refine: false }).quote!.outAmount) <= pairOnly,
    "the grid alone only combines pair routes");
  calls.length = 0;
  const result = quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol] });
  assert.ok(optimum > pairOnly);
  assert.equal(result.quote!.outAmount, optimum.toString());
  const plan = result.quote!.routePlan;
  assert.deepEqual(plan.map(l => [l.pool, l.path, l.hop, l.inAmount, l.hopShareBps]), [
    [String(firstB.address), 0, 1, "600", 6_000], [String(firstA.address), 0, 1, "400", 4_000],
    [String(deep.address), 0, 2, (a(400n) + b(600n)).toString(), 10_000]]);
  assert.ok(plan.every(l => l.shareBps === 10_000), "one path carries the whole input");
  const byAddress = new Map([...curves].map(([p, f]) => [String(p.address), f]));
  for (const leg of plan) assert.equal(leg.outAmount, byAddress.get(leg.pool)!(BigInt(leg.inAmount)).toString(), "every leg is a simulation at its total");
  assert.equal(result.search.evaluated, calls.length);
  assert.ok(result.search.refinement.hopSplit!.moves > 0);
  assert.match(result.search.algorithm, /hop-level split/);
  assert.equal(quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol], maxSplits: 1 }).quote!.routePlan.length, 2, "one pool per hop");
  const limited = quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol], maxQuotes: 60 });
  assert.equal(limited.search.evaluated, 60);
  assert.equal(limited.search.truncated, true);
});

test("one first hop can feed several second hops", () => {
  const first = pairPool("bisonfi", sol.mint, usdc.mint);
  const secondA = pairPool("bisonfi", sol.mint, usdt.mint), secondB = pairPool("kipseli", sol.mint, usdt.mint);
  const { market } = twoHopMarket(new Map([[first, (s: bigint) => s * 10n], [secondA, (m: bigint) => curve(5_000n, 1n)(m) / 10n],
    [secondB, (m: bigint) => curve(3_000n, 1n)(m) / 10n]]));
  const pairOnly = quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol], refine: false });
  const result = quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol] });
  assert.ok(BigInt(result.quote!.outAmount) > BigInt(pairOnly.quote!.outAmount));
  const plan = result.quote!.routePlan;
  assert.deepEqual(plan.map(l => [l.pool, l.path, l.hop]), [[String(first.address), 0, 1], [String(secondA.address), 0, 2],
    [String(secondB.address), 0, 2]]);
  assert.equal(total(plan.filter(l => l.hop === 2), "inAmount"), BigInt(plan[0].outAmount));
});

test("intermediates without pools on both legs add no candidates", () => {
  const direct = pairPool("whirlpool", usdc.mint, usdt.mint), onlyFirst = pairPool("kipseli", sol.mint, usdc.mint);
  const { market } = twoHopMarket(new Map([[direct, (s: bigint) => s], [onlyFirst, (s: bigint) => s * 10n]]));
  const result = quoteExactIn(market, usdc, usdt, 1_000n, { via: [sol, usdc] });
  assert.deepEqual(result.search.twoHop, { via: ["SOL"], candidates: 0, routes: 0, maxTwoHopRoutes: 4 }, "the input itself is not an intermediate");
  assert.deepEqual(result.quote!.routePlan.map(l => l.pool), [String(direct.address)]);
});

// Concave constant-product pools at token scale (pruning needs a probe of at least 40,000 atoms).
const cp = (reserve: bigint, feeBps: bigint) => (size: bigint) => reserve * size * (10_000n - feeBps) / ((reserve + size) * 10_000n);

test("dominance pruning drops pools that cannot carry input and keeps the exact optimum", () => {
  const deep = pool(), second = pool(), shallow = pool(), wide = pool(), sizes = new Map<string, bigint[]>();
  const curves = new Map([[deep, cp(10_000_000_000_000n, 1n)], [second, cp(5_000_000_000_000n, 2n)],
    [shallow, cp(100_000_000_000n, 30n)], [wide, cp(100_000_000_000_000n, 50n)]]);
  const market: QuoteMarket = { pools: [...curves.keys()], quote: (p, _i, size) => {
    sizes.set(String(p.address), [...(sizes.get(String(p.address)) ?? []), size]);
    return fill(size, curves.get(p as ReturnType<typeof pool>)!(size));
  } };
  const amount = 10_000_000_000n;
  const exact = quoteExactIn(market, usdc, usdt, amount, { prune: false });
  sizes.clear();
  const pruned = quoteExactIn(market, usdc, usdt, amount, { diagnostics: false });
  assert.equal(pruned.quote!.outAmount, exact.quote!.outAmount);
  assert.deepEqual(pruned.search.pruned, { routes: 2, intermediates: 0 }, "the 30 bp and 50 bp pools are below the deep pool's marginal rate");
  assert.deepEqual(sizes.get(String(wide.address)), [1_000_000n], "the worst pool is probed (one token) and nothing else");
  assert.deepEqual(sizes.get(String(shallow.address)), [1_000_000n, amount, amount - amount / 1_000n],
    "a leader by probe rate is also simulated at the full amount and 0.1% below it, for the bound, then pruned");
  assert.ok(pruned.search.evaluated < exact.search.evaluated, `${pruned.search.evaluated} < ${exact.search.evaluated} simulations`);
  assert.deepEqual(pruned.singlePoolQuotes, [], "without diagnostics, pools are not reported one by one");
});

test("a deep pool that pays only at size survives pruning at that size", () => {
  const tight = pool(), deep = pool();
  const curves = new Map([[tight, cp(2_000_000_000_000n, 1n)], [deep, cp(10_000_000_000_000n, 3n)]]);
  const market: QuoteMarket = { pools: [...curves.keys()], quote: (p, _i, size) => fill(size, curves.get(p as ReturnType<typeof pool>)!(size)) };
  const small = quoteExactIn(market, usdc, usdt, 10_000_000n);
  assert.deepEqual(small.quote!.routePlan.map(l => l.pool), [String(tight.address)]);
  assert.equal(small.search.pruned!.routes, 1, "at 10 USDC the 3 bp pool is dominated");
  for (const amount of [10_000_000n, 1_000_000_000_000n]) {
    assert.equal(quoteExactIn(market, usdc, usdt, amount).quote!.outAmount, quoteExactIn(market, usdc, usdt, amount, { prune: false }).quote!.outAmount);
  }
  const large = quoteExactIn(market, usdc, usdt, 1_000_000_000_000n);
  assert.equal(large.search.pruned!.routes, 0);
  assert.ok(large.quote!.routePlan.some(l => l.pool === String(deep.address)), "at 1M USDC the deep pool carries most of it");
});

test("a coarser refinement floor costs fewer simulations, and the option is validated", () => {
  const a = pool(), b = pool();
  const curves = new Map([[a, cp(3_000_000_000_000n, 1n)], [b, cp(1_200_000_000_000n, 1n)]]);
  const market: QuoteMarket = { pools: [a, b], quote: (p, _i, size) => fill(size, curves.get(p as ReturnType<typeof pool>)!(size)) };
  const fine = quoteExactIn(market, usdc, usdt, 1_000_000_000_000n, { steps: 4 });
  const coarse = quoteExactIn(market, usdc, usdt, 1_000_000_000_000n, { steps: 4, precisionPpm: 1_000 });
  assert.ok(coarse.search.evaluated < fine.search.evaluated);
  assert.ok(BigInt(fine.quote!.outAmount) - BigInt(coarse.quote!.outAmount) <= 1_000_000_000_000n / 1_000_000n, "within 1 ppm of the input");
  for (const precisionPpm of [0, 10_001, 1.5]) assert.throws(() => quoteExactIn(market, usdc, usdt, 100n, { precisionPpm }), /bounds/);
});
