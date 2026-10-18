import { AddressLookupTableAccount, type AccountInfo, ComputeBudgetProgram, type Connection, Keypair, PublicKey, SYSVAR_CLOCK_PUBKEY,
  type TransactionInstruction, TransactionMessage, VersionedTransaction } from "@solana/web3.js";
import { createAssociatedTokenAccountIdempotentInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { type Transaction, address, getTransactionDecoder, lamports } from "@solana/kit";
import { Clock, FailedTransactionMetadata, LiteSVM, type SimulatedTransactionInfo } from "litesvm";
import type { TokenInfo } from "../tokens.js";
import { GeyserFeed, type GeyserOptions } from "./geyser.js";

const UPGRADEABLE_LOADER = new PublicKey("BPFLoaderUpgradeab1e11111111111111111111111");
const LEGACY_LOADERS = new Set(["BPFLoader2111111111111111111111111111111111", "BPFLoader1111111111111111111111111111111111"]);
const TOKEN_2022 = new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
/** Programs LiteSVM provides itself (Token-2022 is deliberately loaded from mainnet instead). */
const BUILTINS = new Set(["11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
  "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "ComputeBudget111111111111111111111111111111",
  "AddressLookupTab1e1111111111111111111111111"]);
/** Balance given to the owner's token accounts in the mirror: large enough that a quote never runs dry. */
const MIRROR_BALANCE = 1_000_000_000_000_000n;

const names = new WeakMap<PublicKey, string>();
/** A key's base58 string, memoized per PublicKey object. Quote searches build lookup keys for every simulation from the
 * same few key objects, and encoding them each time is a tenth of a search's CPU. */
export function base58(key: PublicKey): string {
  let name = names.get(key);
  if (name === undefined) names.set(key, name = key.toBase58());
  return name;
}

export interface LocalQuote { ok: boolean; amountIn: bigint; amountOut: bigint; computeUnits: number; error?: string; micros: number }

/**
 * A local copy of mainnet: DEX and router programs (loaded from chain) plus every account the
 * tracked pools and routes need, kept current through websocket subscriptions. Quoting a swap
 * executes the real programs against that state in LiteSVM, so the result is exact for any venue
 * with no reimplemented pool math. Signatures are not verified locally, so quotes can be made for
 * the trading wallet's address without its key. Nothing here sends anything.
 */
export class Mirror {
  readonly svm = new LiteSVM().withSigverify(false).withBlockhashCheck(false);
  /** Owner of the mirror's token accounts: the trading wallet (live) or a throwaway address. */
  readonly owner: PublicKey;
  private readonly atas = new Map<string, PublicKey>();
  /** The owner's token accounts hold synthetic balances and are never overwritten from chain. */
  private readonly reserved = new Set<string>();
  private readonly tracked = new Set<string>();
  private trackQueue: Promise<void> = Promise.resolve();
  private readonly accountSlots = new Map<string, { slot: number; writeVersion?: bigint }>();
  /** Compiled [compute limit, swap] transactions by swap account list and data length (see swapTransaction). */
  private readonly templates = new Map<string, { blockhash: string; tx: Transaction; dataAt: number }>();
  private readonly geyser?: GeyserFeed;
  private appliedClockSlot = -1n;
  private readonly exists = new Set<string>();
  private readonly programs = new Set<string>();
  private readonly subscriptions = new Map<string, number>();
  private listeners: ((key: string) => void)[] = [];
  private disposed = false;
  updates = 0;
  lastUpdate = 0;
  /** Accounts too large to stream (refreshed by polling and right before verification instead). */
  private readonly polled = new Set<string>();
  private pollTimer?: NodeJS.Timeout;
  private polling = false;
  /** Current slot, from a dedicated connection so it never queues behind account data. */
  chainSlot = 0;
  private readonly lags: number[] = [];
  private clockSubscription?: number;
  private slotSubscription?: number;
  private canarySubscription?: number;
  private canaryAt = 0;
  private canaryStartedAt = 0;
  private lastClockAt = 0;
  private clockPollTimer?: NodeJS.Timeout;
  private clockPolling = false;
  private fullPollTimer?: NodeJS.Timeout;
  private fullPolling = false;
  private lastFullPollSlot = 0;

  /**
   * @param clockConnection a separate Connection (its own websocket) for the clock and slot: on a
   *   shared socket, large account updates queue ahead of them and the clock falls minutes behind.
   * @param maxStreamBytes accounts larger than this are polled instead of streamed; one 800 KB order
   *   book updating every slot is ~1 MB/s and backs up every other notification.
   */
  constructor(private readonly connection: Connection, owner?: PublicKey, private readonly maxSubscriptions = 1_500,
    private readonly clockConnection: Connection = connection, private readonly maxStreamBytes = 64 * 1024, geyser?: GeyserOptions,
    private readonly streaming = true) {
    this.owner = owner ?? Keypair.generate().publicKey;
    if (geyser) this.geyser = new GeyserFeed({ ...geyser, resync: async () => {
      await this.refresh([...this.tracked].filter((key) => !this.programs.has(key)).map((key) => new PublicKey(key)));
      await geyser.resync?.();
    } }, ({ key, account, slot, writeVersion }) => {
      if (this.disposed || !this.acceptsAccount(key, slot, writeVersion)) return false;
      if (key === SYSVAR_CLOCK_PUBKEY.toBase58()) this.applyClock(account.data);
      else if (this.store(key, account, slot, writeVersion)) { this.updates++; this.lastUpdate = Date.now(); this.emitChange(key); }
      return true;
    });
  }

  get geyserStatus(): { enabled: false } | GeyserFeed["status"] { return this.geyser?.status ?? { enabled: false }; }

  /**
   * How far behind the account stream is: current slot minus the slot of recent notifications
   * (worst of the last 50). Above a few slots, mirrored state is stale and quotes are optimistic.
   */
  get streamLagSlots(): number {
    if (this.geyser?.healthy) return Math.max(0, this.chainSlot - this.geyser.status.lastSlot);
    if (!this.streamStalled) return this.lags.length ? Math.max(...this.lags) : 0;
    // Websocket silent: the mirror is only as fresh as the last full HTTP poll (if one ran).
    return this.lastFullPollSlot ? Math.max(0, this.chainSlot - this.lastFullPollSlot) : Number.MAX_SAFE_INTEGER;
  }

  /**
   * True when the account websocket has gone quiet: a canary (the 40-byte Clock account, which changes
   * every slot) is subscribed on the same socket, so silence means a dead or backed-up stream, not a
   * quiet market. Without this, a mirror that receives nothing would report zero lag.
   */
  get streamStalled(): boolean {
    if (this.geyser?.healthy) return false;
    return this.canaryStartedAt > 0 && Date.now() - this.canaryStartedAt > 5_000 && Date.now() - this.canaryAt > 3_000;
  }
  get polledCount(): number { return this.polled.size; }

  get subscriptionCount(): number { return this.subscriptions.size; }
  get trackedCount(): number { return this.tracked.size; }
  isTracked(key: string): boolean { return this.tracked.has(key) || this.reserved.has(key) || BUILTINS.has(key) || key.startsWith("Sysvar"); }
  /** Milliseconds since the Clock sysvar was last applied (streamed or polled); Infinity before the first. */
  get clockAgeMs(): number { return this.lastClockAt ? Date.now() - this.lastClockAt : Number.POSITIVE_INFINITY; }
  get programCount(): number { return this.programs.size; }

  onChange(listener: (key: string) => void): void { if (!this.disposed) this.listeners.push(listener); }

  private emitChange(key: string): void {
    for (const listener of this.listeners) {
      if (this.disposed) break;
      listener(key);
    }
  }

  private async loadProgram(key: string, account: AccountInfo<Buffer>): Promise<void> {
    if (this.disposed || BUILTINS.has(key) || this.programs.has(key)) return;
    if (account.owner.equals(UPGRADEABLE_LOADER)) {
      const data = await this.connection.getAccountInfo(new PublicKey(account.data.subarray(4, 36)));
      if (this.disposed) return;
      if (!data) throw new Error(`Missing program data for ${key}`);
      this.svm.addProgram(address(key), data.data.subarray(45));
    } else if (LEGACY_LOADERS.has(account.owner.toBase58())) {
      this.svm.addProgram(address(key), account.data);
    } else return;
    this.programs.add(key);
  }

  /** Load programs from chain so quotes run the exact deployed code. */
  async loadPrograms(programs: PublicKey[]): Promise<void> {
    if (this.disposed) return;
    // Token-2022 too: LiteSVM's bundled copy can lag behind extensions used by PYUSD/USDG-style mints.
    for (const id of [...programs, TOKEN_2022]) {
      const account = await this.connection.getAccountInfo(id);
      if (this.disposed) return;
      if (account?.executable) await this.loadProgram(id.toBase58(), account);
      if (this.disposed) return;
      this.tracked.add(id.toBase58());
    }
  }

  /** Create the owner's token accounts for `tokens` in the mirror, funded with a large balance. */
  async fundOwner(tokens: TokenInfo[]): Promise<void> {
    if (this.disposed) return;
    await this.track(tokens.map((t) => t.mint));
    if (this.disposed) return;
    this.svm.airdrop(address(this.owner.toBase58()), lamports(100_000_000_000n));
    const create = tokens.map((t) => {
      const ata = getAssociatedTokenAddressSync(t.mint, this.owner, true, t.program);
      this.atas.set(t.mint.toBase58(), ata);
      this.reserved.add(ata.toBase58());
      return createAssociatedTokenAccountIdempotentInstruction(this.owner, ata, this.owner, t.mint, t.program);
    });
    const result = this.svm.sendTransaction(this.transaction(create));
    if (result instanceof FailedTransactionMetadata) throw new Error(`Mirror token account setup failed: ${String(result.err())}`);
    for (const ata of this.atas.values()) {
      const account = this.svm.getAccount(address(ata.toBase58()));
      if (!account.exists) throw new Error("Mirror token account missing after setup");
      const data = Buffer.from(account.data);
      data.writeBigUInt64LE(MIRROR_BALANCE, 64);
      this.svm.setAccount({ ...account, data });
    }
  }

  ata(mint: PublicKey): PublicKey {
    const ata = this.atas.get(base58(mint));
    if (!ata) throw new Error(`Mirror has no token account for ${mint.toBase58()}`);
    return ata;
  }

  has(key: PublicKey): boolean { return this.exists.has(key.toBase58()); }

  data(key: PublicKey): Buffer | undefined {
    const account = this.svm.getAccount(address(key.toBase58()));
    return account.exists ? Buffer.from(account.data) : undefined;
  }

  /** Slot range of the RPC account batches installed in this local view; not a single-bank guarantee. */
  accountSlotRange(keys: PublicKey[]): { min: number; max: number; unknown: number } {
    const slots = [...new Set(keys.map(String))].map(key => this.accountSlots.get(key)?.slot ?? 0);
    const known = slots.filter(slot => slot > 0);
    return { min: known.length ? Math.min(...known) : 0, max: known.length ? Math.max(...known) : 0,
      unknown: slots.length - known.length };
  }

  lookupTable(key: PublicKey): AddressLookupTableAccount | undefined {
    const data = this.data(key);
    return data ? new AddressLookupTableAccount({ key, state: AddressLookupTableAccount.deserialize(data) }) : undefined;
  }

  private acceptsAccount(key: string, slot: number, writeVersion?: bigint): boolean {
    const previous = this.accountSlots.get(key);
    if (!previous) return true;
    if (slot < previous.slot) return false;
    if (slot === previous.slot && previous.writeVersion !== undefined
      && (writeVersion === undefined || writeVersion <= previous.writeVersion)) return false;
    return true;
  }

  private store(key: string, account: AccountInfo<Buffer> | null, slot = 0, writeVersion?: bigint): boolean {
    if (this.disposed || this.reserved.has(key)) return false;
    if (!this.acceptsAccount(key, slot, writeVersion)) return false;
    if (slot > 0) this.accountSlots.set(key, { slot, writeVersion });
    const previous = this.svm.getAccount(address(key));
    const present = !!account && account.lamports > 0;
    const changed = previous.exists !== present || (present && previous.exists && (
      !Buffer.from(previous.data).equals(account!.data) || BigInt(previous.lamports) !== BigInt(account!.lamports)
      || previous.executable !== account!.executable || String(previous.programAddress) !== account!.owner.toBase58()));
    if (!changed) return false;
    if (account && account.lamports > 0) {
      this.svm.setAccount({ address: address(key), executable: account.executable, lamports: lamports(BigInt(account.lamports)),
        programAddress: address(account.owner.toBase58()), data: account.data, space: BigInt(account.data.length) });
      this.exists.add(key);
    } else if (this.exists.delete(key)) {
      this.svm.setAccount({ address: address(key), executable: false, lamports: lamports(0n),
        programAddress: address("11111111111111111111111111111111"), data: new Uint8Array(), space: 0n });
    }
    return true;
  }

  private async fetchAccounts(keys: PublicKey[]): Promise<{ accounts: (AccountInfo<Buffer> | null)[]; slot: number }> {
    if (typeof this.connection.getMultipleAccountsInfoAndContext === "function") {
      const result = await this.connection.getMultipleAccountsInfoAndContext(keys, "processed");
      return { accounts: result.value, slot: result.context.slot };
    }
    return { accounts: await this.connection.getMultipleAccountsInfo(keys, "processed"), slot: 0 };
  }

  /**
   * Fetch accounts not yet tracked and keep them current. Programs are loaded (not watched);
   * missing accounts are watched too, since they may be created (e.g. a new tick array).
   */
  async track(keys: PublicKey[]): Promise<string[]> {
    // Pool notifications and discovery can request the same new tick arrays concurrently.
    // Serialize registration so a second caller cannot install unowned duplicate subscriptions.
    const pending = this.trackQueue.then(() => this.trackNew(keys));
    this.trackQueue = pending.then(() => undefined, () => undefined);
    return pending;
  }

  private async trackNew(keys: PublicKey[]): Promise<string[]> {
    if (this.disposed) return [];
    const fresh = [...new Set(keys.map(String))].filter((k) => !this.tracked.has(k) && !this.reserved.has(k)
      && !BUILTINS.has(k) && !k.startsWith("Sysvar"));
    const watch: string[] = [];
    const changed: string[] = [];
    try {
      for (let i = 0; i < fresh.length; i += 100) {
        const batch = fresh.slice(i, i + 100);
        const { accounts, slot } = await this.fetchAccounts(batch.map((k) => new PublicKey(k)));
        if (this.disposed) return [];
        for (const [j, key] of batch.entries()) {
          const account = accounts[j];
          if (account?.executable) {
            await this.loadProgram(key, account);
            if (this.disposed) return [];
          }
          else {
            if (this.store(key, account, slot)) changed.push(key);
            if (account && account.data.length > this.maxStreamBytes) this.polled.add(key);
            else watch.push(key);
          }
          this.tracked.add(key);
        }
      }
    } finally {
      for (const key of watch) {
        if (!this.streaming || this.disposed || this.subscriptions.size >= this.maxSubscriptions) break;
        const id = this.connection.onAccountChange(new PublicKey(key), (account, context) => {
          if (this.disposed || this.geyser?.healthy || !this.acceptsAccount(key, context.slot)) return;
          // A write that leaves the account identical still notifies; listeners hear only real changes, as from Geyser.
          const changed = this.store(key, account, context.slot);
          this.updates++;
          this.lastUpdate = Date.now();
          if (this.chainSlot) {
            this.lags.push(Math.max(0, this.chainSlot - context.slot));
            if (this.lags.length > 50) this.lags.shift();
          }
          if (changed) this.emitChange(key);
        }, { commitment: "processed" });
        this.subscriptions.set(key, id);
      }
      this.geyser?.setAccounts([...this.tracked].filter((key) => !this.programs.has(key) && !this.reserved.has(key)));
      for (const key of changed) this.emitChange(key);
    }
    return fresh;
  }

  /**
   * Keep the mirror's clock on the current slot (order expiry, oracle freshness and time-based fees
   * read it), over the dedicated clock connection.
   */
  private applyClock(data: Buffer): void {
      if (this.disposed || data.length < 40) return;
      const slot = data.readBigUInt64LE(0);
      if (slot < this.appliedClockSlot) return;
      this.appliedClockSlot = slot;
      this.lastClockAt = Date.now();
      if (Number(slot) >= this.chainSlot) this.chainSlot = Number(slot);
      this.svm.setClock(new Clock(slot, data.readBigInt64LE(8), data.readBigUInt64LE(16), data.readBigUInt64LE(24), data.readBigInt64LE(32)));
      this.emitChange(SYSVAR_CLOCK_PUBKEY.toBase58());
  }

  async followClock(): Promise<void> {
    if (this.disposed) return;
    const clock = await this.clockConnection.getAccountInfo(SYSVAR_CLOCK_PUBKEY, "processed");
    if (this.disposed) return;
    if (!clock) throw new Error("Could not read the Clock sysvar");
    this.applyClock(clock.data);
    if (!this.streaming) return;
    this.clockSubscription = this.clockConnection.onAccountChange(SYSVAR_CLOCK_PUBKEY, (account) => this.applyClock(account.data), { commitment: "processed" });
    this.slotSubscription = this.clockConnection.onSlotChange((info) => { if (!this.disposed && info.slot > this.chainSlot) this.chainSlot = info.slot; });
    // If the clock's websocket is silent (endpoint without websockets, or a dropped socket), poll it over HTTP.
    this.clockPollTimer = setInterval(() => {
      if (this.disposed || this.geyser?.healthy || this.clockPolling || Date.now() - this.lastClockAt < 1_500) return;
      this.clockPolling = true;
      this.clockConnection.getAccountInfo(SYSVAR_CLOCK_PUBKEY, "processed").then((a) => { if (a) this.applyClock(a.data); })
        .catch(() => undefined).finally(() => { this.clockPolling = false; });
    }, 400);
    this.clockPollTimer.unref();
    // Canary on the data socket (see streamStalled).
    this.canaryStartedAt = Date.now();
    this.canarySubscription = this.connection.onAccountChange(SYSVAR_CLOCK_PUBKEY, () => { if (!this.disposed) this.canaryAt = Date.now(); }, { commitment: "processed" });
    this.geyser?.start();
  }

  /**
   * Fallback when the account websocket is stalled: re-fetch every tracked account over HTTP
   * (100 per call) and notify listeners about the ones that changed. Slower than streaming, but it
   * keeps the mirror usable on an endpoint without websockets; trades are still re-verified on fresh state.
   */
  private async pollAll(): Promise<void> {
    if (this.disposed || this.fullPolling) return;
    this.fullPolling = true;
    try {
      const slot = this.chainSlot;
      const keys = [...this.tracked].filter((k) => !this.reserved.has(k) && !this.programs.has(k) && !BUILTINS.has(k) && !k.startsWith("Sysvar"));
      const changed: string[] = [];
      for (let i = 0; i < keys.length; i += 100) {
        const batch = keys.slice(i, i + 100);
        const { accounts, slot: accountSlot } = await this.fetchAccounts(batch.map((k) => new PublicKey(k)));
        if (this.disposed) return;
        batch.forEach((key, j) => {
          const after = accounts[j];
          if (this.store(key, after, accountSlot)) changed.push(key);
        });
      }
      this.lastFullPollSlot = slot;
      this.updates += changed.length;
      for (const key of changed) this.emitChange(key);
    } finally {
      this.fullPolling = false;
    }
  }

  /** Re-fetch accounts now (large accounts, or everything a trade touches right before it is verified). */
  async refresh(keys: PublicKey[]): Promise<void> {
    if (this.disposed) return;
    const wanted = [...new Set(keys.map(String))].filter((k) => this.tracked.has(k) && !this.reserved.has(k) && !this.programs.has(k)
      && !BUILTINS.has(k) && !k.startsWith("Sysvar"));
    const changed: string[] = [];
    try {
      for (let i = 0; i < wanted.length; i += 100) {
        const batch = wanted.slice(i, i + 100);
        const { accounts, slot } = await this.fetchAccounts(batch.map((k) => new PublicKey(k)));
        if (this.disposed) return;
        batch.forEach((key, j) => { if (this.store(key, accounts[j], slot)) changed.push(key); });
      }
    } finally {
      // Also invalidate already-applied batches if a later RPC request fails.
      this.updates += changed.length;
      for (const key of changed) this.emitChange(key);
    }
  }

  /** Poll the accounts too large to stream, every `intervalMs`. */
  startPolling(intervalMs: number): void {
    if (this.disposed) return;
    this.pollTimer = setInterval(() => {
      if (this.disposed || this.geyser?.healthy || this.polling || !this.polled.size) return;
      this.polling = true;
      const keys = [...this.polled].map((k) => new PublicKey(k));
      this.refresh(keys)
        .catch(() => undefined).finally(() => { this.polling = false; });
    }, intervalMs);
    this.pollTimer.unref();
    this.fullPollTimer = setInterval(() => { if (this.streamStalled) this.pollAll().catch(() => undefined); }, 1_000);
    this.fullPollTimer.unref();
  }

  /** Unsigned transaction (signatures are not verified locally). */
  private transaction(instructions: TransactionInstruction[], tables: AddressLookupTableAccount[] = []) {
    const tx = new VersionedTransaction(new TransactionMessage({ payerKey: this.owner,
      recentBlockhash: this.svm.latestBlockhash(), instructions }).compileToV0Message(tables));
    return getTransactionDecoder().decode(tx.serialize());
  }

  /**
   * [compute limit, swap] as a transaction. Compiling a message costs more than simulating most swaps, and quotes
   * repeat one pool's instruction with only its data (the amount) changed, so the compiled message is kept per
   * account list and data length, and later calls patch the data in place. The swap's data ends the message,
   * followed by an empty lookup-table list.
   */
  private swapTransaction(swap: TransactionInstruction): Transaction {
    const blockhash = this.svm.latestBlockhash();
    const key = `${base58(swap.programId)}|${swap.data.length}|${swap.keys.map((k) => `${base58(k.pubkey)}${k.isSigner ? "s" : ""}${k.isWritable ? "w" : ""}`).join(",")}`;
    const cached = this.templates.get(key);
    if (cached && cached.blockhash === blockhash) {
      const messageBytes = new Uint8Array(cached.tx.messageBytes);
      messageBytes.set(swap.data, cached.dataAt);
      return { ...cached.tx, messageBytes: messageBytes as unknown as Transaction["messageBytes"] };
    }
    const tx = this.transaction([ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }), swap]);
    const dataAt = tx.messageBytes.length - 1 - swap.data.length;
    if (tx.messageBytes[tx.messageBytes.length - 1] === 0 && Buffer.from(tx.messageBytes.subarray(dataAt, dataAt + swap.data.length)).equals(swap.data)) {
      if (this.templates.size >= 4_096) this.templates.clear();
      this.templates.set(key, { blockhash, tx, dataAt });
    }
    return tx;
  }

  /**
   * Execute `swap` (built for the mirror owner's token accounts) without changing state, and report
   * exactly how much went in and came out. A partial fill is reported as not ok.
   */
  quote(swap: TransactionInstruction, mintIn: PublicKey, mintOut: PublicKey, amount: bigint,
    tables: AddressLookupTableAccount[] = []): LocalQuote {
    const started = performance.now();
    let result;
    try {
      result = this.svm.simulateTransaction(tables.length ? this.transaction(
        [ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }), swap], tables) : this.swapTransaction(swap));
    } catch (error) {
      return { ok: false, amountIn: 0n, amountOut: 0n, computeUnits: 0, error: (error as Error).message, micros: 0 };
    }
    const micros = Math.round((performance.now() - started) * 1000);
    if (result instanceof FailedTransactionMetadata) {
      const logs = result.meta().logs();
      const reason = logs.find((l) => /Error Message:|Error:/.test(l)) ?? logs.filter((l) => /failed/.test(l)).pop() ?? String(result.err());
      return { ok: false, amountIn: 0n, amountOut: 0n, computeUnits: Number(result.meta().computeUnitsConsumed()), error: reason, micros };
    }
    const [after, received] = this.postBalances(result, [this.ata(mintIn), this.ata(mintOut)]);
    const amountIn = MIRROR_BALANCE - after;
    const amountOut = received - MIRROR_BALANCE;
    const ok = amountIn === amount && amountOut > 0n;
    return { ok, amountIn, amountOut, computeUnits: Number(result.meta().computeUnitsConsumed()), micros,
      error: ok ? undefined : `partial fill: ${amountIn} of ${amount} in` };
  }

  /**
   * Token balances of `keys` after a simulation. LiteSVM's postAccounts() copies every written account into JS, which for
   * a venue writing large accounts (Deriverse's ~800 KB maps) costs more than the swap; its inner result yields addresses
   * first, so only the accounts asked for are copied. Falls back to postAccounts() if that shape ever changes.
   */
  private postBalances(result: SimulatedTransactionInfo, keys: PublicKey[]): bigint[] {
    const inner = (result as unknown as { inner?: { postAccounts?: () => { address: Uint8Array; account(): { data(): Uint8Array } }[] } }).inner;
    if (typeof inner?.postAccounts === "function") {
      const wanted = keys.map((key) => key.toBuffer()), found: bigint[] = keys.map(() => MIRROR_BALANCE);
      for (const entry of inner.postAccounts()) {
        const i = wanted.findIndex((bytes) => bytes.equals(entry.address));
        if (i >= 0) found[i] = Buffer.from(entry.account().data()).readBigUInt64LE(64);
      }
      return found;
    }
    const post = result.postAccounts();
    return keys.map((key) => {
      const account = post.find((a) => a.address === key.toBase58());
      return account ? Buffer.from(account.data).readBigUInt64LE(64) : MIRROR_BALANCE;
    });
  }

  /** Load a program from a local .so file (the executor in dry-run mode). */
  addProgramFile(id: PublicKey, path: string): void {
    if (this.disposed) return;
    this.svm.addProgramFromFile(address(id.toBase58()), path);
    this.programs.add(id.toBase58());
    this.tracked.add(id.toBase58());
  }

  /**
   * Simulate a whole signed or unsigned transaction against the mirror (no state change) and read
   * the token balances of `watch` afterwards. Used to verify a complete trade in its real context:
   * some venues price differently when called from inside another program.
   */
  simulateTransaction(tx: VersionedTransaction, watch: PublicKey[]):
    { ok: boolean; error?: string; logs: string[]; computeUnits: number; balances: Map<string, bigint> } {
    const result = this.svm.simulateTransaction(getTransactionDecoder().decode(tx.serialize()));
    const balances = new Map<string, bigint>();
    if (result instanceof FailedTransactionMetadata) {
      const logs = result.meta().logs();
      return { ok: false, error: logs.find((l) => /Error Message:|Error:|failed/.test(l)) ?? String(result.err()), logs,
        computeUnits: Number(result.meta().computeUnitsConsumed()), balances };
    }
    for (const key of watch) {
      const account = result.postAccounts().find((a) => a.address === key.toBase58());
      const before = this.svm.getAccount(address(key.toBase58()));
      const data = account ? Buffer.from(account.data) : before.exists ? Buffer.from(before.data) : undefined;
      if (data && data.length >= 72) balances.set(key.toBase58(), data.readBigUInt64LE(64));
    }
    return { ok: true, logs: result.meta().logs(), computeUnits: Number(result.meta().computeUnitsConsumed()), balances };
  }

  /** The synthetic balance every owner token account starts each simulation with. */
  static readonly ownerBalance = MIRROR_BALANCE;

  latestBlockhash(): string { return this.svm.latestBlockhash(); }

  close(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.geyser?.close();
    this.listeners = [];
    for (const id of this.subscriptions.values()) this.connection.removeAccountChangeListener(id).catch(() => undefined);
    this.subscriptions.clear();
    if (this.pollTimer) clearInterval(this.pollTimer);
    if (this.fullPollTimer) clearInterval(this.fullPollTimer);
    if (this.clockPollTimer) clearInterval(this.clockPollTimer);
    if (this.canarySubscription !== undefined) this.connection.removeAccountChangeListener(this.canarySubscription).catch(() => undefined);
    if (this.clockSubscription !== undefined) this.clockConnection.removeAccountChangeListener(this.clockSubscription).catch(() => undefined);
    if (this.slotSubscription !== undefined) this.clockConnection.removeSlotChangeListener(this.slotSubscription).catch(() => undefined);
  }
}
