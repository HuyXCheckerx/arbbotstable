import test from "node:test";
import assert from "node:assert/strict";
import { type Connection, Keypair, type PublicKey } from "@solana/web3.js";
import { Clock } from "litesvm";
import type { LocalQuote } from "../src/market/mirror.js";
import { LiveQuoteMarket } from "../src/quote/live.js";
import type { QuotePool } from "../src/quote/venues.js";
import { knownToken } from "../src/tokens.js";

const usdc = knownToken("USDC"), usdt = knownToken("USDT");
const key = () => Keypair.generate().publicKey;
/** Counts simulations instead of running them; the cache and invalidation logic is what is under test. */
class CountingMarket extends LiveQuoteMarket {
  calls = 0;
  protected override simulate(_model: QuotePool | undefined, _input: unknown, amount: bigint): LocalQuote {
    this.calls++;
    return { ok: true, amountIn: amount, amountOut: amount, computeUnits: 1, micros: 1 };
  }
}
const fakePool = (venue: QuotePool["venue"], staticAccounts: PublicKey[], extra: Partial<QuotePool> = {}): QuotePool => ({
  venue, address: key(), mintA: usdc.mint, mintB: usdt.mint, label: venue, feeBps: 0, staticAccounts,
  dynamicAccounts: () => [], tradable: () => true, swap: () => { throw new Error("not simulated here"); }, ...extra });
const live = () => {
  const market = new CountingMarket({} as Connection, usdc, usdt);
  const internals = market as unknown as { index(pool: QuotePool): void; changed(key: string): void; syncing: Set<string> };
  const at = (slot: bigint) => { market.mirror.svm.setClock(new Clock(slot, 0n, 0n, 0n, 0n)); market.prepare(); };
  return { market, internals, at };
};

test("live simulations are reused until an account the pool reads changes; mint and unrelated updates do not count", () => {
  const { market, internals, at } = live();
  try {
    const vault = key(), pool = fakePool("raydium-clmm", [vault, usdc.mint, usdt.mint]);
    internals.index(pool);
    at(100n);
    market.quote(pool, usdc, 1_000n); market.quote(pool, usdc, 1_000n);
    assert.equal(market.calls, 1);
    assert.deepEqual([market.hits, market.misses], [1, 1]);
    internals.changed(String(usdc.mint));
    internals.changed(String(key()));
    at(5_000n);
    market.quote(pool, usdc, 1_000n);
    assert.equal(market.calls, 1, "mint supply changes, unrelated accounts and time do not move a CLMM price");
    internals.changed(String(vault));
    market.quote(pool, usdc, 1_000n);
    assert.equal(market.calls, 2);
    market.quote(pool, usdc, 2_000n); market.quote(pool, usdt, 1_000n);
    assert.equal(market.calls, 4, "amount and input side are part of the key");
  } finally { market.close(); }
});

test("slot-sensitive pools re-simulate every slot, time-sensitive venues every 8 slots", () => {
  const { market, internals, at } = live();
  try {
    const oracle = fakePool("goonfi", [], { minQuoteSlot: () => 0n }), book = fakePool("manifest", []), clmm = fakePool("pancakeswap", []);
    for (const pool of [oracle, book, clmm]) internals.index(pool);
    const all = () => { for (const pool of [oracle, book, clmm]) market.quote(pool, usdc, 1_000n); };
    at(96n); all();
    assert.equal(market.calls, 3);
    at(97n); all();
    assert.equal(market.calls, 4, "only the slot-sensitive pool");
    at(104n); all();
    assert.equal(market.calls, 6, "the order book's 8-slot bucket changed too; the CLMM never re-simulates for time");
  } finally { market.close(); }
});

test("a pool waiting for newly needed accounts sits out instead of quoting stale state", () => {
  const { market, internals, at } = live();
  try {
    const pool = fakePool("whirlpool", [key(), key(), key(), key(), key(), key()]);
    internals.index(pool);
    at(10n);
    internals.syncing.add(String(pool.address));
    const q = market.quote(pool, usdc, 1_000n);
    assert.equal(q.ok, false);
    assert.match(q.error!, /resyncing/);
    assert.equal(market.calls, 0);
    const health = market.health();
    assert.equal(health.ok, false, "no snapshot, no clock and no tradable pools");
    assert.ok(health.reasons.some(r => /clock/.test(r)) && health.reasons.some(r => /no tradable pools/.test(r)));
  } finally { market.close(); }
});
