import { createHash } from "node:crypto";
import { ASSOCIATED_TOKEN_PROGRAM_ID, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { type AccountMeta, PublicKey, SYSVAR_INSTRUCTIONS_PUBKEY, SystemProgram, TransactionInstruction } from "@solana/web3.js";
import { RAYDIUM_CLMM, WHIRLPOOL } from "../constants.js";
import { type PoolModel, type SwapParams, raydiumClmmModel, whirlpoolModel } from "../market/venues.js";

/**
 * Quote-only pool models for venues the trading executor does not call. Like the shared models, each one lists
 * the accounts a swap reads and builds one exact-input swap instruction; the deployed program does the pricing.
 * Account orders and instruction data come from the venues' SDKs where published (Manifest, Deriverse, PancakeSwap
 * as a Raydium CLMM fork) and otherwise from swaps observed on chain (GoonFi V2, JupLend AMM, BisonFi, Kipseli), checked
 * by simulating them against Jupiter's quote for the same pool. Those observed layouts have no published specification.
 * GoonFi V2 and Kipseli price Jupiter-routed swaps better than direct calls, so their legs are one-step Jupiter routes.
 */

export type QuoteVenue = "whirlpool" | "raydium-clmm" | "pancakeswap" | "manifest" | "goonfi" | "deriverse" | "juplend" | "bisonfi" | "kipseli";
export const QUOTE_VENUES: readonly QuoteVenue[] = ["whirlpool", "raydium-clmm", "pancakeswap", "manifest", "goonfi", "deriverse", "juplend",
  "bisonfi", "kipseli"];
export interface QuotePool extends Omit<PoolModel, "venue"> {
  venue: QuoteVenue;
  /** Oracle pools, which price only for a few slots after an update: the slot from which the last update applies. */
  validFromSlot?: (data: Buffer) => bigint;
  /** Pools that charge more in a slot in which they already traded or took a price update: the first slot after those.
   * A transaction built from a snapshot lands later, so quotes run no earlier. `read` returns snapshot account data. */
  minQuoteSlot?: (data: Buffer, read: (key: PublicKey) => Buffer | undefined) => bigint;
}

export const PANCAKESWAP = new PublicKey("HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq");
export const MANIFEST = new PublicKey("MNFSTqtC93rEfYHB6hF82sKdZpUDFWkViLByLd1k1Ms");
export const GOONFI = new PublicKey("goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE");
export const DERIVERSE = new PublicKey("DRVSpZ2YUYYKgZP8XtLhAGtT1zYSCKzeHfb4DgRnrgqD");
export const JUPLEND_AMM = new PublicKey("jupZ4m2GqUCJ5iueMfzQf8khFfH31d4XAQt3RzCT9Vd");
/** Jupiter Lend liquidity layer; JupLend AMM pools borrow and supply through it. */
export const JUPLEND_LIQUIDITY = new PublicKey("jupeiUmn818Jg1ekPURTpr4mFo29p46vygyykFJ3wZC");
/** Passed read-only as the last account of every observed JupLend AMM swap. */
const JUPLEND_LAST = new PublicKey("jupnw4B6Eqs7ft6rxpzYLJZYSnrpRgPcr589n5Kv4oc");
/** GoonFi V2 program state shared by every observed pool (719,000 bytes, owned by the program). */
export const GOONFI_STATE = new PublicKey("BNrK9LpEn65QA4TyBLVSMdngW3XHj3xLfFPwGdCBv8wV");
/** Validator vote account GoonFi, BisonFi and Kipseli swaps read; Jito refuses bundles that lock vote accounts. */
export const GOONFI_VOTE = new PublicKey("J1to1yufRnoWn81KYg1XkTWzmKjnYSnmE2VY8DGUJ9Qv");
export const BISONFI = new PublicKey("BiSoNHVpsVZW2F7rx2eQ59yQwKxzU5NvBcmKshCSUypi");
/** Per-slot market account (owned by tickUcsEQegChaAuo9VYQQztB4ZGApY6ZT4FkULWY6N) passed last in every Jupiter-routed BisonFi swap. */
export const BISONFI_TICK = new PublicKey("4cG31VNF9TzFinNc7BmnjhFvGjxkY3sCETVMtMgbrhPs");
export const KIPSELI = new PublicKey("3TK9D8aoBFYjYZtKCjciPrVrRStsnvo7KmpcJqDavpaU");
/** Jupiter v6 router; GoonFi V2 and Kipseli legs are quoted through its `route` instruction (see jupiterRoute). */
export const JUPITER = new PublicKey("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4");

/** Programs each venue's simulation needs, loaded (and cached) before quoting. */
export const VENUE_PROGRAMS: Record<QuoteVenue, PublicKey[]> = {
  whirlpool: [WHIRLPOOL], "raydium-clmm": [RAYDIUM_CLMM], pancakeswap: [PANCAKESWAP], manifest: [MANIFEST],
  goonfi: [GOONFI, JUPITER], deriverse: [DERIVERSE], juplend: [JUPLEND_AMM, JUPLEND_LIQUIDITY, JUPLEND_LAST], bisonfi: [BISONFI],
  kipseli: [KIPSELI, JUPITER],
};

const anchorAccount = (name: string) => createHash("sha256").update(`account:${name}`).digest().subarray(0, 8);

/** Account layouts used to validate and discover pools: owner, exact size (when fixed), discriminator, mint offsets. */
export const VENUE_LAYOUTS: Record<QuoteVenue, { program: PublicKey; size?: number; discriminator: Buffer; mintA: number; mintB: number }> = {
  whirlpool: { program: WHIRLPOOL, size: 653, discriminator: anchorAccount("Whirlpool"), mintA: 101, mintB: 181 },
  "raydium-clmm": { program: RAYDIUM_CLMM, size: 1544, discriminator: anchorAccount("PoolState"), mintA: 73, mintB: 105 },
  pancakeswap: { program: PANCAKESWAP, size: 1544, discriminator: anchorAccount("PoolState"), mintA: 73, mintB: 105 },
  // Manifest markets grow as orders are added; the fixed header is 256 bytes.
  manifest: { program: MANIFEST, discriminator: Buffer.from("f0af618f939f7143", "hex"), mintA: 16, mintB: 48 },
  goonfi: { program: GOONFI, size: 2048, discriminator: Buffer.from("30bc2f353458329a", "hex"), mintA: 80, mintB: 112 },
  // Deriverse instrument accounts start with u32 tag 7 (instr) and u32 version 1.
  deriverse: { program: DERIVERSE, discriminator: Buffer.from("0700000001000000", "hex"), mintA: 416, mintB: 448 },
  juplend: { program: JUPLEND_AMM, size: 329, discriminator: Buffer.from("ec1eb550d1d919a3", "hex"), mintA: 11, mintB: 43 },
  bisonfi: { program: BISONFI, size: 2048, discriminator: Buffer.from("POOLSTAT"), mintA: 184, mintB: 216 },
  kipseli: { program: KIPSELI, size: 864, discriminator: anchorAccount("Pool"), mintA: 72, mintB: 104 },
};

const meta = (pubkey: PublicKey, isWritable = false, isSigner = false) => ({ pubkey, isWritable, isSigner });
const key = (d: Buffer, at: number) => new PublicKey(d.subarray(at, at + 32));
const short = (k: PublicKey) => k.toBase58().slice(0, 6);
const u64 = (v: bigint) => { const b = Buffer.alloc(8); b.writeBigUInt64LE(v); return b; };
const u16 = (v: number) => { const b = Buffer.alloc(2); b.writeUInt16LE(v); return b; };
const sideOf = (p: SwapParams, a: PublicKey, b: PublicKey, address: PublicKey) => {
  if (p.mintIn.equals(a)) return true;
  if (p.mintIn.equals(b)) return false;
  throw new Error(`${short(address)} does not trade ${p.mintIn.toBase58()}`);
};
const fixedPool = (pool: Omit<QuotePool, "dynamicAccounts" | "tradable" | "feeBps"> & Partial<Pick<QuotePool, "tradable">>): QuotePool =>
  ({ feeBps: 0, dynamicAccounts: () => [], tradable: () => true, ...pool });

export function quoteWhirlpoolModel(address: PublicKey, data: Buffer): QuotePool { return whirlpoolModel(address, data); }
export function quoteRaydiumModel(address: PublicKey, data: Buffer, starts: number[]): QuotePool { return raydiumClmmModel(address, data, starts); }

/** PancakeSwap's Solana CLMM: Raydium CLMM's layout, instructions and PDA seeds under another program id. */
export function pancakeswapModel(address: PublicKey, data: Buffer, starts: number[]): QuotePool {
  return { ...raydiumClmmModel(address, data, starts, PANCAKESWAP), venue: "pancakeswap", label: `PancakeSwap ${short(address)}` };
}

// ---------------------------------------------------------------- Manifest (CKS-Systems/manifest)
const manifestPda = (seeds: Buffer[]) => PublicKey.findProgramAddressSync(seeds, MANIFEST)[0];
const MANIFEST_NIL = 0xffff_ffff;
/** Global order accounts of a mint; a swap passes them only when they exist, so global orders can be matched. */
export const manifestGlobal = (mint: PublicKey) => [manifestPda([Buffer.from("global"), mint.toBuffer()]),
  manifestPda([Buffer.from("global-vault"), mint.toBuffer()])] as const;

/** MarketFixed: base mint @16, quote mint @48, base vault @80, quote vault @112. Swap instruction tag 4. */
export function manifestModel(address: PublicKey, data: Buffer): QuotePool {
  const baseMint = key(data, 16), quoteMint = key(data, 48), baseVault = key(data, 80), quoteVault = key(data, 112);
  const globals = [manifestGlobal(baseMint), manifestGlobal(quoteMint)];
  return fixedPool({ venue: "manifest", address, mintA: baseMint, mintB: quoteMint, label: `Manifest ${short(address)}`,
    staticAccounts: [address, baseVault, quoteVault, baseMint, quoteMint, ...globals.flat()],
    // bids_root_index @156, asks_root_index @164; NIL (u32::MAX) when that side of the book is empty.
    tradable: d => d.readUInt32LE(156) !== MANIFEST_NIL || d.readUInt32LE(164) !== MANIFEST_NIL,
    swap(p) {
      const baseIn = sideOf(p, baseMint, quoteMint, address);
      const [traderBase, traderQuote] = baseIn ? [p.ataIn, p.ataOut] : [p.ataOut, p.ataIn];
      return new TransactionInstruction({ programId: MANIFEST, keys: [
        meta(p.owner, true, true), meta(address, true), meta(SystemProgram.programId), meta(traderBase, true), meta(traderQuote, true),
        meta(baseVault, true), meta(quoteVault, true), meta(p.tokenProgram(baseMint)), meta(baseMint), meta(p.tokenProgram(quoteMint)), meta(quoteMint),
        ...globals.filter(([global]) => p.exists(global)).flatMap(([global, vault]) => [meta(global, true), meta(vault, true)])],
        // in_atoms, out_atoms (minimum out for exact input), is_base_in, is_exact_in
        data: Buffer.concat([Buffer.from([4]), u64(p.amount), u64(p.minOut), Buffer.from([baseIn ? 1 : 0, 1])]) });
    } });
}

// ---------------------------------------------------------------- One-step Jupiter v6 routes
const JUPITER_ROUTE = Buffer.from("e517cb977ae3ad2a", "hex");
const JUPITER_EVENT_AUTHORITY = PublicKey.findProgramAddressSync([Buffer.from("__event_authority")], JUPITER)[0];
/** Kipseli refuses Jupiter routes with less than ~10 bps of slippage tolerance (NotSupport); no venue's price depends on it. */
const ROUTE_SLIPPAGE_BPS = 50n;

/**
 * A swap of `p` as a one-step Jupiter v6 `route`, which anyone can send: Jupiter's deployed program runs and CPIs into
 * `venue` with `accounts`. `step` is Jupiter's `Swap` enum variant followed by its fields. GoonFi V2 and Kipseli read the
 * instructions sysvar and pay swaps whose top-level program is Jupiter more than direct calls (observed 2026-10-17).
 */
function jupiterRoute(p: SwapParams, mintOut: PublicKey, step: number[], venue: PublicKey, accounts: AccountMeta[]): TransactionInstruction {
  // Jupiter requires quoted_out - floor(quoted_out * slippage_bps / 10,000) out (a 0 bp leg returning exactly its minimum
  // proved the rounding); the smallest quoted_out whose bound reaches minOut makes that bound exactly minOut.
  const bound = (quoted: bigint) => quoted - quoted * ROUTE_SLIPPAGE_BPS / 10_000n;
  let quotedOut = p.minOut * 10_000n / (10_000n - ROUTE_SLIPPAGE_BPS);
  while (bound(quotedOut) < p.minOut) quotedOut++;
  while (quotedOut > 0n && bound(quotedOut - 1n) >= p.minOut) quotedOut--;
  return new TransactionInstruction({ programId: JUPITER, keys: [
    meta(p.tokenProgram(mintOut)), meta(p.owner, false, true), meta(p.ataIn, true), meta(p.ataOut, true),
    meta(JUPITER), meta(mintOut), meta(JUPITER), meta(JUPITER_EVENT_AUTHORITY), meta(JUPITER), // no destination or platform fee account
    meta(venue), ...accounts],
    // route_plan [{ step, percent 100, input_index 0, output_index 1 }], in_amount, quoted_out_amount, slippage_bps, platform_fee_bps
    data: Buffer.concat([JUPITER_ROUTE, Buffer.from([1, 0, 0, 0, ...step, 100, 0, 1]), u64(p.amount), u64(quotedOut),
      u16(Number(ROUTE_SLIPPAGE_BPS)), Buffer.from([0])]) });
}

// ---------------------------------------------------------------- GoonFi V2 (layout observed on chain), via Jupiter
/** Jupiter's `Swap::GoonFiV2` variant; its one field is GoonFi's own quote-in flag. */
const JUPITER_GOONFI_V2 = 151;

/** Pool: base mint @80, quote mint @112, base vault @144, quote vault @176, price account @208, last trade slots @32 and
 * @40. Price account: bid, ask, u32 update slot @16, scale, millisecond timestamp. GoonFi pays Jupiter-routed swaps
 * 0.05–0.1 bp more than direct calls (more when its price is stale), so a leg is a one-step Jupiter route, which then
 * matched Jupiter's GoonFi quote to the atomic unit. Jupiter builds GoonFi's swap (tag 1: quote_in, amount_in,
 * minimum_out, flag). Prices depend on the clock: a swap in the slot of the price update or of the latest trade gets
 * ~0.75 bp less, prices worsen ~0.75 bp per slot once the update is over 50 slots old, and swaps fail (custom error
 * 0x15) once it is over 100. A vault running out fails with "insufficient funds". */
export function goonfiModel(address: PublicKey, data: Buffer): QuotePool {
  const baseMint = key(data, 80), quoteMint = key(data, 112), baseVault = key(data, 144), quoteVault = key(data, 176), price = key(data, 208);
  return fixedPool({ venue: "goonfi", address, mintA: baseMint, mintB: quoteMint, label: `GoonFi V2 ${short(address)}`,
    staticAccounts: [address, baseVault, quoteVault, baseMint, quoteMint, price, GOONFI_STATE, GOONFI_VOTE],
    minQuoteSlot(d, read) {
      const quote = read(price), updated = quote && quote.length >= 20 ? BigInt(quote.readUInt32LE(16)) : 0n;
      return [d.readBigUInt64LE(32), d.readBigUInt64LE(40), updated].reduce((a, b) => a > b ? a : b) + 1n;
    },
    swap(p) {
      const baseIn = sideOf(p, baseMint, quoteMint, address);
      const [userBase, userQuote] = baseIn ? [p.ataIn, p.ataOut] : [p.ataOut, p.ataIn];
      return jupiterRoute(p, baseIn ? quoteMint : baseMint, [JUPITER_GOONFI_V2, baseIn ? 0 : 1], GOONFI, [
        meta(p.owner, false, true), meta(address, true), meta(userBase, true), meta(userQuote, true), meta(baseVault, true), meta(quoteVault, true),
        meta(baseMint), meta(quoteMint), meta(price), meta(GOONFI_STATE), meta(SYSVAR_INSTRUCTIONS_PUBKEY),
        meta(p.tokenProgram(baseMint)), meta(p.tokenProgram(quoteMint)), meta(GOONFI_VOTE)]);
    } });
}

// ---------------------------------------------------------------- Deriverse (@deriverse/kit 1.0.76)
const DERIVERSE_VERSION = 1;
const DERIVERSE_TAG = { instr: 7, maps: 10, clientInfos: 12, bidsTree: 14, asksTree: 15, bidOrders: 16, askOrders: 17, lines: 18 } as const;
let deriverseAuthority: PublicKey | undefined;
const deriverseRoot = () => deriverseAuthority ??= PublicKey.findProgramAddressSync([Buffer.from("ndxnt")], DERIVERSE)[0];
/** Token registry entry for `mint`: the mint with the version over its last four bytes. Token id is a u32 @72. */
export function deriverseTokenState(mint: PublicKey): PublicKey {
  const seed = Buffer.from(mint.toBuffer()); seed.writeInt32LE(DERIVERSE_VERSION, 28);
  return PublicKey.findProgramAddressSync([seed, deriverseRoot().toBuffer()], DERIVERSE)[0];
}
export function deriverseInstrAccount(tag: number, assetTokenId: number, crncyTokenId: number): PublicKey {
  const seed = Buffer.alloc(16);
  seed.writeInt32LE(DERIVERSE_VERSION, 0); seed.writeInt32LE(tag, 4); seed.writeInt32LE(assetTokenId, 8); seed.writeInt32LE(crncyTokenId, 12);
  return PublicKey.findProgramAddressSync([seed, deriverseRoot().toBuffer()], DERIVERSE)[0];
}
const deriverseVault = (mint: PublicKey) => {
  const version = Buffer.alloc(4); version.writeUInt32LE(DERIVERSE_VERSION);
  return PublicKey.findProgramAddressSync([mint.toBuffer(), version], DERIVERSE)[0];
};

/** Spot instrument: instrument id @8, asset/currency token ids @12/@16, maps @352, asset mint @416, currency mint @448. */
export function deriverseModel(address: PublicKey, data: Buffer): QuotePool {
  const instrId = data.readUInt32LE(8), assetId = data.readUInt32LE(12), crncyId = data.readUInt32LE(16);
  const maps = key(data, 352), assetMint = key(data, 416), crncyMint = key(data, 448);
  const [assetVault, crncyVault] = [deriverseVault(assetMint), deriverseVault(crncyMint)];
  const account = (tag: number) => deriverseInstrAccount(tag, assetId, crncyId);
  const T = DERIVERSE_TAG;
  const [bidsTree, asksTree, bidOrders, askOrders, lines, clientInfos] = [T.bidsTree, T.asksTree, T.bidOrders, T.askOrders, T.lines, T.clientInfos].map(account);
  return fixedPool({ venue: "deriverse", address, mintA: assetMint, mintB: crncyMint, label: `Deriverse ${short(address)}`,
    staticAccounts: [address, assetVault, crncyVault, assetMint, crncyMint, bidsTree, asksTree, bidOrders, askOrders, lines, maps, clientInfos],
    swap(p) {
      const assetIn = sideOf(p, assetMint, crncyMint, address);
      const [userAsset, userCrncy] = assetIn ? [p.ataIn, p.ataOut] : [p.ataOut, p.ataIn];
      // Selling the asset fills bids; buying it with the currency fills asks.
      const [tree, orders] = assetIn ? [bidsTree, bidOrders] : [asksTree, askOrders];
      const assetProgram = p.tokenProgram(assetMint), crncyProgram = p.tokenProgram(crncyMint);
      const d = Buffer.alloc(32);
      d[0] = 26; d[1] = assetIn ? 0 : 1; d.writeUInt32LE(instrId, 4);
      d.writeBigInt64LE(0n, 8); // limit price 0: none
      d.writeBigInt64LE(p.amount, 16); d.writeBigInt64LE(p.minOut, 24);
      return new TransactionInstruction({ programId: DERIVERSE, data: d, keys: [
        meta(p.owner, false, true), meta(assetMint), meta(crncyMint), meta(assetVault, true), meta(crncyVault, true),
        meta(address, true), meta(tree, true), meta(orders, true), meta(lines, true), meta(maps, true), meta(clientInfos, true),
        meta(userAsset, true), meta(userCrncy, true), meta(assetProgram), ...(assetProgram.equals(crncyProgram) ? [] : [meta(crncyProgram)]),
        meta(SystemProgram.programId), meta(ASSOCIATED_TOKEN_PROGRAM_ID)] });
    } });
}

// ---------------------------------------------------------------- JupLend AMM (layout observed on chain)
const liquidityPda = (seeds: Buffer[]) => PublicKey.findProgramAddressSync(seeds, JUPLEND_LIQUIDITY)[0];
const JUPLEND_SWAP = Buffer.from("8dac0ad04509389a", "hex");

/** Pool: mint A @11, mint B @43. Reserves, rate models and the pool's supply/borrow positions are liquidity-layer PDAs. */
export function juplendModel(address: PublicKey, data: Buffer): QuotePool {
  const mintA = key(data, 11), mintB = key(data, 43);
  const liquidity = liquidityPda([Buffer.from("liquidity")]);
  const per = (mint: PublicKey) => ({
    reserve: liquidityPda([Buffer.from("reserve"), mint.toBuffer()]), rate: liquidityPda([Buffer.from("rate_model"), mint.toBuffer()]),
    vault: getAssociatedTokenAddressSync(mint, liquidity, true),
    supply: liquidityPda([Buffer.from("user_supply_position"), mint.toBuffer(), address.toBuffer()]),
    borrow: liquidityPda([Buffer.from("user_borrow_position"), mint.toBuffer(), address.toBuffer()]) });
  const a = per(mintA), b = per(mintB);
  return fixedPool({ venue: "juplend", address, mintA, mintB, label: `JupLend AMM ${short(address)}`,
    staticAccounts: [address, mintA, mintB, a.reserve, b.reserve, a.rate, b.rate, a.vault, b.vault, a.supply, b.supply, a.borrow, b.borrow, liquidity],
    swap(p) {
      const aIn = sideOf(p, mintA, mintB, address);
      const [userA, userB] = aIn ? [p.ataIn, p.ataOut] : [p.ataOut, p.ataIn];
      return new TransactionInstruction({ programId: JUPLEND_AMM, keys: [
        meta(p.owner, true, true), meta(address, true), meta(userA, true), meta(userB, true),
        meta(p.owner, true, true), meta(userA, true), meta(userB, true), meta(mintA), meta(mintB),
        meta(a.reserve, true), meta(b.reserve, true), meta(a.rate), meta(b.rate), meta(a.vault, true), meta(b.vault, true),
        meta(a.supply, true), meta(b.supply, true), meta(a.borrow, true), meta(b.borrow, true), meta(liquidity),
        meta(JUPLEND_LIQUIDITY), meta(p.tokenProgram(mintA)), meta(p.tokenProgram(mintB)), meta(JUPLEND_LAST)],
        // a_to_b, amount_in, minimum_out
        data: Buffer.concat([JUPLEND_SWAP, Buffer.from([aIn ? 1 : 0]), u64(p.amount), u64(p.minOut)]) });
    } });
}

// ---------------------------------------------------------------- BisonFi (layout observed on chain)
/** Pool ("POOLSTAT"): vault A @120, vault B @152, mint A @184, mint B @216; oracle updates write a valid-from slot @72.
 * Swap tag 2 is the form Jupiter routes; called directly it prices exactly as Jupiter quotes it. The program returns a
 * zero fill instead of failing while its oracle is outside [valid-from, valid-from + 2] slots or the cluster's last
 * restart slot is unknown, and fails (custom error 0x35) when invoked by a program outside its CPI allowlist. */
export function bisonfiModel(address: PublicKey, data: Buffer): QuotePool {
  const vaultA = key(data, 120), vaultB = key(data, 152), mintA = key(data, 184), mintB = key(data, 216);
  return fixedPool({ venue: "bisonfi", address, mintA, mintB, label: `BisonFi ${short(address)}`,
    staticAccounts: [address, vaultA, vaultB, mintA, mintB, GOONFI_VOTE, BISONFI_TICK],
    validFromSlot: d => d.readBigUInt64LE(72),
    swap(p) {
      const aIn = sideOf(p, mintA, mintB, address);
      const [userA, userB] = aIn ? [p.ataIn, p.ataOut] : [p.ataOut, p.ataIn];
      return new TransactionInstruction({ programId: BISONFI, keys: [
        meta(p.owner, true, true), meta(address, true), meta(vaultA, true), meta(vaultB, true), meta(userA, true), meta(userB, true),
        meta(p.tokenProgram(mintA)), meta(p.tokenProgram(mintB)), meta(mintA), meta(mintB), meta(SYSVAR_INSTRUCTIONS_PUBKEY),
        meta(GOONFI_VOTE), meta(BISONFI_TICK)],
        // amount_in, minimum_out, input side (0 = token A in)
        data: Buffer.concat([Buffer.from([2]), u64(p.amount), u64(p.minOut), Buffer.from([aIn ? 0 : 1])]) });
    } });
}

// ---------------------------------------------------------------- Kipseli (layout observed on chain), via Jupiter
/** Jupiter's `Swap::Kipseli` variant; its one field is Kipseli's own side byte (0 = quote in). */
const JUPITER_KIPSELI = 181;
/** Kipseli checks a ban-list entry for the token authority and for the payer; the entries need not exist. */
export const kipseliBanEntry = (user: PublicKey) => PublicKey.findProgramAddressSync([Buffer.from("ban"), user.toBuffer()], KIPSELI)[0];

/** Pool: base mint @72, quote mint @104 (vaults are the pool's associated token accounts), price update slot @432.
 * Kipseli pays swaps whose top-level program is Jupiter about 6 bp more than direct calls, so a leg is a one-step Jupiter
 * route. Quotes decay within a few slots of the last price update and fail (PriceExpired) soon after. */
export function kipseliModel(address: PublicKey, data: Buffer): QuotePool {
  const baseMint = key(data, 72), quoteMint = key(data, 104);
  const baseVault = getAssociatedTokenAddressSync(baseMint, address, true), quoteVault = getAssociatedTokenAddressSync(quoteMint, address, true);
  return fixedPool({ venue: "kipseli", address, mintA: baseMint, mintB: quoteMint, label: `Kipseli ${short(address)}`,
    staticAccounts: [address, baseVault, quoteVault, baseMint, quoteMint, GOONFI_VOTE],
    validFromSlot: d => d.readBigUInt64LE(432),
    swap(p) {
      const baseIn = sideOf(p, baseMint, quoteMint, address);
      const [userBase, userQuote] = baseIn ? [p.ataIn, p.ataOut] : [p.ataOut, p.ataIn];
      const ban = kipseliBanEntry(p.owner);
      return jupiterRoute(p, baseIn ? quoteMint : baseMint, [JUPITER_KIPSELI, baseIn ? 1 : 0], KIPSELI, [
        meta(p.owner, false, true), meta(address, true), meta(baseVault, true), meta(quoteVault, true), meta(baseMint),
        meta(quoteMint), meta(userBase, true), meta(userQuote, true), meta(p.tokenProgram(baseMint)), meta(SystemProgram.programId),
        meta(p.owner, false, true), meta(ban), meta(ban), meta(GOONFI_VOTE), meta(SYSVAR_INSTRUCTIONS_PUBKEY)]);
    } });
}
