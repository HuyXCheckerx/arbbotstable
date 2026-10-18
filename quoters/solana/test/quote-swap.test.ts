import test from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { AccountLayout, MintLayout, TOKEN_PROGRAM_ID, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { type Connection, Keypair, PublicKey, SystemProgram, TransactionInstruction } from "@solana/web3.js";
import { address, lamports } from "@solana/kit";
import { SnapshotQuoteMarket } from "../src/quote/market.js";
import type { SwapQuote } from "../src/quote/engine.js";
import { TransactionLimitError, buildSwap, instructionJson, simulateSwapLocally } from "../src/quote/swap.js";
import { GOONFI_VOTE, type QuotePool } from "../src/quote/venues.js";
import type { TokenInfo } from "../src/tokens.js";

// The repo's TEST ONLY mock router (npm run build:test-router) stands in for a venue: it takes `amount_in` from the
// taker into one vault and pays amount_in * rate / 1e6 from another, both vaults owned by its PDA ["pool"].
const ROUTER_SO = resolve("artifacts/test/mock_router.so");
const skip = existsSync(ROUTER_SO) ? false : "build the mock router first: npm run build:test-router";
const ROUTER = Keypair.generate().publicKey;
const AUTHORITY = PublicKey.findProgramAddressSync([Buffer.from("pool")], ROUTER)[0];
const BLOCKHASH = "11111111111111111111111111111111";

function setup() {
  const tokens: TokenInfo[] = ["AAA", "BBB", "MID"].map(symbol => ({ symbol, mint: Keypair.generate().publicKey, decimals: 6, program: TOKEN_PROGRAM_ID }));
  const [a, b, mid] = tokens;
  const market = new SnapshotQuoteMarket({} as Connection, a, b, undefined, [mid]);
  const svm = market.mirror.svm;
  market.mirror.addProgramFile(ROUTER, ROUTER_SO);
  const put = (key: PublicKey, owner: PublicKey, data: Buffer) => svm.setAccount({ address: address(key.toBase58()), lamports: lamports(10_000_000n),
    programAddress: address(owner.toBase58()), data, executable: false, space: BigInt(data.length) });
  for (const t of tokens) {
    const data = Buffer.alloc(MintLayout.span);
    MintLayout.encode({ mintAuthorityOption: 1, mintAuthority: AUTHORITY, supply: 1n << 62n, decimals: 6, isInitialized: true,
      freezeAuthorityOption: 0, freezeAuthority: PublicKey.default }, data);
    put(t.mint, TOKEN_PROGRAM_ID, data);
  }
  const vault = (t: TokenInfo) => {
    const key = Keypair.generate().publicKey, data = Buffer.alloc(AccountLayout.span);
    AccountLayout.encode({ mint: t.mint, owner: AUTHORITY, amount: 1n << 60n, delegateOption: 0, delegate: PublicKey.default, state: 1,
      isNativeOption: 0, isNative: 0n, delegatedAmount: 0n, closeAuthorityOption: 0, closeAuthority: PublicKey.default }, data);
    put(key, TOKEN_PROGRAM_ID, data);
    return key;
  };
  /** A pool paying `ratePpm` per million in both directions; `extra` adds read-only accounts to its swap. */
  const pool = (x: TokenInfo, y: TokenInfo, ratePpm: bigint, extra: Partial<QuotePool> & { padding?: number } = {}): QuotePool => {
    const address = Keypair.generate().publicKey, vaults = new Map([[String(x.mint), vault(x)], [String(y.mint), vault(y)]]);
    put(address, ROUTER, Buffer.alloc(8));
    const padding = Array.from({ length: extra.padding ?? 0 }, () => Keypair.generate().publicKey);
    return { venue: "whirlpool", address, mintA: x.mint, mintB: y.mint, feeBps: 0, label: `mock ${String(address).slice(0, 4)}`,
      staticAccounts: [], dynamicAccounts: () => [], tradable: () => true, ...extra,
      swap: p => {
        const out = p.mintIn.equals(x.mint) ? y.mint : x.mint, data = Buffer.alloc(50);
        data.writeBigUInt64LE(p.amount, 8); data.writeBigUInt64LE(1_000_000n, 34); data.writeBigUInt64LE(ratePpm, 42);
        const ro = (pubkey: PublicKey) => ({ pubkey, isSigner: false, isWritable: false });
        const keys = [{ pubkey: p.owner, isSigner: true, isWritable: false }, { pubkey: p.ataIn, isSigner: false, isWritable: true },
          { pubkey: p.ataOut, isSigner: false, isWritable: true }, ro(address), ro(SystemProgram.programId), ro(TOKEN_PROGRAM_ID), ro(TOKEN_PROGRAM_ID),
          ro(SystemProgram.programId), ro(SystemProgram.programId), ro(SystemProgram.programId),
          { pubkey: vaults.get(String(p.mintIn))!, isSigner: false, isWritable: true }, { pubkey: vaults.get(String(out))!, isSigner: false, isWritable: true },
          ro(AUTHORITY), ...padding.map(ro)];
        return new TransactionInstruction({ programId: padding.length ? SystemProgram.programId : ROUTER, keys, data });
      } };
  };
  return { market, a, b, mid, pool };
}
const leg = (pool: QuotePool, input: TokenInfo, output: TokenInfo, inAmount: bigint, outAmount: bigint, path = 0, hop = 1) => ({
  pool: String(pool.address), venue: pool.venue, label: pool.label, inputMint: String(input.mint), outputMint: String(output.mint),
  inAmount: inAmount.toString(), outAmount: outAmount.toString(), shareBps: 0, hopShareBps: 0, computeUnits: 20_000, path, hop });
const quoteOf = (legs: ReturnType<typeof leg>[]) => ({ routePlan: legs }) as unknown as SwapQuote;
const taker = () => Keypair.generate().publicKey;

test("a single leg becomes [compute budget, output account creation, swap] for the taker, and runs whole", { skip }, () => {
  const { market, a, b, pool } = setup();
  try {
    const p = pool(a, b, 999_000n);
    market.pools = [p];
    const wallet = taker(), amount = 1_000_000_000n;
    const build = buildSwap(market, quoteOf([leg(p, a, b, amount, 999_000_000n)]), b, { taker: wallet, slippageBps: 5, recentBlockhash: BLOCKHASH });
    assert.equal(build.outAmount, 999_000_000n);
    assert.equal(build.minOutAmount, 999_000_000n * 9_995n / 10_000n);
    assert.deepEqual(build.legs.map(l => [l.inAmount, l.outAmount, l.minOutAmount]), [["1000000000", "999000000", "998500500"]]);
    assert.equal(build.version, 0);
    assert.equal(build.setup.length, 1, "only the output account is created; the input account must hold the input");
    assert.ok(build.setup[0].keys[1].pubkey.equals(getAssociatedTokenAddressSync(b.mint, wallet)));
    assert.equal(build.transaction.message.staticAccountKeys[0].toBase58(), wallet.toBase58(), "the taker pays the fee");
    assert.equal(build.transaction.signatures.length, 1);
    assert.ok(build.transaction.signatures[0].every(byte => byte === 0), "nothing is signed");
    assert.equal(build.bundleSafe, true);
    const sim = simulateSwapLocally(market, build, a, b, amount, wallet);
    assert.equal(sim.ok, true, sim.error);
    assert.equal(sim.outAmount, build.outAmount);
    assert.deepEqual(instructionJson(build.swaps[0]).accounts[0], { pubkey: wallet.toBase58(), isSigner: true, isWritable: false });
  } finally { market.close(); }
});

test("a split measures each leg in company and flags vote-account venues", { skip }, () => {
  const { market, a, b, pool } = setup();
  try {
    const split = [pool(a, b, 999_000n), pool(a, b, 998_000n), pool(a, b, 997_000n, { staticAccounts: [GOONFI_VOTE] })];
    market.pools = split;
    const amounts = [600_000_000n, 300_000_000n, 100_000_000n], wallet = taker();
    const build = buildSwap(market, quoteOf(split.map((p, i) => leg(p, a, b, amounts[i], 0n))), b, { taker: wallet, slippageBps: 10, recentBlockhash: BLOCKHASH });
    assert.deepEqual(build.legs.map(l => l.outAmount), ["599400000", "299400000", "99700000"], "each leg's own output, as the taker");
    assert.equal(build.outAmount, 998_500_000n);
    assert.deepEqual(build.legs.map(l => l.bundleSafe), [true, true, false]);
    assert.equal(build.bundleSafe, false);
    const sim = simulateSwapLocally(market, build, a, b, 1_000_000_000n, wallet);
    assert.equal(sim.ok, true, sim.error);
    assert.equal(sim.outAmount, build.outAmount);
  } finally { market.close(); }
});

test("transactions over 64 account locks or the packet size are refused with TransactionLimitError", { skip }, () => {
  const { market, a, b, pool } = setup();
  try {
    const wide = pool(a, b, 999_000n, { padding: 60 });
    market.pools = [wide];
    assert.throws(() => buildSwap(market, quoteOf([leg(wide, a, b, 1_000n, 999n)]), b, { taker: taker(), slippageBps: 5, recentBlockhash: BLOCKHASH }),
      (error: unknown) => error instanceof TransactionLimitError && error.reason === "locks" && error.limit === 64);
    const split = [0, 1, 2, 3, 4, 5, 6].map(i => pool(a, b, 999_000n - BigInt(i)));
    market.pools = split;
    const quote = quoteOf(split.map((p, i) => leg(p, a, b, i < 6 ? 150_000_000n : 100_000_000n, 0n)));
    assert.throws(() => buildSwap(market, quote, b, { taker: taker(), slippageBps: 5, recentBlockhash: BLOCKHASH, version: "v0" }),
      (error: unknown) => error instanceof TransactionLimitError && error.reason === "bytes" && error.limit === 1232);
    const wallet = taker(), auto = buildSwap(market, quote, b, { taker: wallet, slippageBps: 5, recentBlockhash: BLOCKHASH });
    assert.equal(auto.version, 1);
    assert.ok(auto.bytes > 1232 && auto.bytes <= 4096);
    const sim = simulateSwapLocally(market, auto, a, b, 1_000_000_000n, wallet);
    assert.equal(sim.ok, true, sim.error);
    assert.equal(sim.outAmount, auto.outAmount);
  } finally { market.close(); }
});

test("a two-hop path spends exactly hop 1's guaranteed minimum on hop 2 and reports the possible dust", { skip }, () => {
  const { market, a, b, mid, pool } = setup();
  try {
    const first = pool(a, mid, 2_000_000n), second = pool(mid, b, 499_000n);
    market.pools = [first, second];
    const wallet = taker(), amount = 1_000_000_000n;
    const quote = quoteOf([leg(first, a, mid, amount, 2_000_000_000n, 0, 1), leg(second, mid, b, 2_000_000_000n, 998_000_000n, 0, 2)]);
    const build = buildSwap(market, quote, b, { taker: wallet, slippageBps: 20, recentBlockhash: BLOCKHASH });
    const guaranteed = 2_000_000_000n * 9_980n / 10_000n;
    assert.deepEqual(build.legs.map(l => [l.hop, l.inAmount]), [[1, "1000000000"], [2, guaranteed.toString()]]);
    assert.equal(build.outAmount, guaranteed * 499_000n / 1_000_000n);
    assert.equal(build.minOutAmount, build.outAmount * 9_980n / 10_000n);
    assert.deepEqual(build.intermediateDust, [{ mint: String(mid.mint), atMost: (2_000_000_000n - guaranteed).toString() }]);
    assert.equal(build.setup.length, 2, "output and intermediate accounts");
    const sim = simulateSwapLocally(market, build, a, b, amount, wallet);
    assert.equal(sim.ok, true, sim.error);
    assert.equal(sim.outAmount, build.outAmount);
  } finally { market.close(); }
});
