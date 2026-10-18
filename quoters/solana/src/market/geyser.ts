import { type AccountInfo, PublicKey, SYSVAR_CLOCK_PUBKEY } from "@solana/web3.js";
import type { SubscribeRequest, SubscribeUpdate } from "@triton-one/yellowstone-grpc";

export interface GeyserStream {
  on(event: string, listener: (...args: any[]) => void): unknown;
  write(request: SubscribeRequest, callback?: (error?: Error | null) => void): unknown;
  destroy(): unknown;
}

export interface GeyserOptions {
  url: string;
  token?: string;
  /** Injectable transport and clock for offline tests. Production uses Yellowstone's real SDK. */
  open?: (request: SubscribeRequest) => Promise<GeyserStream>;
  now?: () => number;
  reconnectMs?: number;
  healthyMs?: number;
  /** Reconcile accounts over RPC before promoting a newly opened stream to healthy. */
  resync?: () => Promise<void>;
  log?: (event: string, data: Record<string, unknown>) => void;
}

export interface GeyserAccount {
  key: string;
  account: AccountInfo<Buffer>;
  slot: number;
  writeVersion: bigint;
}

export function geyserOptionsFromEnv(env: NodeJS.ProcessEnv = process.env): GeyserOptions | undefined {
  return env.GEYSER_GRPC_URL ? { url: env.GEYSER_GRPC_URL, token: env.GEYSER_X_TOKEN || undefined } : undefined;
}

/** Explicit account-only subscription. Keeps endpoint/token/error contents out of diagnostics. */
export class GeyserFeed {
  private readonly keys = new Set<string>([SYSVAR_CLOCK_PUBKEY.toBase58()]);
  private readonly versions = new Map<string, { slot: number; writeVersion: bigint }>();
  private stream?: GeyserStream;
  private started = false;
  private closed = false;
  private connecting = false;
  private retryTimer?: NodeJS.Timeout;
  private updateTimer?: NodeJS.Timeout;
  private watchdog?: NodeJS.Timeout;
  private lastDataAt = 0;
  private connectedAt = 0;
  private receivedAccount = false;
  private resyncing = false;
  private retries = 0;
  private rejected = 0;
  private slot = 0;

  constructor(private readonly options: GeyserOptions, private readonly apply: (update: GeyserAccount) => boolean | void) {}

  private now(): number { return this.options.now?.() ?? Date.now(); }
  get healthy(): boolean {
    return !!this.stream && !this.resyncing && this.receivedAccount && this.slot > 0 && this.now() - this.lastDataAt < (this.options.healthyMs ?? 3_000);
  }
  get status(): { enabled: true; connected: boolean; healthy: boolean; lastSlot: number; trackedAccounts: number; reconnects: number; rejectedUpdates: number } {
    return { enabled: true, connected: !!this.stream, healthy: this.healthy, lastSlot: this.slot,
      trackedAccounts: this.keys.size, reconnects: this.retries, rejectedUpdates: this.rejected };
  }

  setAccounts(keys: Iterable<string>): void {
    const next = new Set([SYSVAR_CLOCK_PUBKEY.toBase58(), ...keys]);
    if (next.size === this.keys.size && [...next].every((key) => this.keys.has(key))) return;
    this.keys.clear();
    for (const key of next) this.keys.add(key);
    this.receivedAccount = false;
    if (this.stream && !this.updateTimer) {
      this.updateTimer = setTimeout(() => { this.updateTimer = undefined; this.write(); }, 25);
      this.updateTimer.unref();
    }
  }

  private request(ping?: number): SubscribeRequest {
    return { accounts: { tracked: { account: [...this.keys], owner: [], filters: [] } },
      slots: { clock: { filterByCommitment: true, interslotUpdates: false } }, transactions: {}, transactionsStatus: {},
      blocks: {}, blocksMeta: {}, blockFooter: {}, entry: {}, accountsDataSlice: [], commitment: 0,
      ...(ping === undefined ? {} : { ping: { id: ping } }) };
  }

  start(): void {
    if (this.started || this.closed) return;
    this.started = true;
    this.watchdog = setInterval(() => {
      if (this.stream && this.now() - Math.max(this.lastDataAt, this.connectedAt) > 10_000) this.disconnect();
    }, 1_000);
    this.watchdog.unref();
    void this.connect();
  }

  private async connect(): Promise<void> {
    if (this.closed || this.connecting || this.stream) return;
    this.connecting = true;
    try {
      const request = this.request();
      const stream = this.options.open ? await this.options.open(request) : await this.open(request);
      stream.on("error", () => undefined);
      if (this.closed) { stream.destroy(); return; }
      this.stream = stream;
      this.connectedAt = this.now();
      this.lastDataAt = 0;
      this.receivedAccount = false;
      this.resyncing = true;
      this.versions.clear();
      stream.on("data", (update: SubscribeUpdate) => { if (this.stream === stream) this.receive(update); });
      stream.on("error", () => { if (this.stream === stream) this.disconnect(); });
      stream.on("end", () => { if (this.stream === stream) this.disconnect(); });
      stream.on("close", () => { if (this.stream === stream) this.disconnect(); });
      // Account discovery may have changed the filter while opening the channel.
      this.write();
      await this.options.resync?.();
      if (this.stream !== stream || this.closed) return;
      this.resyncing = false;
      this.options.log?.("geyser", { reason: "connected; websocket fallback remains available", accounts: this.keys.size });
    } catch {
      const stream = this.stream;
      this.stream = undefined;
      stream?.destroy();
      this.scheduleReconnect();
    } finally {
      this.connecting = false;
      // An error/watchdog can fire while resync is still awaited. Its retry timer may have
      // coalesced with this connection; ensure it cannot leave the feed permanently idle.
      if (!this.closed && !this.stream && !this.retryTimer) this.scheduleReconnect();
    }
  }

  private async open(request: SubscribeRequest): Promise<GeyserStream> {
    const sdk = await import("@triton-one/yellowstone-grpc");
    // The package exposes an ESM constructor, but its declaration is resolved as CJS by NodeNext.
    const Client = sdk.default as unknown as new (url: string, token: string | undefined,
      options: { grpcConnectTimeout: number; grpcMaxDecodingMessageSize: number; grpcTcpNodelay: boolean }) => {
      subscribe(request: SubscribeRequest): Promise<GeyserStream>;
    };
    const client = new Client(this.options.url, this.options.token, { grpcConnectTimeout: 5_000,
      grpcMaxDecodingMessageSize: 64 * 1024 * 1024, grpcTcpNodelay: true });
    return client.subscribe(request);
  }

  private write(ping?: number): void {
    if (!this.stream || this.closed) return;
    const stream = this.stream;
    try { stream.write(this.request(ping), (error) => { if (error && this.stream === stream) this.disconnect(); }); }
    catch { this.disconnect(); }
  }

  private receive(update: SubscribeUpdate): void {
    try {
      if (update.ping) this.write(1);
      if (update.slot) {
        const slot = Number(update.slot.slot);
        if (Number.isSafeInteger(slot) && slot >= this.slot) { this.slot = slot; this.lastDataAt = this.now(); }
      }
      const message = update.account;
      const info = message?.account;
      if (!message || !info) return;
      const key = new PublicKey(info.pubkey).toBase58();
      if (!this.keys.has(key)) return;
      const slot = Number(message.slot), writeVersion = BigInt(info.writeVersion);
      const previous = this.versions.get(key);
      if (!Number.isSafeInteger(slot) || slot < 0 || writeVersion < 0n || (previous && (slot < previous.slot
        || (slot === previous.slot && writeVersion <= previous.writeVersion)))) { this.rejected++; return; }
      const lamports = Number(info.lamports);
      if (!Number.isSafeInteger(lamports) || lamports < 0) { this.rejected++; return; }
      const account: AccountInfo<Buffer> = { data: Buffer.from(info.data), owner: new PublicKey(info.owner), executable: info.executable,
        lamports, rentEpoch: Number(info.rentEpoch) };
      if (this.apply({ key, account, slot, writeVersion }) === false) { this.rejected++; return; }
      this.versions.set(key, { slot, writeVersion });
      this.slot = Math.max(this.slot, slot);
      this.lastDataAt = this.now();
      this.receivedAccount = true;
    } catch { this.rejected++; }
  }

  private disconnect(): void {
    const stream = this.stream;
    this.stream = undefined;
    this.receivedAccount = false;
    stream?.destroy();
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.closed || this.retryTimer) return;
    this.retries++;
    const delay = Math.min(30_000, (this.options.reconnectMs ?? 1_000) * 2 ** Math.min(5, this.retries - 1));
    this.options.log?.("geyser", { reason: "stream unavailable; using websocket/HTTP fallback", retryInMs: delay });
    this.retryTimer = setTimeout(() => { this.retryTimer = undefined; void this.connect(); }, delay);
    this.retryTimer.unref();
  }

  close(): void {
    this.closed = true;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.updateTimer) clearTimeout(this.updateTimer);
    if (this.watchdog) clearInterval(this.watchdog);
    const stream = this.stream;
    this.stream = undefined;
    stream?.destroy();
  }
}
