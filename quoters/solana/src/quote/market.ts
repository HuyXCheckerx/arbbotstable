import { existsSync } from "node:fs";
import { mkdir, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { NATIVE_MINT, TOKEN_2022_PROGRAM_ID } from "@solana/spl-token";
import { type Connection, PublicKey } from "@solana/web3.js";
import { address, lamports } from "@solana/kit";
import { Clock } from "litesvm";
import { Mirror } from "../market/mirror.js";
import type { TokenInfo } from "../tokens.js";
import { type PoolCandidate, bitmapExtensionFor, buildQuotePool, hydrateQuotePools } from "./discovery.js";
import type { QuoteMarket } from "./engine.js";
import { type QuotePool, type QuoteVenue, VENUE_PROGRAMS } from "./venues.js";

const UPGRADEABLE_LOADER = new PublicKey("BPFLoaderUpgradeab1e11111111111111111111111");
/** UpgradeableLoaderState::ProgramData header: u32 tag, u64 last deploy slot, Option<Pubkey> authority. */
const PROGRAM_DATA_HEADER = 45;
const LAST_RESTART_SLOT = new PublicKey("SysvarLastRestartS1ot1111111111111111111111");
/** Pairs in order of first appearance, either orientation counting as the same pair. */
const uniquePairs = (pairs: [TokenInfo, TokenInfo][]) => {
  const seen = new Set<string>();
  return pairs.filter(pair => { const key = pair.map(t => String(t.mint)).sort().join("/"); return !seen.has(key) && seen.add(key); });
};

/** One-shot RPC snapshots. No wallet key, executor artifact, aggregator API or websocket is needed.
 * With `streaming`, the mirror keeps its accounts current over websockets afterwards (see LiveQuoteMarket). */
export class SnapshotQuoteMarket implements QuoteMarket {
  readonly mirror: Mirror;
  /** Tokens the mirror owner holds: input, output and any two-hop intermediates. */
  readonly tokens: TokenInfo[];
  /** Pairs quoted directly: input/output and any further `options.pairs`, so one mirror can serve several pairs. */
  readonly served: [TokenInfo, TokenInfo][];
  pools: QuotePool[] = [];
  /** Every hydrated pool, tradable or not when the snapshot was taken. */
  protected all: QuotePool[] = [];
  warnings: string[] = [];
  /** Valid pools skipped because they had no active liquidity when hydrated. */
  emptyPools = 0;
  /** `quoteSlot`: the clock slot quotes run at (see pinClock); `clockSlot`: the chain slot when the clock was read. */
  snapshot?: { fetchedAt: string; commitment: "processed"; accountSlotMin: number; accountSlotMax: number;
    slotSpread: number; clockSlot: number; quoteSlot: number; accounts: number; singleBank: false };
  private readonly programLoads = new Map<string, Promise<void>>();
  private readonly programs: Map<string, PublicKey>;

  /** `programCache`: directory for deployed program binaries, keyed by program id and last deploy slot.
   * `via`: intermediate tokens; pools of input/intermediate and intermediate/output are then hydrated too. */
  constructor(protected readonly connection: Connection, readonly input: TokenInfo, readonly output: TokenInfo,
    private readonly programCache?: string, readonly via: TokenInfo[] = [],
    options: { streaming?: boolean; clockConnection?: Connection; pairs?: [TokenInfo, TokenInfo][] } = {}) {
    this.mirror = new Mirror(connection, undefined, options.streaming ? 1_500 : 0, options.clockConnection ?? connection, 64 * 1024,
      undefined, options.streaming ?? false);
    this.served = uniquePairs([[input, output], ...(options.pairs ?? [])]);
    this.tokens = [...new Map([...this.served.flat(), ...via].map(t => [String(t.mint), t])).values()];
    this.programs = new Map(this.tokens.map(t => [String(t.mint), t.program]));
  }

  /** Pairs a pool may trade: each served pair and, for each intermediate, its two legs. */
  get pairs(): [TokenInfo, TokenInfo][] {
    const all = this.served.flatMap(([a, b]) => [[a, b], ...this.via.filter(m => !m.mint.equals(a.mint) && !m.mint.equals(b.mint))
      .flatMap(m => [[a, m], [m, b]])] as [TokenInfo, TokenInfo][]);
    return uniquePairs(all);
  }

  /** Load the deployed programs `venues` need (and Token-2022). Independent of pool data, so it can run during discovery. */
  async loadPrograms(venues: Iterable<QuoteVenue>): Promise<void> {
    const ids = [...new Map([...new Set(venues)].flatMap(venue => VENUE_PROGRAMS[venue]).concat(TOKEN_2022_PROGRAM_ID)
      .map(id => [String(id), id])).values()];
    const fresh = ids.filter(id => !this.programLoads.has(String(id)));
    if (fresh.length) {
      const batch = this.programCache ? this.loadCachedPrograms(fresh, this.programCache) : this.mirror.loadPrograms(fresh);
      for (const id of fresh) this.programLoads.set(String(id), batch);
    }
    await Promise.all(ids.map(id => this.programLoads.get(String(id))));
  }

  /** A cached binary is used only while the programdata deploy slot still matches, so upgrades are always fetched. */
  private async loadCachedPrograms(ids: PublicKey[], dir: string): Promise<void> {
    const accounts = await this.connection.getMultipleAccountsInfo(ids);
    const upgradeable = ids.flatMap((id, i) => accounts[i]?.executable && accounts[i]!.owner.equals(UPGRADEABLE_LOADER)
      ? [{ id, programData: new PublicKey(accounts[i]!.data.subarray(4, 36)) }] : []);
    const others = ids.filter(id => !upgradeable.some(p => p.id.equals(id)));
    const headers = upgradeable.length ? await this.connection.getMultipleAccountsInfo(upgradeable.map(p => p.programData),
      { dataSlice: { offset: 0, length: PROGRAM_DATA_HEADER } }) : [];
    await mkdir(dir, { recursive: true });
    await Promise.all([others.length ? this.mirror.loadPrograms(others) : undefined, ...upgradeable.map(async ({ id, programData }, i) => {
      const header = headers[i]?.data;
      const cached = header?.length === PROGRAM_DATA_HEADER ? join(dir, `${id}-${header.readBigUInt64LE(4)}.so`) : undefined;
      if (cached && existsSync(cached)) return this.mirror.addProgramFile(id, cached);
      const account = await this.connection.getAccountInfo(programData);
      if (!account || account.data.length <= PROGRAM_DATA_HEADER) throw new Error(`Missing program data for ${id}`);
      const path = join(dir, `${id}-${account.data.readBigUInt64LE(4)}.so`), temporary = `${path}.${process.pid}.tmp`;
      await writeFile(temporary, account.data.subarray(PROGRAM_DATA_HEADER));
      await rename(temporary, path);
      this.mirror.addProgramFile(id, path);
    })]);
  }

  async start(candidates: PoolCandidate[], maxSlotSpread = 32): Promise<void> {
    if (!Number.isSafeInteger(maxSlotSpread) || maxSlotSpread < 0) throw new Error("Invalid maximum snapshot slot spread");
    const [hydrated] = await Promise.all([hydrateQuotePools(this.connection, candidates, this.pairs, { skipEmpty: true }),
      this.loadPrograms(candidates.map(p => p.venue))]);
    this.pools = hydrated.pools;
    this.warnings = hydrated.warnings;
    this.emptyPools = hydrated.empty;
    if (!this.pools.length) throw new Error(`No pools with active liquidity (${hydrated.empty} empty)${this.warnings.length ? `: ${this.warnings.join("; ")}` : ""}`);
    await this.mirror.fundOwner(this.tokens);
    this.fundNativeLamports();
    // One pass fetches everything the hydrated pool states need (tick arrays included), so it is itself a complete,
    // close-in-time refresh. Pool price can move into different tick arrays while it loads: then the new arrays are
    // fetched and everything is refreshed together before any quote is exposed.
    let keys = this.dependencies(key => hydrated.data.get(String(key)));
    await this.mirror.track(keys);
    for (let attempt = 0; attempt < 3; attempt++) {
      if (attempt > 0) {
        await this.mirror.track(keys);
        await this.mirror.refresh(keys);
      }
      this.rebuild();
      const required = this.dependencies();
      const present = new Set(keys.map(String));
      keys = required;
      if (required.some(key => !present.has(String(key)))) continue;
      await this.mirror.followClock();
      const range = this.mirror.accountSlotRange(keys);
      if (range.unknown || range.max - range.min > maxSlotSpread || Math.abs(this.mirror.chainSlot - range.min) > maxSlotSpread) {
        if (attempt < 2) continue;
        throw new Error(`Pool snapshot spans too many slots (${range.min}-${range.max}, clock ${this.mirror.chainSlot}); retry with a faster RPC`);
      }
      const quoteSlot = await this.pinClock(range.max, maxSlotSpread);
      const final = this.mirror.accountSlotRange(keys);
      this.snapshot = { fetchedAt: new Date().toISOString(), commitment: "processed", accountSlotMin: final.min,
        accountSlotMax: final.max, slotSpread: final.max - final.min, clockSlot: this.mirror.chainSlot, quoteSlot,
        accounts: keys.length, singleBank: false };
      this.all = this.pools;
      this.pools = this.pools.filter(pool => pool.tradable(this.mirror.data(pool.address)!));
      return;
    }
    throw new Error("Pool tick arrays changed repeatedly while loading; retry the quote");
  }

  /** A wrapped-SOL source account moves lamports along with its token balance, so the owner's needs matching lamports. */
  private fundNativeLamports(): void {
    if (!this.tokens.some(t => t.mint.equals(NATIVE_MINT))) return;
    const account = this.mirror.svm.getAccount(address(this.mirror.ata(NATIVE_MINT).toBase58()));
    if (!account.exists) throw new Error("Mirror wrapped SOL account missing after setup");
    this.mirror.svm.setAccount({ ...account, lamports: lamports(BigInt(account.lamports) + Mirror.ownerBalance) });
  }

  /**
   * Quote as of the accounts, not as of the later moment the clock was read. Oracle pools (BisonFi, Kipseli) price only
   * for a few slots after an update, and a multi-batch snapshot can span several slots, so their accounts are refreshed
   * last, in one request, and the clock moves to that request's slot (or to a later slot from which an update applies,
   * within `maxSlotSpread`), with its timestamp shifted 400 ms per slot. Without oracle pools the clock moves to the
   * newest account batch. A transaction built from the snapshot lands later than its slot, so the clock also moves past
   * slots in which a pool charges more because it already traded or took a price update there (GoonFi). The cluster's
   * last restart slot is mirrored too; LiteSVM defaults it to 0, and BisonFi refuses to price without it.
   */
  private async pinClock(dataSlot: number, maxSlotSpread: number): Promise<number> {
    const oracles = this.pools.filter(pool => pool.validFromSlot);
    const oracleKeys = [...new Map(oracles.flatMap(pool => [pool.address, ...pool.staticAccounts]).map(key => [String(key), key])).values()];
    const [restart] = await Promise.all([this.connection.getAccountInfo(LAST_RESTART_SLOT, "processed"),
      oracleKeys.length ? this.mirror.refresh(oracleKeys) : undefined]);
    if (restart && restart.data.length >= 8) this.mirror.svm.setLastRestartSlot(restart.data.readBigUInt64LE(0));
    const base = oracles.length ? this.mirror.accountSlotRange(oracles.map(pool => pool.address)).max : dataSlot;
    let slot = BigInt(base);
    for (const pool of oracles) {
      const from = pool.validFromSlot!(this.mirror.data(pool.address)!);
      if (from > slot && from <= BigInt(base + maxSlotSpread)) slot = from;
    }
    for (const pool of this.pools) {
      const from = pool.minQuoteSlot?.(this.mirror.data(pool.address)!, key => this.mirror.data(key));
      if (from !== undefined && from > slot && from <= BigInt(base + maxSlotSpread)) slot = from;
    }
    const clock = this.mirror.svm.getClock();
    const shift = BigInt(Math.round(Number(clock.slot - slot) * 0.4));
    this.mirror.svm.setClock(new Clock(slot, clock.epochStartTimestamp, clock.epoch, clock.leaderScheduleEpoch, clock.unixTimestamp - shift));
    return Number(slot);
  }

  private rebuild(): void {
    this.pools = this.pools.map(pool => {
      const data = this.mirror.data(pool.address);
      if (!data) throw new Error(`Pool ${pool.address} disappeared during snapshot loading`);
      const extension = bitmapExtensionFor(pool);
      return buildQuotePool(pool, data, extension && this.mirror.data(extension));
    });
  }

  protected dependencies(read: (key: PublicKey) => Buffer | undefined = key => this.mirror.data(key)): PublicKey[] {
    return [...new Map(this.pools.flatMap(pool => {
      const data = read(pool.address)!;
      return [pool.address, ...pool.staticAccounts, ...(pool.tradable(data) ? pool.dynamicAccounts(data) : [])];
    }).map(key => [String(key), key])).values()];
  }

  /** Simulate `amount` of `input` into `pool`; the output token is the pool's other mint. */
  quote(pool: QuoteMarket["pools"][number], input: TokenInfo, amount: bigint) {
    return this.simulate(this.pools.find(p => p.address.equals(pool.address)), input, amount);
  }

  protected simulate(model: QuotePool | undefined, input: TokenInfo, amount: bigint) {
    const traded = model && [model.mintA, model.mintB].some(mint => mint.equals(input.mint));
    const output = traded ? this.tokens.find(t => !t.mint.equals(input.mint) && (t.mint.equals(model.mintA) || t.mint.equals(model.mintB))) : undefined;
    if (!this.snapshot || !model || !output || !this.tokens.some(t => t.mint.equals(input.mint))) {
      throw new Error("Quote market is not ready for this input/pool");
    }
    const data = this.mirror.data(model.address)!;
    const programs = this.programs;
    const instruction = model.swap({ owner: this.mirror.owner, mintIn: input.mint, ataIn: this.mirror.ata(input.mint),
      ataOut: this.mirror.ata(output.mint), amount, minOut: 1n, data,
      tokenProgram: mint => {
        const program = programs.get(String(mint));
        if (!program) throw new Error(`No token program known for ${mint}`);
        return program;
      },
      exists: key => this.mirror.has(key) });
    return this.mirror.quote(instruction, input.mint, output.mint, amount);
  }

  close(): void { this.mirror.close(); }
}
