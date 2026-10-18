import { type Connection, PublicKey, SYSVAR_CLOCK_PUBKEY } from "@solana/web3.js";
import { Clock } from "litesvm";
import { type LocalQuote, base58 } from "../market/mirror.js";
import type { TokenInfo } from "../tokens.js";
import { type PoolCandidate, bitmapExtensionFor, buildQuotePool } from "./discovery.js";
import type { QuoteMarket } from "./engine.js";
import { SnapshotQuoteMarket } from "./market.js";
import type { QuotePool } from "./venues.js";

/** Simulations kept before stale entries are swept (each entry is a few hundred bytes). */
const MAX_CACHE = 200_000;
/** Venues whose price can move with time alone reuse a simulation for at most this many slots (2^3 = 8, ~3 s) without an
 * account change: Manifest orders can expire by slot, JupLend accrues interest, Deriverse is left in this class untested,
 * and adaptive-fee Whirlpools (those with an oracle account) decay their volatility fee with time. */
const SLOT_BUCKET_BITS = 3n;
const TIMED_VENUES = new Set<QuotePool["venue"]>(["manifest", "juplend", "deriverse"]);
/** How far ahead of the chain clock a quote may run to step past a slot a pool prices worse in (see prepare). */
const MAX_CLOCK_LEAD = 32n;

export interface LiveHealth {
  ok: boolean;
  /** Why `ok` is false, in words. */
  reasons: string[];
  chainSlot: number; clockSlot: number; clockAgeMs: number;
  streamLagSlots: number; streamStalled: boolean; lastAccountUpdateAgeMs: number;
  pools: number; syncing: number; tracked: number; subscriptions: number; polled: number;
  cache: { entries: number; hits: number; misses: number };
  startedAt: string;
}

/**
 * A quote market kept current over websockets: accounts stream into the mirror after one consistent snapshot, the
 * clock follows the chain, and accounts too large to stream are polled. Each pool's simulations are cached by
 * (pool, input mint, amount) and stay valid until an account the pool reads changes, and also: for slot-sensitive pools
 * (oracle pools with `validFromSlot`, and those with `minQuoteSlot`) until the slot changes, for venues whose price can
 * move with time (TIMED_VENUES, adaptive-fee Whirlpools) for at most 8 slots. When a pool account changes,
 * its model is rebuilt; if it now needs accounts not yet mirrored (new tick arrays), it sits out until they arrive.
 * Call `prepare()` before each search.
 */
export class LiveQuoteMarket extends SnapshotQuoteMarket {
  private readonly versions = new Map<string, number>();
  /** Account → pools that read it. Token mints are left out: supply changes do not move prices. */
  private readonly dependents = new Map<string, Set<string>>();
  private readonly deps = new Map<string, string[]>();
  /** Accounts whose change means a pool's model must be rebuilt: the pool itself and Raydium-style bitmap extensions. */
  private readonly rebuildOn = new Map<string, string>();
  private readonly models = new Map<string, QuotePool>();
  private readonly syncing = new Set<string>();
  private readonly cache = new Map<string, { version: string; quote: LocalQuote }>();
  /** How each pool's price depends on the clock: every slot, within a slot bucket, or not at all (see quote). */
  private readonly timing = new Map<string, "slot" | "bucket" | "none">();
  private readonly mints: Set<string>;
  private readonly pollMs: number;
  private quoteSlot = 0n;
  private candidates = new Set<string>();
  private readonly startedAt = new Date().toISOString();
  hits = 0;
  misses = 0;

  constructor(connection: Connection, input: TokenInfo, output: TokenInfo, programCache?: string, via: TokenInfo[] = [],
    options: { clockConnection?: Connection; pollMs?: number; pairs?: [TokenInfo, TokenInfo][] } = {}) {
    super(connection, input, output, programCache, via, { streaming: true, clockConnection: options.clockConnection, pairs: options.pairs });
    this.mints = new Set(this.tokens.map(t => String(t.mint)));
    this.pollMs = options.pollMs ?? 1_000;
  }

  override async start(candidates: PoolCandidate[], maxSlotSpread = 32): Promise<void> {
    this.candidates = new Set(candidates.map(pool => String(pool.address)));
    await super.start(candidates, maxSlotSpread);
    for (const pool of this.all) this.index(pool);
    this.activate();
    this.mirror.onChange(key => this.changed(key));
    this.mirror.startPolling(this.pollMs);
  }

  /** Point the clock at the slot the next search should price: the chain's, moved past slots in which a pool charges
   * more (GoonFi's last trade and price update) or to an oracle pool's later valid-from slot. Returns that slot. */
  prepare(): number {
    const clock = this.mirror.svm.getClock(), read = (key: PublicKey) => this.mirror.data(key);
    let slot = clock.slot;
    for (const pool of this.pools) {
      const data = this.mirror.data(pool.address);
      if (!data) continue;
      for (const from of [pool.validFromSlot?.(data), pool.minQuoteSlot?.(data, read)]) {
        if (from !== undefined && from > slot && from <= clock.slot + MAX_CLOCK_LEAD) slot = from;
      }
    }
    if (slot !== clock.slot) {
      const lead = BigInt(Math.round(Number(slot - clock.slot) * 0.4));
      this.mirror.svm.setClock(new Clock(slot, clock.epochStartTimestamp, clock.epoch, clock.leaderScheduleEpoch, clock.unixTimestamp + lead));
    }
    this.quoteSlot = slot;
    return Number(slot);
  }

  override quote(pool: QuoteMarket["pools"][number], input: TokenInfo, amount: bigint): LocalQuote {
    const key = base58(pool.address), model = this.models.get(key);
    if (!model || this.syncing.has(key)) {
      return { ok: false, amountIn: 0n, amountOut: 0n, computeUnits: 0, micros: 0, error: "pool is resyncing" };
    }
    const timing = this.timing.get(key);
    const version = `${this.versions.get(key) ?? 0}@${timing === "slot" ? this.quoteSlot : timing === "bucket" ? this.quoteSlot >> SLOT_BUCKET_BITS : ""}`;
    const id = `${key}|${base58(input.mint)}|${amount}`, hit = this.cache.get(id);
    if (hit && hit.version === version) { this.hits++; return hit.quote; }
    this.misses++;
    const quote = this.simulate(model, input, amount);
    if (this.cache.size >= MAX_CACHE) this.sweep();
    this.cache.set(id, { version, quote });
    return quote;
  }

  health(limits: { maxLagSlots?: number; maxClockAgeMs?: number } = {}): LiveHealth {
    const { maxLagSlots = 8, maxClockAgeMs = 3_000 } = limits, m = this.mirror, reasons: string[] = [];
    const lag = m.streamLagSlots, clockAgeMs = Math.round(m.clockAgeMs);
    if (m.streamStalled) reasons.push("account stream stalled");
    else if (lag > maxLagSlots) reasons.push(`account stream ${lag} slots behind`);
    if (!(clockAgeMs <= maxClockAgeMs)) reasons.push(`clock not updated for ${clockAgeMs} ms`);
    if (!this.pools.length) reasons.push("no tradable pools");
    return { ok: !reasons.length, reasons, chainSlot: m.chainSlot, clockSlot: Number(m.svm.getClock().slot), clockAgeMs,
      streamLagSlots: lag, streamStalled: m.streamStalled, lastAccountUpdateAgeMs: m.lastUpdate ? Date.now() - m.lastUpdate : -1,
      pools: this.pools.length, syncing: this.syncing.size, tracked: m.trackedCount, subscriptions: m.subscriptionCount,
      polled: m.polledCount, cache: { entries: this.cache.size, hits: this.hits, misses: this.misses }, startedAt: this.startedAt };
  }

  /** Re-read every account a pool depends on over HTTP. Websocket notifications can be dropped without notice; changed
   * accounts flow through the same change handling as streamed ones. */
  async resync(): Promise<void> {
    await this.mirror.refresh([...new Set([...this.deps.values()].flat())].map(key => new PublicKey(key)));
  }

  /** Candidate addresses this market was built from (pools without liquidity included), to tell whether a fresh
   * discovery found anything new. */
  get addresses(): Set<string> { return this.candidates; }

  private changed(key: string): void {
    if (key === SYSVAR_CLOCK_PUBKEY.toBase58()) return;
    const pools = this.dependents.get(key);
    if (!pools) return;
    let rebuilt = false;
    for (const address of [...pools]) {
      this.versions.set(address, (this.versions.get(address) ?? 0) + 1);
      if (this.rebuildOn.get(key) === address) { this.rebuild1(address); rebuilt = true; }
    }
    if (rebuilt) this.activate();
  }

  /** Rebuild one pool's model from its new account data; new dependencies are mirrored before it quotes again. */
  private rebuild1(address: string): void {
    const previous = this.models.get(address)!, data = this.mirror.data(previous.address);
    if (!data) { this.syncing.add(address); return; }
    let model: QuotePool;
    try {
      const extension = bitmapExtensionFor(previous);
      model = buildQuotePool(previous, data, extension && this.mirror.data(extension));
    } catch { this.syncing.add(address); return; }
    this.models.set(address, model);
    this.index(model);
    const missing = this.deps.get(address)!.filter(key => !this.mirror.isTracked(key));
    if (!missing.length) { this.syncing.delete(address); return; }
    this.syncing.add(address);
    this.mirror.track(missing.map(key => new PublicKey(key))).then(() => {
      if (this.deps.get(address)!.every(key => this.mirror.isTracked(key))) this.syncing.delete(address);
      this.versions.set(address, (this.versions.get(address) ?? 0) + 1);
      this.activate();
    }, () => undefined);
  }

  private index(pool: QuotePool): void {
    const address = String(pool.address), data = this.mirror.data(pool.address);
    for (const key of this.deps.get(address) ?? []) this.dependents.get(key)?.delete(address);
    const keys = [pool.address, ...pool.staticAccounts, ...(data && pool.tradable(data) ? pool.dynamicAccounts(data) : [])]
      .map(String).filter(key => !this.mints.has(key));
    this.deps.set(address, keys);
    for (const key of keys) {
      if (!this.dependents.has(key)) this.dependents.set(key, new Set());
      this.dependents.get(key)!.add(address);
    }
    this.models.set(address, pool);
    // whirlpoolModel lists the pool's oracle PDA sixth; only adaptive-fee pools have that account.
    this.timing.set(address, pool.validFromSlot !== undefined || pool.minQuoteSlot !== undefined ? "slot"
      : TIMED_VENUES.has(pool.venue) || (pool.venue === "whirlpool" && this.mirror.has(pool.staticAccounts[5])) ? "bucket" : "none");
    this.rebuildOn.set(address, address);
    const extension = bitmapExtensionFor(pool);
    if (extension) this.rebuildOn.set(String(extension), address);
    if (!this.versions.has(address)) this.versions.set(address, 0);
  }

  /** The pools searches see: tradable now and not waiting for accounts. */
  private activate(): void {
    this.pools = [...this.models.values()].filter(pool => {
      const data = this.mirror.data(pool.address);
      return data !== undefined && !this.syncing.has(String(pool.address)) && pool.tradable(data);
    });
  }

  private sweep(): void {
    for (const [id, entry] of this.cache) {
      const address = id.slice(0, id.indexOf("|"));
      if (!entry.version.startsWith(`${this.versions.get(address) ?? 0}@`)) this.cache.delete(id);
    }
    if (this.cache.size >= MAX_CACHE / 2) this.cache.clear();
  }
}
