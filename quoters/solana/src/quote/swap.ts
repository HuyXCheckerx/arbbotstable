import { address, getTransactionDecoder, lamports } from "@solana/kit";
import { ASSOCIATED_TOKEN_PROGRAM_ID, NATIVE_MINT, createAssociatedTokenAccountIdempotentInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { type AddressLookupTableAccount, ComputeBudgetProgram, type Connection, MessageV0, type PublicKey, SystemProgram,
  type TransactionInstruction, TransactionMessage, VersionedTransaction } from "@solana/web3.js";
import { FailedTransactionMetadata } from "litesvm";
import type { TokenInfo } from "../tokens.js";
import { V0_PACKET_LIMIT, V1_PACKET_LIMIT, compileToV1Message } from "../v1.js";
import type { SwapQuote } from "./engine.js";
import type { SnapshotQuoteMarket } from "./market.js";
import { GOONFI_VOTE, type QuotePool } from "./venues.js";

/** The runtime's per-transaction account-lock limit; lookup tables shrink bytes, not locks. */
export const MAX_ACCOUNT_LOCKS = 64;
/** Compute for one idempotent associated-token-account creation (Token-2022 included), with room to spare. */
const ATA_CREATE_UNITS = 40_000;
/** Simulated compute is exact for the simulated state; the limit leaves this much headroom for state that moves. */
const UNIT_MARGIN = 1.25;
const MAX_UNITS = 1_400_000;
/** A v1 header without a loaded-data limit gets a small default, too small for programs like Jupiter's and accounts like
 * Deriverse's maps; this is the runtime's maximum (64 MiB), what a v0 transaction gets by default. */
const MAX_LOADED_DATA = 64 * 1024 * 1024;

export type TxVersion = "v0" | "v1" | "auto";
export interface SwapBuildOptions {
  /** The wallet that signs: fee payer, token-account owner and swap authority. Only its public key is used. */
  taker: PublicKey;
  slippageBps: number;
  recentBlockhash: string;
  /** "v0" fits 1,232 bytes (with `tables`), "v1" 4,096 bytes without lookup tables, "auto" v0 first. */
  version?: TxVersion;
  tables?: AddressLookupTableAccount[];
  /** Priority fee in micro-lamports per compute unit; 0 adds none. */
  computeUnitPrice?: bigint;
}
export interface BuiltLeg {
  pool: string; venue: string; label: string; inputMint: string; outputMint: string; path: number; hop: number;
  /** `outAmount` is this leg simulated as the taker; `quotedOutAmount` is the quote's (a fresh wallet's) figure. Some venues
   * price by wallet: GoonFi V2 paid one of 16 recent traders 0.1 bp less than the rest (2026-10-18). */
  inAmount: string; outAmount: string; quotedOutAmount: string; minOutAmount: string;
  /** False when the swap locks a staked validator's vote account, which Jito bundles refuse. */
  bundleSafe: boolean;
}
export interface SwapBuild {
  legs: BuiltLeg[];
  computeBudget: TransactionInstruction[]; setup: TransactionInstruction[]; swaps: TransactionInstruction[];
  computeUnitLimit: number;
  /** What the transaction returns if every leg fills as simulated, and the least it can return without failing. */
  outAmount: bigint; minOutAmount: bigint;
  /** Intermediate tokens a two-hop path can leave behind: hop 1's output above the minimum hop 2 is fixed to spend. */
  intermediateDust: { mint: string; atMost: string }[];
  bundleSafe: boolean;
  transaction: VersionedTransaction; version: 0 | 1; bytes: number; accountLocks: number;
}

export class TransactionLimitError extends Error {
  constructor(readonly reason: "bytes" | "locks", readonly value: number | undefined, readonly limit: number) {
    super(reason === "bytes" ? `transaction ${value ?? "over"} bytes exceeds ${limit}` : `transaction locks ${value} accounts, over ${limit}`);
  }
}

const floorSlippage = (amount: bigint, slippageBps: number) => amount * BigInt(10_000 - slippageBps) / 10_000n;
const ata = (owner: PublicKey, token: TokenInfo) => getAssociatedTokenAddressSync(token.mint, owner, false, token.program);

/**
 * Turn a quote's route plan into the taker's own swap transaction. Each leg is built with the venue model the quote
 * simulated (GoonFi V2 and Kipseli legs are the same one-step Jupiter routes), for the taker's associated token accounts,
 * with its own minimum output: the quoted output less `slippageBps`. A two-hop path cannot know hop 1's output in
 * advance, so its hop-2 legs spend exactly what hop 1's minimums guarantee, split in the quoted proportions and
 * re-simulated at that size; anything hop 1 returns above its minimum stays in the taker's intermediate token account.
 * The output and intermediate accounts are created idempotently; the input account must already hold the input.
 * Nothing is signed: the transaction is returned unsigned with the taker as fee payer.
 */
export function buildSwap(market: SnapshotQuoteMarket, quote: SwapQuote, output: TokenInfo, options: SwapBuildOptions): SwapBuild {
  const { taker, slippageBps } = options;
  if (!Number.isInteger(slippageBps) || slippageBps < 0 || slippageBps > 10_000) throw new Error("Invalid slippage");
  const token = (mint: string) => {
    const found = market.tokens.find(t => String(t.mint) === mint);
    if (!found) throw new Error(`No token for mint ${mint}`);
    return found;
  };
  const model = (pool: string) => {
    const found = market.pools.find(p => String(p.address) === pool);
    if (!found) throw new Error(`Pool ${pool} is not tradable now`);
    return found;
  };
  const programs = new Map(market.tokens.map(t => [String(t.mint), t.program]));
  const tokenProgram = (mint: PublicKey) => {
    const program = programs.get(String(mint));
    if (!program) throw new Error(`No token program known for ${mint}`);
    return program;
  };
  const swapOf = (pool: QuotePool, input: TokenInfo, output: TokenInfo, amount: bigint, minOut: bigint) => pool.swap({ owner: taker,
    mintIn: input.mint, ataIn: ata(taker, input), ataOut: ata(taker, output), amount, minOut, data: market.mirror.data(pool.address)!,
    tokenProgram, exists: key => market.mirror.has(key) });
  const bundleSafe = (pool: QuotePool) => !pool.staticAccounts.some(key => key.equals(GOONFI_VOTE));

  // Every account a leg or token-account creation touches is locked; over the limit the route cannot be one transaction.
  const touched = new Set([String(taker), String(ComputeBudgetProgram.programId)]);
  for (const leg of quote.routePlan) {
    const ix = swapOf(model(leg.pool), token(leg.inputMint), token(leg.outputMint), BigInt(leg.inAmount), 1n);
    for (const key of [ix.programId, ...ix.keys.map(k => k.pubkey)]) touched.add(String(key));
  }
  for (const t of new Set([String(output.mint), ...quote.routePlan.filter(leg => leg.hop === 2).map(leg => leg.inputMint)])) {
    const mint = token(t);
    for (const key of [ata(taker, mint), mint.mint, mint.program, ASSOCIATED_TOKEN_PROGRAM_ID, SystemProgram.programId]) touched.add(String(key));
  }
  const lookedUp = new Set((options.tables ?? []).flatMap(table => table.state.addresses.map(String)));
  if (touched.size > MAX_ACCOUNT_LOCKS) throw new TransactionLimitError("locks", touched.size, MAX_ACCOUNT_LOCKS);
  if (lookedUp.size === 0 && touched.size * 32 > V1_PACKET_LIMIT) throw new TransactionLimitError("bytes", touched.size * 32, V1_PACKET_LIMIT);
  fundTaker(market, taker);
  // Each leg's output is measured as the taker and in company. GoonFi V2 and Kipseli pay their Jupiter-routed price only when
  // their Jupiter route is the transaction's only swap: beside other legs GoonFi paid every wallet tested ~0.1 bp less, and
  // Kipseli failed a 5 bp minimum (2026-10-18). So a leg's output is the change in its output-token account when all legs
  // run together, less the change without that leg. The taker's intermediate accounts hold a synthetic balance in the
  // mirror, so hop-2 legs still run when the hop-1 leg feeding them is left out.
  const order = [...quote.routePlan].sort((x, y) => x.hop - y.hop);
  const mids = [...new Set(order.filter(leg => leg.hop === 2).map(leg => leg.inputMint))].map(token);
  const watch = [output, ...mids].map(t => ata(taker, t));
  const measure = (sizes: bigint[]) => {
    const probes = order.map((leg, k) => swapOf(model(leg.pool), token(leg.inputMint), token(leg.outputMint), sizes[k], 1n));
    const whole = simulateSet(market, taker, probes, watch);
    const outs = order.map((leg, k) => {
      const key = ata(taker, token(leg.outputMint)).toBase58();
      if (order.length === 1) return whole.balances.get(key)! - FUNDED;
      return whole.balances.get(key)! - simulateSet(market, taker, probes.filter((_, j) => j !== k), watch).balances.get(key)!;
    });
    return { outs, units: whole.computeUnits };
  };
  // Round 1 at the quoted sizes sets hop 1's outputs and minimums; a two-hop path's hop 2 then spends exactly what hop 1's
  // minimums guarantee, split in the quoted proportions (the largest leg takes the rounding), and round 2 measures hop 2.
  let sizes = order.map(leg => BigInt(leg.inAmount)), measured = measure(sizes);
  const minOuts = measured.outs.map(out => floorSlippage(out, slippageBps));
  const dust = new Map<string, bigint>();
  for (const path of [...new Set(order.map(leg => leg.path))]) {
    const hop1 = order.flatMap((leg, k) => leg.path === path && leg.hop === 1 ? [k] : []), hop2 = order.flatMap((leg, k) => leg.path === path && leg.hop === 2 ? [k] : []);
    if (!hop2.length) continue;
    const guaranteed = hop1.reduce((sum, k) => sum + minOuts[k], 0n), quotedMid = hop2.reduce((sum, k) => sum + BigInt(order[k].inAmount), 0n);
    const mid = order[hop2[0]].inputMint;
    dust.set(mid, (dust.get(mid) ?? 0n) + hop1.reduce((sum, k) => sum + measured.outs[k], 0n) - guaranteed);
    for (const k of hop2) sizes[k] = BigInt(order[k].inAmount) * guaranteed / quotedMid;
    const largest = hop2.reduce((a, b) => sizes[b] > sizes[a] ? b : a);
    sizes[largest] += guaranteed - hop2.reduce((sum, k) => sum + sizes[k], 0n);
  }
  if (mids.length) {
    sizes = [...sizes];
    const second = measure(sizes);
    order.forEach((leg, k) => { if (leg.hop === 2) { measured.outs[k] = second.outs[k]; minOuts[k] = floorSlippage(second.outs[k], slippageBps); } });
    measured = { outs: measured.outs, units: second.units };
  }
  const swaps = order.map((leg, k) => swapOf(model(leg.pool), token(leg.inputMint), token(leg.outputMint), sizes[k], minOuts[k]));
  const legs: BuiltLeg[] = order.map((leg, k) => ({ pool: leg.pool, venue: leg.venue, label: leg.label, inputMint: leg.inputMint,
    outputMint: leg.outputMint, path: leg.path, hop: leg.hop, inAmount: sizes[k].toString(), outAmount: measured.outs[k].toString(),
    quotedOutAmount: leg.outAmount, minOutAmount: minOuts[k].toString(), bundleSafe: bundleSafe(model(leg.pool)) }));
  const final = order.flatMap((leg, k) => leg.outputMint === String(output.mint) ? [k] : []);
  const outAmount = final.reduce((sum, k) => sum + measured.outs[k], 0n), minOutAmount = final.reduce((sum, k) => sum + minOuts[k], 0n);
  const units = measured.units, intermediates = new Map(mids.map(t => [String(t.mint), t]));
  const setup = [output, ...intermediates.values()].map(t => createAssociatedTokenAccountIdempotentInstruction(taker, ata(taker, t), taker, t.mint, t.program));
  const computeUnitLimit = Math.min(MAX_UNITS, Math.ceil((units + setup.length * ATA_CREATE_UNITS) * UNIT_MARGIN));
  const price = options.computeUnitPrice ?? 0n;
  const computeBudget = [ComputeBudgetProgram.setComputeUnitLimit({ units: computeUnitLimit }),
    ...(price > 0n ? [ComputeBudgetProgram.setComputeUnitPrice({ microLamports: price })] : [])];
  const compiled = compile(taker, options, computeBudget, [...setup, ...swaps], computeUnitLimit, price);
  return { legs, computeBudget, setup, swaps, computeUnitLimit, outAmount, minOutAmount,
    intermediateDust: [...dust].map(([mint, atMost]) => ({ mint, atMost: (atMost > 0n ? atMost : 0n).toString() })),
    bundleSafe: legs.every(leg => leg.bundleSafe), ...compiled };
}

/** `watch` balances after `instructions` run together as one v1 transaction signed by the taker (funded in the mirror). */
function simulateSet(market: SnapshotQuoteMarket, taker: PublicKey, instructions: TransactionInstruction[], watch: PublicKey[]) {
  const tx = new VersionedTransaction(compileToV1Message({ payerKey: taker, recentBlockhash: market.mirror.svm.latestBlockhash(), instructions,
    config: { computeUnitLimit: MAX_UNITS, loadedAccountsDataSizeLimit: MAX_LOADED_DATA } }));
  const result = simulateSerialized(market.mirror.svm, tx.serialize(), watch);
  if (!result.ok) throw new Error(`The route's legs fail together for the taker: ${result.error}`);
  return result;
}

/** Synthetic balance of every taker token account in the mirror while legs are simulated as the taker. */
const FUNDED = 1n << 60n;

/** Give the taker, in the mirror only, SOL and an associated token account for every market token holding FUNDED. The
 * accounts are committed to the mirror (never streamed from chain), so later simulations as the taker can reuse them. */
function fundTaker(market: SnapshotQuoteMarket, taker: PublicKey): void {
  const svm = market.mirror.svm, owner = address(taker.toBase58()), funded = svm.getAccount(owner);
  if (!funded.exists || funded.lamports < 1_000_000_000n) svm.airdrop(owner, lamports(10_000_000_000n));
  const missing = market.tokens.filter(t => !svm.getAccount(address(ata(taker, t).toBase58())).exists);
  if (missing.length) {
    const create = new VersionedTransaction(new TransactionMessage({ payerKey: taker, recentBlockhash: svm.latestBlockhash(),
      instructions: missing.map(t => createAssociatedTokenAccountIdempotentInstruction(taker, ata(taker, t), taker, t.mint, t.program)) })
      .compileToV0Message());
    const created = svm.sendTransaction(getTransactionDecoder().decode(create.serialize()));
    if (created instanceof FailedTransactionMetadata) throw new Error(`Could not create the taker's token accounts: ${String(created.err())}`);
  }
  for (const t of market.tokens) {
    const account = svm.getAccount(address(ata(taker, t).toBase58()));
    if (!account.exists) throw new Error(`The taker's ${t.symbol} account is missing in the mirror`);
    const data = Buffer.from(account.data);
    data.writeBigUInt64LE(FUNDED, 64);
    // A wrapped-SOL account's token amount must be backed by its lamports (rent-exempt minimum plus the amount).
    svm.setAccount({ ...account, data, ...(t.mint.equals(NATIVE_MINT) ? { lamports: lamports(WSOL_RENT + FUNDED) } : {}) });
  }
}
/** Rent-exempt minimum of a 165-byte token account. */
const WSOL_RENT = 2_039_280n;

/** v0 (with lookup tables) when it fits 1,232 bytes; v1 (4,096 bytes, compute budget in its header) when allowed. */
function compile(payer: PublicKey, options: SwapBuildOptions, budget: TransactionInstruction[], instructions: TransactionInstruction[],
  computeUnitLimit: number, price: bigint): { transaction: VersionedTransaction; version: 0 | 1; bytes: number; accountLocks: number } {
  const version = options.version ?? "auto", tables = options.tables ?? [];
  let v0Bytes: number | undefined;
  if (version !== "v1") {
    try {
      const transaction = new VersionedTransaction(new TransactionMessage({ payerKey: payer, recentBlockhash: options.recentBlockhash,
        instructions: [...budget, ...instructions] }).compileToV0Message(tables));
      const message = transaction.message as MessageV0;
      const accountLocks = message.staticAccountKeys.length
        + message.addressTableLookups.reduce((sum, lookup) => sum + lookup.writableIndexes.length + lookup.readonlyIndexes.length, 0);
      if (accountLocks > MAX_ACCOUNT_LOCKS) throw new TransactionLimitError("locks", accountLocks, MAX_ACCOUNT_LOCKS);
      const bytes = transaction.serialize().length;
      if (bytes <= V0_PACKET_LIMIT) return { transaction, version: 0, bytes, accountLocks };
      v0Bytes = bytes;
    } catch (error) {
      if (error instanceof TransactionLimitError) throw error;
      if (!(error instanceof RangeError || /overrun|too large|exceed|out of range|encoding/i.test((error as Error).message))) throw error;
    }
    if (version === "v0") throw new TransactionLimitError("bytes", v0Bytes, V0_PACKET_LIMIT);
  }
  if (tables.length) throw new Error("v1 transactions cannot use address lookup tables");
  const transaction = new VersionedTransaction(compileToV1Message({ payerKey: payer, recentBlockhash: options.recentBlockhash, instructions,
    config: { computeUnitLimit, priorityFee: price > 0n ? Number(price) : null, loadedAccountsDataSizeLimit: MAX_LOADED_DATA } }));
  const accountLocks = transaction.message.staticAccountKeys.length;
  if (accountLocks > MAX_ACCOUNT_LOCKS) throw new TransactionLimitError("locks", accountLocks, MAX_ACCOUNT_LOCKS);
  let bytes: number;
  try { bytes = transaction.serialize().length; }
  catch { throw new TransactionLimitError("bytes", undefined, V1_PACKET_LIMIT); }
  if (bytes > V1_PACKET_LIMIT) throw new TransactionLimitError("bytes", bytes, V1_PACKET_LIMIT);
  return { transaction, version: 1, bytes, accountLocks };
}

export interface SwapSimulation { ok: boolean; error?: string; outAmount: bigint; computeUnits: number; logs?: string[] }

/**
 * Run the whole unsigned transaction in the mirror as the taker would send it, with synthetic funding: the taker gets
 * SOL for fees and rent and an input token account holding exactly the input (created and committed in the mirror;
 * only the taker's own accounts are written). The output is the change in the taker's output token account.
 */
export function simulateSwapLocally(market: SnapshotQuoteMarket, build: SwapBuild, input: TokenInfo, output: TokenInfo,
  amount: bigint, taker: PublicKey): SwapSimulation {
  const svm = market.mirror.svm, owner = address(taker.toBase58()), source = ata(taker, input), target = ata(taker, output);
  const funded = svm.getAccount(owner);
  if (!funded.exists || funded.lamports < 1_000_000_000n) svm.airdrop(owner, lamports(10_000_000_000n));
  const create = new VersionedTransaction(new TransactionMessage({ payerKey: taker, recentBlockhash: svm.latestBlockhash(),
    instructions: [createAssociatedTokenAccountIdempotentInstruction(taker, source, taker, input.mint, input.program)] }).compileToV0Message());
  const created = svm.sendTransaction(getTransactionDecoder().decode(create.serialize()));
  if (created instanceof FailedTransactionMetadata) throw new Error(`Could not create the taker's input account: ${String(created.err())}`);
  const account = svm.getAccount(address(source.toBase58()));
  if (!account.exists) throw new Error("The taker's input account is missing after creation");
  const data = Buffer.from(account.data);
  data.writeBigUInt64LE(amount, 64);
  svm.setAccount({ ...account, data, ...(input.mint.equals(NATIVE_MINT) ? { lamports: lamports(WSOL_RENT + amount) } : {}) });
  const before = (() => { const a = svm.getAccount(address(target.toBase58())); return a.exists ? Buffer.from(a.data).readBigUInt64LE(64) : 0n; })();
  // The returned transaction carries a real recent blockhash; the mirror only accepts its own.
  const local = rebuildWithBlockhash(build, svm.latestBlockhash(), taker);
  const result = build.version === 1 ? simulateSerialized(svm, local.serialize(), [target]) : market.mirror.simulateTransaction(local, [target]);
  const after = result.balances.get(target.toBase58()) ?? before;
  return { ok: result.ok, error: result.error, outAmount: after - before, computeUnits: result.computeUnits, logs: result.ok ? undefined : result.logs.slice(-12) };
}

type Inner = { simulateVersionedTransaction(bytes: Uint8Array): unknown };
type InnerResult = { meta(): { logs(): string[]; computeUnitsConsumed(): bigint }; err?(): unknown;
  postAccounts?(): { address: Uint8Array; account(): { data(): Uint8Array } }[] };
/** @solana/kit cannot decode v1 transactions, so a v1 transaction goes to LiteSVM's binding as bytes. Token balances of
 * `watch` afterwards (an account the transaction did not write keeps its current balance). */
function simulateSerialized(svm: SnapshotQuoteMarket["mirror"]["svm"], bytes: Uint8Array, watch: PublicKey[]) {
  const inner = (svm as unknown as { inner: Inner }).inner;
  const result = inner.simulateVersionedTransaction(bytes) as InnerResult;
  const meta = result.meta(), logs = meta.logs(), computeUnits = Number(meta.computeUnitsConsumed()), balances = new Map<string, bigint>();
  if (result instanceof FailedTransactionMetadata || !result.postAccounts) {
    return { ok: false, error: logs.find(l => /Error Message:|Error:|failed/.test(l)) ?? String(result.err?.()), logs, computeUnits, balances };
  }
  const post = result.postAccounts();
  for (const key of watch) {
    const entry = post.find(a => key.toBuffer().equals(a.address)), current = svm.getAccount(address(key.toBase58()));
    const data = entry ? Buffer.from(entry.account().data()) : current.exists ? Buffer.from(current.data) : undefined;
    if (data && data.length >= 72) balances.set(key.toBase58(), data.readBigUInt64LE(64));
  }
  return { ok: true, error: undefined, logs, computeUnits, balances };
}

/** Simulate the unsigned transaction over RPC against the taker's real balances (no signature check, fresh blockhash). */
export async function simulateSwapOnRpc(connection: Connection, build: SwapBuild, output: TokenInfo, taker: PublicKey): Promise<SwapSimulation> {
  const target = ata(taker, output);
  const before = await connection.getAccountInfo(target, "processed");
  const result = await connection.simulateTransaction(build.transaction, { sigVerify: false, replaceRecentBlockhash: true,
    commitment: "processed", accounts: { encoding: "base64", addresses: [target.toBase58()] } });
  const value = result.value;
  const post = value.accounts?.[0];
  const after = post ? Buffer.from(post.data[0], "base64").readBigUInt64LE(64) : 0n;
  const start = before && before.data.length >= 72 ? before.data.readBigUInt64LE(64) : 0n;
  return { ok: !value.err, error: value.err ? JSON.stringify(value.err) : undefined, outAmount: value.err ? 0n : after - start,
    computeUnits: value.unitsConsumed ?? 0, logs: value.err ? (value.logs ?? []).slice(-12) : undefined };
}

function rebuildWithBlockhash(build: SwapBuild, blockhash: string, taker: PublicKey): VersionedTransaction {
  if (build.version === 1) {
    return new VersionedTransaction(compileToV1Message({ payerKey: taker, recentBlockhash: blockhash, instructions: [...build.setup, ...build.swaps],
      config: { computeUnitLimit: build.computeUnitLimit, loadedAccountsDataSizeLimit: MAX_LOADED_DATA } }));
  }
  const message = build.transaction.message as MessageV0;
  return new VersionedTransaction(new MessageV0({ ...message, recentBlockhash: blockhash, header: message.header,
    staticAccountKeys: message.staticAccountKeys, compiledInstructions: message.compiledInstructions, addressTableLookups: message.addressTableLookups }));
}

/** An instruction as JSON, in the shape Jupiter's /swap-instructions uses. */
export const instructionJson = (ix: TransactionInstruction) => ({ programId: ix.programId.toBase58(),
  accounts: ix.keys.map(k => ({ pubkey: k.pubkey.toBase58(), isSigner: k.isSigner, isWritable: k.isWritable })),
  data: Buffer.from(ix.data).toString("base64") });
