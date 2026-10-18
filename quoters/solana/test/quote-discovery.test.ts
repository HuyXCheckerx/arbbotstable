import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { Keypair, type AccountInfo, type Connection, PublicKey } from "@solana/web3.js";
import { RAYDIUM_CLMM, WHIRLPOOL } from "../src/constants.js";
import { discoverQuotePools, hydrateQuotePools, raydiumArrayStarts, validatePoolAccount } from "../src/quote/discovery.js";
import { knownToken } from "../src/tokens.js";
import { Mirror } from "../src/market/mirror.js";
import { SnapshotQuoteMarket } from "../src/quote/market.js";
import { readFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const input = knownToken("USDC"), output = knownToken("USDT");
const sol = { symbol: "SOL", mint: new PublicKey("So11111111111111111111111111111111111111112"), decimals: 9, program: input.program };
const disc = (name: string) => createHash("sha256").update(`account:${name}`).digest().subarray(0, 8);
const pool = Keypair.generate().publicKey;
function data(wp: boolean): Buffer {
  const d = Buffer.alloc(wp ? 653 : 1544);
  disc(wp ? "Whirlpool" : "PoolState").copy(d);
  input.mint.toBuffer().copy(d, wp ? 101 : 73);
  output.mint.toBuffer().copy(d, wp ? 181 : 105);
  d.writeUInt16LE(1, wp ? 41 : 235);
  return d;
}
const account = (d: Buffer, owner: PublicKey): AccountInfo<Buffer> => ({ data: d, owner, executable: false, lamports: 1, rentEpoch: 0 });

test("DEX indexes discover only addresses and explicitly report partial coverage", async () => {
  const urls: string[] = [];
  const request = (async (url: URL) => {
    urls.push(String(url));
    return Response.json(String(url).includes("orca.so") ? { data: [{ address: String(pool) }], meta: { cursor: { next: "more" } } }
      : { success: true, data: { count: 1, data: [{ id: String(Keypair.generate().publicKey) }] } });
  }) as typeof fetch;
  const result = await discoverQuotePools(input, output, request);
  assert.equal(result.pools.length, 2);
  assert.equal(result.warnings.length, 1);
  assert.ok(urls.every(url => url.includes(String(input.mint)) && url.includes(String(output.mint))));
  assert.ok(urls.every(url => !url.includes("jup.ag") && !url.includes("okx")));
});

test("discovery keeps healthy venues but never hides failed sources or empty coverage", async () => {
  const request = (async (url: URL) => String(url).includes("orca.so") ? new Response("denied", { status: 403 })
    : Response.json({ success: true, data: { count: 1, data: [{ id: String(pool) }] } })) as typeof fetch;
  const result = await discoverQuotePools(input, output, request);
  assert.equal(result.pools.length, 1);
  assert.match(result.warnings[0], /Orca.*403/);
  await assert.rejects(discoverQuotePools(input, output, (async () => Response.json({ data: [] })) as typeof fetch), /No direct pools/);
});

test("intermediate legs keep only the largest index pools without reporting the cut as partial coverage", async () => {
  const urls: string[] = [];
  const orca = [5, 50, 1, 20].map(tvl => ({ address: String(Keypair.generate().publicKey), tvlUsdc: String(tvl) }));
  const request = (async (url: URL) => {
    urls.push(String(url));
    return Response.json(String(url).includes("orca.so") ? { data: orca, meta: { cursor: { next: "more" } } }
      : { success: true, data: { count: 40, hasNextPage: true, data: [{ id: String(Keypair.generate().publicKey) }] } });
  }) as typeof fetch;
  const result = await discoverQuotePools(input, sol, request, undefined, { indexLimit: 2 });
  assert.deepEqual(result.pools.filter(p => p.venue === "whirlpool").map(p => String(p.address)), [orca[1].address, orca[3].address]);
  assert.ok(urls.some(url => url.includes("api-v3.raydium.io") && url.includes("pageSize=2")));
  assert.deepEqual(result.warnings, []);
  assert.equal((await discoverQuotePools(input, sol, request)).warnings.length, 2, "an uncapped list still reports partial pages");
  await assert.rejects(discoverQuotePools(input, sol, request, undefined, { indexLimit: 0 }), /index limit/);
});

test("snapshot markets hydrate the direct pair and both legs of each intermediate", () => {
  const market = new SnapshotQuoteMarket({} as Connection, input, output, undefined, [sol, input]);
  try {
    assert.deepEqual(market.pairs.map(pair => pair.map(t => t.symbol).join("/")), ["USDC/USDT", "USDC/SOL", "SOL/USDT"]);
    assert.deepEqual(market.tokens.map(t => t.symbol), ["USDC", "USDT", "SOL"]);
  } finally { market.close(); }
});

test("one market can serve several pairs, each with its intermediate legs, without duplicates", () => {
  const pyusd = knownToken("PYUSD"), usdg = knownToken("USDG");
  const market = new SnapshotQuoteMarket({} as Connection, usdg, input, undefined, [input], { pairs: [[pyusd, input], [pyusd, usdg], [input, usdg]] });
  try {
    assert.deepEqual(market.served.map(pair => pair.map(t => t.symbol).join("/")), ["USDG/USDC", "PYUSD/USDC", "PYUSD/USDG"], "USDC/USDG is USDG/USDC");
    assert.deepEqual(market.pairs.map(pair => pair.map(t => t.symbol).join("/")), ["USDG/USDC", "PYUSD/USDC", "PYUSD/USDG"],
      "routing PYUSD/USDG through USDC needs only pools the other pairs already have");
    assert.deepEqual(market.tokens.map(t => t.symbol), ["USDG", "USDC", "PYUSD"]);
  } finally { market.close(); }
});

test("pool addresses are checked against chain owner, discriminator, size, pair and tick spacing", () => {
  const candidate = { venue: "whirlpool" as const, address: pool };
  const valid = account(data(true), WHIRLPOOL);
  assert.equal(validatePoolAccount(candidate, valid, input, output), valid.data);
  assert.throws(() => validatePoolAccount(candidate, { ...valid, owner: RAYDIUM_CLMM }, input, output), /Invalid/);
  assert.throws(() => validatePoolAccount(candidate, { ...valid, data: Buffer.alloc(653) }, input, output), /Invalid/);
  const wrongPair = data(true); Keypair.generate().publicKey.toBuffer().copy(wrongPair, 101);
  assert.throws(() => validatePoolAccount(candidate, account(wrongPair, WHIRLPOOL), input, output), /requested pair/);
  const zeroSpacing = data(true); zeroSpacing.writeUInt16LE(0, 41);
  assert.throws(() => validatePoolAccount(candidate, account(zeroSpacing, WHIRLPOOL), input, output), /spacing/);
});

test("Raydium initializes tick arrays from pool and signed extension bitmaps without program scans", () => {
  const d = data(false), extension = Buffer.alloc(1832);
  disc("TickArrayBitmapExtension").copy(extension); pool.toBuffer().copy(extension, 8);
  const set = (buffer: Buffer, at: number, bit: number) => { buffer[at + (bit >> 3)] |= 1 << (bit & 7); };
  for (const bit of [0, 512, 513, 1023]) set(d, 904, bit);
  set(extension, 40, 0); set(extension, 104, 7);
  set(extension, 936, 0); set(extension, 936, 511); set(extension, 1000, 10);
  assert.deepEqual(raydiumArrayStarts(d, pool, extension), [-1526, -1024, -513, -512, 0, 1, 511, 512, 1031].map(n => n * 60));
  assert.throws(() => raydiumArrayStarts(d, Keypair.generate().publicKey, extension), /extension/);
  assert.throws(() => raydiumArrayStarts(d, pool, Buffer.alloc(1832)), /extension/);
});

test("RPC hydration rejects substituted pools and needs no getProgramAccounts", async () => {
  const connection = {
    getMultipleAccountsInfoAndContext: async () => ({ context: { slot: 100 }, value: [account(data(true), WHIRLPOOL), account(data(true), RAYDIUM_CLMM)] }),
  } as unknown as Connection;
  const result = await hydrateQuotePools(connection, [
    { venue: "whirlpool", address: pool }, { venue: "whirlpool", address: Keypair.generate().publicKey },
  ], [[input, output]]);
  assert.equal(result.pools.length, 1);
  assert.equal(result.warnings.length, 1);
});

test("snapshot mirrors read a clock and account slots without installing subscriptions", async () => {
  const clock = Buffer.alloc(40); clock.writeBigUInt64LE(100n);
  const connection = {
    getAccountInfo: async () => account(clock, WHIRLPOOL),
    getMultipleAccountsInfoAndContext: async () => ({ context: { slot: 100 }, value: [account(Buffer.from([1]), WHIRLPOOL)] }),
    onAccountChange: () => { throw new Error("snapshot quotes must not open websockets"); },
    onSlotChange: () => { throw new Error("snapshot quotes must not open websockets"); },
  } as unknown as Connection;
  const mirror = new Mirror(connection, undefined, 0, connection, 64 * 1024, undefined, false);
  try {
    await mirror.track([pool]);
    await mirror.followClock();
    assert.equal(mirror.chainSlot, 100);
    assert.equal(mirror.subscriptionCount, 0);
    assert.deepEqual(mirror.accountSlotRange([pool]), { min: 100, max: 100, unknown: 0 });
    assert.deepEqual(mirror.accountSlotRange([pool, Keypair.generate().publicKey]), { min: 100, max: 100, unknown: 1 });
  } finally { mirror.close(); }
});

test("the quote clock moves past slots in which a pool charges more, within the slot spread", async () => {
  const market = new SnapshotQuoteMarket({ getAccountInfo: async () => null } as unknown as Connection, input, output);
  try {
    const stamp = Buffer.alloc(8); stamp.writeBigUInt64LE(1_001n);
    (market.mirror as unknown as { data: (key: PublicKey) => Buffer | undefined }).data = key => key.equals(pool) ? stamp : undefined;
    market.pools = [{ address: pool, minQuoteSlot: (d: Buffer) => d.readBigUInt64LE(0) } as never];
    const pin = (slot: number) => (market as unknown as { pinClock(slot: number, spread: number): Promise<number> }).pinClock(slot, 32);
    assert.equal(await pin(1_000), 1_001);
    assert.equal(market.mirror.svm.getClock().slot, 1_001n);
    assert.equal(await pin(1_005), 1_005, "a later snapshot slot is kept");
    assert.equal(await pin(900), 900, "never beyond the slot spread");
  } finally { market.close(); }
});

test("hydration can skip pools without active liquidity before their accounts are tracked", async () => {
  const live = data(true); live.writeBigUInt64LE(5n, 49);
  const connection = {
    getMultipleAccountsInfoAndContext: async () => ({ context: { slot: 100 }, value: [account(live, WHIRLPOOL), account(data(true), WHIRLPOOL)] }),
  } as unknown as Connection;
  const candidates = [{ venue: "whirlpool" as const, address: pool }, { venue: "whirlpool" as const, address: Keypair.generate().publicKey }];
  assert.equal((await hydrateQuotePools(connection, candidates, [[input, output]])).pools.length, 2);
  const result = await hydrateQuotePools(connection, candidates, [[input, output]], { skipEmpty: true });
  assert.deepEqual(result.pools.map(p => String(p.address)), [String(pool)]);
  assert.equal(result.empty, 1);
  assert.deepEqual(result.warnings, []);
});

test("deployed program binaries are cached by deploy slot and fetched again after an upgrade", async () => {
  const UPGRADEABLE = new PublicKey("BPFLoaderUpgradeab1e11111111111111111111111");
  const dir = await mkdtemp(join(tmpdir(), "quote-programs-"));
  const programData = new Map<string, PublicKey>();
  let slot = 7n;
  const downloads: string[] = [];
  const header = () => { const d = Buffer.alloc(45); d.writeUInt32LE(3); d.writeBigUInt64LE(slot, 4); return d; };
  const connection = {
    getMultipleAccountsInfo: async (keys: PublicKey[], config?: { dataSlice?: unknown }) => keys.map(key => {
      if (config?.dataSlice) return account(header(), UPGRADEABLE);
      if (!programData.has(String(key))) programData.set(String(key), Keypair.generate().publicKey);
      const d = Buffer.alloc(36); d.writeUInt32LE(2); programData.get(String(key))!.toBuffer().copy(d, 4);
      return { ...account(d, UPGRADEABLE), executable: true };
    }),
    getAccountInfo: async (key: PublicKey) => {
      downloads.push(String(key));
      return account(Buffer.concat([header(), Buffer.from(`elf ${key} ${slot}`)]), UPGRADEABLE);
    },
  } as unknown as Connection;
  const load = async () => {
    const market = new SnapshotQuoteMarket(connection, input, output, dir), added: string[] = [];
    market.mirror.addProgramFile = (id, path) => { added.push(`${id} ${readFileSync(path, "utf8")}`); };
    try {
      await market.loadPrograms(["whirlpool"]);
      await market.loadPrograms(["whirlpool", "raydium-clmm"]);
      return added.sort();
    } finally { market.close(); }
  };
  try {
    const first = await load();
    assert.equal(first.length, 3, "Whirlpool, Raydium CLMM and Token-2022, each loaded once");
    assert.equal(downloads.length, 3);
    assert.ok(first.every(line => line.endsWith(" 7")));
    assert.deepEqual(await load(), first, "an unchanged deploy slot reuses the cached binary");
    assert.equal(downloads.length, 3);
    slot = 8n;
    const upgraded = await load();
    assert.equal(downloads.length, 6, "a new deploy slot is downloaded again");
    assert.ok(upgraded.every(line => line.endsWith(" 8")));
  } finally { await rm(dir, { recursive: true, force: true }); }
});
