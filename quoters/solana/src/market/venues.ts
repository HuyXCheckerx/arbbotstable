import { type Connection, PublicKey, TransactionInstruction } from "@solana/web3.js";
import { getBase58Decoder } from "@solana/kit";
import { RAYDIUM_CLMM, WHIRLPOOL } from "../constants.js";

/**
 * Pool models for the venues the executor can call directly. Each model knows which accounts a
 * swap needs right now (they move with the price: tick arrays) and builds an exact-input swap.
 * Layouts were taken from the official clients (@orca-so/whirlpools-client, @raydium-io/raydium-sdk-v2)
 * and verified by executing swaps against mainnet state in LiteSVM.
 */

export type VenueName = "whirlpool" | "raydium-clmm";
const MEMO = new PublicKey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr");
const TOKEN = new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
const TOKEN_2022 = new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
/** Anchor `global:swap_v2`, shared by both venues. */
const SWAP_V2 = Buffer.from([43, 4, 237, 11, 26, 201, 30, 98]);
const U64 = (1n << 64n) - 1n;

export interface SwapParams {
  owner: PublicKey; mintIn: PublicKey; ataIn: PublicKey; ataOut: PublicKey; amount: bigint; minOut: bigint;
  /** Latest pool account data (tick arrays are chosen from its current tick). */
  data: Buffer;
  tokenProgram: (mint: PublicKey) => PublicKey;
  exists: (key: PublicKey) => boolean;
}

export interface PoolModel {
  venue: VenueName; address: PublicKey; mintA: PublicKey; mintB: PublicKey; feeBps: number; label: string;
  /** Accounts that never change for this pool. */
  staticAccounts: PublicKey[];
  /** Accounts a swap needs at the price encoded in `data` (both directions). */
  dynamicAccounts(data: Buffer): PublicKey[];
  /** Whether the pool can trade right now (liquidity, not disabled). */
  tradable(data: Buffer): boolean;
  swap(params: SwapParams): TransactionInstruction;
}

const meta = (pubkey: PublicKey, isWritable = false, isSigner = false) => ({ pubkey, isWritable, isSigner });
const u128 = (d: Buffer, at: number) => d.readBigUInt64LE(at) | (d.readBigUInt64LE(at + 8) << 64n);
const writeU128 = (d: Buffer, at: number, v: bigint) => { d.writeBigUInt64LE(v & U64, at); d.writeBigUInt64LE(v >> 64n, at + 8); };
const short = (k: PublicKey) => k.toBase58().slice(0, 6);
/** PDA derivation memoized per model: swap builders derive up to five tick arrays per call, and an uncached
 * derivation costs more than the simulated swap itself. */
const memoPda = (program: PublicKey) => {
  const cache = new Map<string, PublicKey>();
  return (seeds: Buffer[]) => {
    const key = seeds.map((seed) => seed.toString("hex")).join("|");
    let found = cache.get(key);
    if (!found) cache.set(key, found = PublicKey.findProgramAddressSync(seeds, program)[0]);
    return found;
  };
};
/** Floor division that rounds toward negative infinity (ticks can be negative). */
export const arrayStart = (tick: number, span: number) => Math.floor(tick / span) * span;

// ---------------------------------------------------------------- Orca Whirlpool
const WP_TICK_ARRAY_SIZE = 88;

export function whirlpoolModel(address: PublicKey, data: Buffer): PoolModel {
  const mintA = new PublicKey(data.subarray(101, 133)), vaultA = new PublicKey(data.subarray(133, 165));
  const mintB = new PublicKey(data.subarray(181, 213)), vaultB = new PublicKey(data.subarray(213, 245));
  const spacing = data.readUInt16LE(41);
  const feeBps = data.readUInt16LE(45) / 100; // fee_rate is in hundredths of a basis point
  const pda = memoPda(WHIRLPOOL);
  const oracle = pda([Buffer.from("oracle"), address.toBuffer()]);
  const span = WP_TICK_ARRAY_SIZE * spacing;
  const tickArrays = (d: Buffer) => {
    const start = arrayStart(d.readInt32LE(81), span);
    // Same set the Orca SDK passes: current, two above, two below (the last two as supplemental accounts).
    return [0, 1, 2, -1, -2].map((k) => pda([Buffer.from("tick_array"), address.toBuffer(), Buffer.from(String(start + k * span))]));
  };
  return {
    venue: "whirlpool", address, mintA, mintB, feeBps, label: `Whirlpool ${short(address)} (${feeBps} bps)`,
    staticAccounts: [address, vaultA, vaultB, mintA, mintB, oracle],
    dynamicAccounts: tickArrays,
    tradable: (d) => u128(d, 49) > 0n,
    swap(p) {
      const aToB = p.mintIn.equals(mintA);
      if (!aToB && !p.mintIn.equals(mintB)) throw new Error(`${short(address)} does not trade ${p.mintIn.toBase58()}`);
      const arrays = tickArrays(p.data);
      const data = Buffer.alloc(49);
      SWAP_V2.copy(data, 0);
      data.writeBigUInt64LE(p.amount, 8);
      data.writeBigUInt64LE(p.minOut, 16);
      // 0 = no explicit limit: the program then refuses a partial fill instead of swapping less.
      writeU128(data, 24, 0n);
      data[40] = 1; // amount is the exact input
      data[41] = aToB ? 1 : 0;
      // remaining_accounts_info = Some({ slices: [{ SupplementalTickArrays, 2 }] })
      data[42] = 1; data.writeUInt32LE(1, 43); data[47] = 6; data[48] = 2;
      const [ownerA, ownerB] = aToB ? [p.ataIn, p.ataOut] : [p.ataOut, p.ataIn];
      return new TransactionInstruction({ programId: WHIRLPOOL, data, keys: [
        meta(p.tokenProgram(mintA)), meta(p.tokenProgram(mintB)), meta(MEMO), meta(p.owner, false, true), meta(address, true),
        meta(mintA), meta(mintB), meta(ownerA, true), meta(vaultA, true), meta(ownerB, true), meta(vaultB, true),
        meta(arrays[0], true), meta(arrays[1], true), meta(arrays[2], true), meta(oracle, true),
        meta(arrays[3], true), meta(arrays[4], true)] });
    },
  };
}

// ---------------------------------------------------------------- Raydium CLMM
const RAY_MIN_SQRT = 4295048016n;
const RAY_MAX_SQRT = 79226673521066979257578248091n;
const RAY_TICK_ARRAY_SIZE = 60;
const RAY_TICK_ARRAY_DISC = Buffer.from([192, 155, 85, 205, 49, 249, 129, 42]);
/** PoolStatusBitIndex::Swap: set means swaps are disabled. */
const RAY_SWAP_DISABLED = 1 << 4;

/** `tickArrayStarts` must list the start index of every tick array account the pool has (see listRaydiumTickArrays).
 * `program` selects a deployment with the identical layout (PancakeSwap's Solana CLMM is a Raydium CLMM fork). */
export function raydiumClmmModel(address: PublicKey, data: Buffer, tickArrayStarts: number[], program = RAYDIUM_CLMM): PoolModel {
  const config = new PublicKey(data.subarray(9, 41));
  const mintA = new PublicKey(data.subarray(73, 105)), mintB = new PublicKey(data.subarray(105, 137));
  const vaultA = new PublicKey(data.subarray(137, 169)), vaultB = new PublicKey(data.subarray(169, 201));
  const observation = new PublicKey(data.subarray(201, 233));
  const spacing = data.readUInt16LE(235);
  const span = RAY_TICK_ARRAY_SIZE * spacing;
  const pda = memoPda(program);
  const extension = pda([Buffer.from("pool_tick_array_bitmap_extension"), address.toBuffer()]);
  const starts = [...tickArrayStarts].sort((a, b) => a - b);
  const arrayKey = (start: number) => { const be = Buffer.alloc(4); be.writeInt32BE(start); return pda([Buffer.from("tick_array"), address.toBuffer(), be]); };
  // Initialized arrays in traversal order: price down (zero for one) or up.
  const down = (tick: number) => starts.filter((s) => s <= tick).reverse().slice(0, 5).map(arrayKey);
  const up = (tick: number) => starts.filter((s) => s + span > tick).slice(0, 5).map(arrayKey);
  const model: PoolModel & { tickArrayStarts: number[] } = {
    venue: "raydium-clmm", address, mintA, mintB, feeBps: 0, label: `Raydium CLMM ${short(address)}`, tickArrayStarts: starts,
    staticAccounts: [address, config, vaultA, vaultB, observation, mintA, mintB, extension],
    dynamicAccounts: (d) => [...new Set([...down(d.readInt32LE(269)), ...up(d.readInt32LE(269))].map(String))].map((k) => new PublicKey(k)),
    tradable: (d) => u128(d, 237) > 0n && (d[389] & RAY_SWAP_DISABLED) === 0,
    swap(p) {
      const zeroForOne = p.mintIn.equals(mintA);
      if (!zeroForOne && !p.mintIn.equals(mintB)) throw new Error(`${short(address)} does not trade ${p.mintIn.toBase58()}`);
      const tick = p.data.readInt32LE(269);
      const data = Buffer.alloc(41);
      SWAP_V2.copy(data, 0);
      data.writeBigUInt64LE(p.amount, 8);
      data.writeBigUInt64LE(p.minOut, 16);
      writeU128(data, 24, zeroForOne ? RAY_MIN_SQRT + 1n : RAY_MAX_SQRT - 1n);
      data[40] = 1; // is_base_input: amount is the exact input
      const [inVault, outVault, inMint, outMint] = zeroForOne ? [vaultA, vaultB, mintA, mintB] : [vaultB, vaultA, mintB, mintA];
      return new TransactionInstruction({ programId: program, data, keys: [
        meta(p.owner, false, true), meta(config), meta(address, true), meta(p.ataIn, true), meta(p.ataOut, true),
        meta(inVault, true), meta(outVault, true), meta(observation, true), meta(TOKEN), meta(TOKEN_2022), meta(MEMO),
        meta(inMint), meta(outMint),
        ...(p.exists(extension) ? [meta(extension, true)] : []),
        ...(zeroForOne ? down(tick) : up(tick)).map((k) => meta(k, true))] });
    },
  };
  return model;
}

/** Start indexes of every tick array account a Raydium CLMM pool has. */
export async function listRaydiumTickArrays(connection: Connection, pool: PublicKey): Promise<number[]> {
  const accounts = await connection.getProgramAccounts(RAYDIUM_CLMM, { dataSlice: { offset: 40, length: 4 }, filters: [
    { memcmp: { offset: 0, bytes: getBase58Decoder().decode(RAY_TICK_ARRAY_DISC) } }, { memcmp: { offset: 8, bytes: pool.toBase58() } }] });
  return accounts.map((a) => a.account.data.readInt32LE(0));
}

// ---------------------------------------------------------------- discovery
const LAYOUT = {
  whirlpool: { program: WHIRLPOOL, size: 653, mintA: 101, mintB: 181 },
  "raydium-clmm": { program: RAYDIUM_CLMM, size: 1544, mintA: 73, mintB: 105 },
} as const;

/** Every pool of `venue` whose two tokens are both in `mints` (both orderings). */
export async function discoverPools(connection: Connection, venue: VenueName, mints: PublicKey[]): Promise<PoolModel[]> {
  const layout = LAYOUT[venue];
  const pools: PoolModel[] = [];
  for (const a of mints) for (const b of mints) {
    if (a.equals(b)) continue;
    const found = await connection.getProgramAccounts(layout.program, { filters: [{ dataSize: layout.size },
      { memcmp: { offset: layout.mintA, bytes: a.toBase58() } }, { memcmp: { offset: layout.mintB, bytes: b.toBase58() } }] });
    for (const { pubkey, account } of found) {
      // Tick arrays are only listed for pools with liquidity: an empty pool cannot be swapped anyway,
      // and each listing is a program-account scan that counts against the RPC rate limit.
      const live = (venue === "whirlpool" ? account.data.readBigUInt64LE(49) | account.data.readBigUInt64LE(57)
        : account.data.readBigUInt64LE(237) | account.data.readBigUInt64LE(245)) > 0n;
      pools.push(venue === "whirlpool" ? whirlpoolModel(pubkey, account.data)
        : raydiumClmmModel(pubkey, account.data, live ? await listRaydiumTickArrays(connection, pubkey) : []));
    }
  }
  return pools;
}
