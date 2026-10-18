import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { type AccountInfo, type Connection, PublicKey, type TransactionInstruction } from "@solana/web3.js";
import { discoverQuotePools, validatePoolAccount } from "../src/quote/discovery.js";
import { BISONFI, DERIVERSE, GOONFI, JUPITER, JUPLEND_AMM, KIPSELI, MANIFEST, PANCAKESWAP, VENUE_LAYOUTS, VENUE_PROGRAMS, bisonfiModel,
  deriverseInstrAccount, deriverseModel, deriverseTokenState, goonfiModel, juplendModel, kipseliBanEntry, kipseliModel, manifestGlobal,
  manifestModel, pancakeswapModel } from "../src/quote/venues.js";
import { knownToken } from "../src/tokens.js";

// Expected account lists are swaps observed on mainnet on 2026-10-17 (user accounts are whatever that trader used).
const usdc = knownToken("USDC"), usdt = knownToken("USDT");
const TOKEN = new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
const sol = { symbol: "SOL", mint: new PublicKey("So11111111111111111111111111111111111111112"), decimals: 9, program: TOKEN };
const K = (s: string) => new PublicKey(s);
const put = (d: Buffer, at: number, key: string) => K(key).toBuffer().copy(d, at);
const swapOf = (pool: { swap: (p: never) => TransactionInstruction }, p: Record<string, unknown>) =>
  pool.swap({ minOut: 0n, tokenProgram: () => TOKEN, exists: () => true, ...p } as never);
const observed = (list: string[]) => list.map(entry => { const [key, flags] = entry.split(":"); return { key, writable: flags.includes("w") }; });
const keysOf = (ix: TransactionInstruction) => ix.keys.map(k => ({ key: k.pubkey.toBase58(), writable: k.isWritable }));
const account = (data: Buffer, owner: PublicKey): AccountInfo<Buffer> => ({ data, owner, executable: false, lamports: 1, rentEpoch: 0 });

test("GoonFi V2 legs are one-step Jupiter routes that match an observed Jupiter route", () => {
  const data = Buffer.alloc(2048);
  VENUE_LAYOUTS.goonfi.discriminator.copy(data);
  put(data, 80, String(usdt.mint)); put(data, 112, String(usdc.mint));
  put(data, 144, "5dRazfLSjTq15r7XL6b5WBGTFtria1EHZQS7VZr7BD5V"); put(data, 176, "F9xyBfChZ2uCv7aQujJCe6gKVx7ydCmxfh2ZWrNwFoKr");
  put(data, 208, "4V7iPfdqmjs2aAkS3BUDoRd9MuDBoTm5tETLC7fEDkjd");
  const pool = goonfiModel(K("EEUNhHsRoUVgJUFpkupmdF4v7uLUw1zhYLp7u9s8zFqG"), data);
  assert.deepEqual(VENUE_PROGRAMS.goonfi.map(String), [String(GOONFI), String(JUPITER)]);
  // Jupiter `route` tx YZtWn8oN…: one step swapping 0.998157 USDC (quote) for USDT on this pool.
  const params = { owner: K("CkmnvUw7rnAahLxoJYYd5Mrit6EFYbxkmWM1f8HEkWcS"), mintIn: usdc.mint, amount: 998_157n, data,
    ataIn: K("FZaabW8VG2PvVm8iVnUxdAGQWKibAdzA1JedMV74vWiY"), ataOut: K("8Z8UHDGmvEovp4Tb6SAyZqtdwTiZCWGUQ55kfj4cmQGZ") };
  const ix = swapOf(pool, params);
  assert.ok(ix.programId.equals(JUPITER));
  assert.deepEqual(keysOf(ix), observed(["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "CkmnvUw7rnAahLxoJYYd5Mrit6EFYbxkmWM1f8HEkWcS:s",
    "FZaabW8VG2PvVm8iVnUxdAGQWKibAdzA1JedMV74vWiY:w", "8Z8UHDGmvEovp4Tb6SAyZqtdwTiZCWGUQ55kfj4cmQGZ:w", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4:",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB:", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4:", "D8cy77BBepLMngZx6ZukaTff5hCt1HrWyKk3Hnd9oitf:",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4:",
    "goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE:", "CkmnvUw7rnAahLxoJYYd5Mrit6EFYbxkmWM1f8HEkWcS:s", "EEUNhHsRoUVgJUFpkupmdF4v7uLUw1zhYLp7u9s8zFqG:w",
    "8Z8UHDGmvEovp4Tb6SAyZqtdwTiZCWGUQ55kfj4cmQGZ:w", "FZaabW8VG2PvVm8iVnUxdAGQWKibAdzA1JedMV74vWiY:w", "5dRazfLSjTq15r7XL6b5WBGTFtria1EHZQS7VZr7BD5V:w",
    "F9xyBfChZ2uCv7aQujJCe6gKVx7ydCmxfh2ZWrNwFoKr:w", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB:", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:",
    "4V7iPfdqmjs2aAkS3BUDoRd9MuDBoTm5tETLC7fEDkjd:", "BNrK9LpEn65QA4TyBLVSMdngW3XHj3xLfFPwGdCBv8wV:", "Sysvar1nstructions1111111111111111111111111:",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "J1to1yufRnoWn81KYg1XkTWzmKjnYSnmE2VY8DGUJ9Qv:"]));
  assert.deepEqual(ix.keys.flatMap((k, i) => k.isSigner ? [i] : []), [1, 10]);
  // route discriminator, one step (observed bytes 97 01 64 00 01: GoonFiV2 { quote_in }), in_amount, quoted_out 0 (minOut 0),
  // slippage 50 bps, no fee. The observed route quoted 999462 out at 30 bps; GoonFi's price depends on neither.
  assert.equal(ix.data.toString("hex"), "e517cb977ae3ad2a" + "01000000" + "9701640001" + "0d3b0f0000000000" + "0000000000000000" + "3200" + "00");
  const reverse = swapOf(pool, { ...params, mintIn: usdt.mint, ataIn: params.ataOut, ataOut: params.ataIn });
  assert.deepEqual(keysOf(reverse).slice(9), keysOf(ix).slice(9), "base and quote user accounts keep their positions");
  assert.equal(reverse.data[13], 0, "base in");
  assert.equal(keysOf(reverse)[5].key, String(usdc.mint), "destination mint");
  // Quotes run after the latest trade slot (@32, @40) and the price account's update slot (u32 @16), which GoonFi charges more in.
  // Price account 4V7iPf… as read on 2026-10-17: bid, ask, update slot 458013610, scale 1e6, timestamp in ms.
  const price = Buffer.from("273d0f0000000000283d0f0000000000aabb4c1b40420f00ed898949a1010000", "hex");
  const read = (key: PublicKey) => key.equals(K("4V7iPfdqmjs2aAkS3BUDoRd9MuDBoTm5tETLC7fEDkjd")) ? price : undefined;
  data.writeBigUInt64LE(458_013_598n, 32); data.writeBigUInt64LE(458_013_550n, 40);
  assert.equal(pool.minQuoteSlot!(data, read), 458_013_611n);
  data.writeBigUInt64LE(458_013_620n, 40);
  assert.equal(pool.minQuoteSlot!(data, read), 458_013_621n);
  assert.equal(pool.minQuoteSlot!(data, () => undefined), 458_013_621n);
});

test("Deriverse instruments, books and vaults derive to the deployed accounts and match observed swaps", () => {
  assert.equal(String(deriverseInstrAccount(7, 3, 1)), "H7o7KCr5n3whqcPsPkeCPWrzivKrarqzidD3kt8fijvB");
  const data = Buffer.alloc(2344);
  VENUE_LAYOUTS.deriverse.discriminator.copy(data);
  data.writeUInt32LE(1, 8); data.writeUInt32LE(3, 12); data.writeUInt32LE(1, 16);
  put(data, 352, "5DMkwX1edkbAywTecSjP2nyPjzmi8dCKvWfQXybVLvCw"); put(data, 416, String(usdt.mint)); put(data, 448, String(usdc.mint));
  const pool = deriverseModel(K("H7o7KCr5n3whqcPsPkeCPWrzivKrarqzidD3kt8fijvB"), data);
  const common = (tree: string, orders: string, user: string, asset: string, crncy: string) => observed([`${user}:`,
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB:", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:", "UUUXQdoC85FmgUgDMfYj3UsBWMLSjU7gVj4QmYGdMRx:w",
    "EPUHjseXG3izk3MVUJ1PcyWT9xeBhtAnq8gUpq3j8Uni:w", "H7o7KCr5n3whqcPsPkeCPWrzivKrarqzidD3kt8fijvB:w", `${tree}:w`, `${orders}:w`,
    "H6mmDwnQHBq68tqEhBConeRBBDUobqHrQbDUcBVNTW9M:w", "5DMkwX1edkbAywTecSjP2nyPjzmi8dCKvWfQXybVLvCw:w", "FCgtbMQPyLVNnvd7mpxc3SCE8Ne8qEL9xkU2gwf7xceA:w",
    `${asset}:w`, `${crncy}:w`, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "11111111111111111111111111111111:",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL:"]);
  // Buying the asset (USDT) with the currency (USDC) fills asks.
  const buy = swapOf(pool, { owner: K("H87jnZMfN9utfyw5G26VCbsZcWgMAsVhPLiMQFmb3Wo9"), mintIn: usdc.mint, amount: 0x243fe6n, data,
    ataIn: K("6eAoU2RyenbU1zHQNzgcSmAiTTxNdsHBx5X6KQ1rfxys"), ataOut: K("8fWjd5GAJyZnyaN6UX4Nwy8Nt8AweELpyp5oKJGhpSM5") });
  assert.ok(buy.programId.equals(DERIVERSE));
  assert.deepEqual(keysOf(buy), common("AYhievvcVNZ8ATrSeVTUPeEX2JAz1RhAnBp5CUmxYjy4", "GUGu95pqRvUk1NeZTmadqrUa1RxFcmTMCCrqTXK6976V",
    "H87jnZMfN9utfyw5G26VCbsZcWgMAsVhPLiMQFmb3Wo9", "8fWjd5GAJyZnyaN6UX4Nwy8Nt8AweELpyp5oKJGhpSM5", "6eAoU2RyenbU1zHQNzgcSmAiTTxNdsHBx5X6KQ1rfxys"));
  assert.ok(buy.keys[0].isSigner);
  assert.equal(buy.data.toString("hex"), "1a010000010000000000000000000000e63f2400000000000000000000000000");
  const sell = swapOf(pool, { owner: K("pNuTauxtdqBHJTv6pGgMGdWuh29McRL1DXJYnwgkkms"), mintIn: usdt.mint, amount: 0x3bd139n, data,
    ataIn: K("GhFXQ2zMvS1swHXzbkBCDiHg23ZWg91NVE8NLLFHyGwK"), ataOut: K("2EmSdt6eCf2Kxpk7txNmhAeKSu2S84evAZ3xvu9h4ycs") });
  assert.deepEqual(keysOf(sell), common("42Xtwh4LeVX7yHtn77wXSXsDMKFqggcafgdHWKvsguxu", "6PMK33AGG7qCyMn2uMYdWBMqdESW3VNUQHbqUFUqSgwP",
    "pNuTauxtdqBHJTv6pGgMGdWuh29McRL1DXJYnwgkkms", "GhFXQ2zMvS1swHXzbkBCDiHg23ZWg91NVE8NLLFHyGwK", "2EmSdt6eCf2Kxpk7txNmhAeKSu2S84evAZ3xvu9h4ycs"));
  assert.equal(sell.data.toString("hex"), "1a00000001000000000000000000000039d13b00000000000000000000000000");
});

test("JupLend AMM liquidity-layer PDAs and swap layout match an observed swap", () => {
  const data = Buffer.alloc(329);
  VENUE_LAYOUTS.juplend.discriminator.copy(data);
  put(data, 11, String(usdc.mint)); put(data, 43, String(usdt.mint));
  const pool = juplendModel(K("FfEJYz4hLLFe4KyNyKYCjvfhMfbtpLH3CXpcPe8nhmfy"), data);
  const ix = swapOf(pool, { owner: K("FyzX9EYKVtjBVDdHbBT9kRhnCn3Bz6cL7aHxnpqMHrpX"), mintIn: usdc.mint, amount: 30_999_834n, data,
    ataIn: K("5szx5YihsupCBZGeu6hPxx8SDiMdxsWPdF9988GFE61P"), ataOut: K("D2xP2DnvettgjHk8y47PjpaNcT4vqP3FVCEuqCYPCB4j") });
  assert.ok(ix.programId.equals(JUPLEND_AMM));
  assert.deepEqual(keysOf(ix), observed(["FyzX9EYKVtjBVDdHbBT9kRhnCn3Bz6cL7aHxnpqMHrpX:sw", "FfEJYz4hLLFe4KyNyKYCjvfhMfbtpLH3CXpcPe8nhmfy:w",
    "5szx5YihsupCBZGeu6hPxx8SDiMdxsWPdF9988GFE61P:w", "D2xP2DnvettgjHk8y47PjpaNcT4vqP3FVCEuqCYPCB4j:w", "FyzX9EYKVtjBVDdHbBT9kRhnCn3Bz6cL7aHxnpqMHrpX:sw",
    "5szx5YihsupCBZGeu6hPxx8SDiMdxsWPdF9988GFE61P:w", "D2xP2DnvettgjHk8y47PjpaNcT4vqP3FVCEuqCYPCB4j:w", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB:", "94vK29npVbyRHXH63rRcTiSr26SFhrQTzbpNJuhQEDu:w", "Enao27EWUV2fv3rUqwknJ1eRaM5aAeN5ijeCrM9tayRX:w",
    "5pjzT5dFTsXcwixoab1QDLvZQvpYJxJeBphkyfHGn688:", "6sAbVeSvEfjQGRAGg9W4PAfhB5qNhYiGdx6Fh9uVEsEC:", "BmkUoKMFYBxNSzWXyUjyMJjMAaVz4d8ZnxwwmhDCUXFB:w",
    "4HTRHjdgy4VSVRcsumuzVFCgWywNhjGsD5oG3kqAt5vo:w", "6gVvQNMBecUAqSswntizqMHy6QEJnsUryWLa4Us1Lva5:w", "A6YLsnavkN39xSCEb8YEczPwKdJdkWd7Z5PD7r6a57hR:w",
    "4NB8MfVbDzZEEKAda5kxChxPSqJJ2vjTb7hRFZyJ5EWr:w", "37PKKRw8Jv6sXUWi16hjj4TsCmXGugZejsRpWmGzf2PZ:w", "7s1da8DduuBFqGra5bJBjpnvL5E9mGzCuMk1Qkh4or2Z:",
    "jupeiUmn818Jg1ekPURTpr4mFo29p46vygyykFJ3wZC:", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:",
    "jupnw4B6Eqs7ft6rxpzYLJZYSnrpRgPcr589n5Kv4oc:"]));
  assert.equal(ix.data.toString("hex"), "8dac0ad04509389a011a05d901000000000000000000000000");
});

test("Manifest swaps match an observed swap and pass global accounts only when they exist", () => {
  const data = Buffer.alloc(88_016);
  VENUE_LAYOUTS.manifest.discriminator.copy(data);
  put(data, 16, String(usdt.mint)); put(data, 48, String(usdc.mint));
  put(data, 80, "CNRQ2Q5YURFcQrATzYeKUWgKUoBDfqzkDrRWf21UXCVo"); put(data, 112, "FGQoLafigpyVb7mLa6pvsDDpDaEE3JetrzQoAggTo3n7");
  const pool = manifestModel(K("8sjV1AqBFvFuADBCQHhotaRq5DFFYSjjg1jMyVWMqXvZ"), data);
  const usdtGlobals = manifestGlobal(usdt.mint).map(String);
  assert.deepEqual(usdtGlobals, ["Gqr3Y3xGv3Rz948JnrKq4aH5Z8ZpKjPcysQDyYPJs7tZ", "9x85APtURYxxxosTmJhvZWBmPSoTMqhYHz4tBnJ7N4T8"]);
  const params = { owner: K("4JTxWfGjg49PJzXqPbcnD5R9ETCUTVb1Vj5WqSjeHDbo"), mintIn: usdc.mint, amount: 148_260_533n, data,
    ataIn: K("EFh3kWMTNEXbhkvxJcPxrK6UXeQB92QCnRCk7QEFxUou"), ataOut: K("3CorGnkT6XNsw6xwgeMoUtC6XyMftcVxoeLjp6vb2FKd") };
  const ix = swapOf(pool, { ...params, exists: (key: PublicKey) => usdtGlobals.includes(String(key)) });
  assert.ok(ix.programId.equals(MANIFEST));
  assert.deepEqual(keysOf(ix), observed(["4JTxWfGjg49PJzXqPbcnD5R9ETCUTVb1Vj5WqSjeHDbo:sw", "8sjV1AqBFvFuADBCQHhotaRq5DFFYSjjg1jMyVWMqXvZ:w",
    "11111111111111111111111111111111:", "3CorGnkT6XNsw6xwgeMoUtC6XyMftcVxoeLjp6vb2FKd:w", "EFh3kWMTNEXbhkvxJcPxrK6UXeQB92QCnRCk7QEFxUou:w",
    "CNRQ2Q5YURFcQrATzYeKUWgKUoBDfqzkDrRWf21UXCVo:w", "FGQoLafigpyVb7mLa6pvsDDpDaEE3JetrzQoAggTo3n7:w", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB:", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:",
    "Gqr3Y3xGv3Rz948JnrKq4aH5Z8ZpKjPcysQDyYPJs7tZ:w", "9x85APtURYxxxosTmJhvZWBmPSoTMqhYHz4tBnJ7N4T8:w"]));
  assert.equal(ix.data.toString("hex"), "04b546d6080000000000000000000000000001");
  assert.equal(swapOf(pool, { ...params, exists: () => false }).keys.length, 11);
  assert.equal(swapOf(pool, { ...params, mintIn: usdt.mint }).data[17], 1, "base input");
});

test("PancakeSwap reuses the Raydium CLMM model under its own program and PDAs", () => {
  const data = Buffer.alloc(1544);
  VENUE_LAYOUTS.pancakeswap.discriminator.copy(data);
  put(data, 9, "HZzqEWHEvSiqYt6PxxLs7zXETmqGuryqfmvggjAkqisp"); put(data, 73, String(usdc.mint)); put(data, 105, String(usdt.mint));
  put(data, 137, "G3V3RCKKLuK5k3DSwuA8hnakufyubAcd89rh2WNeXK8C"); put(data, 169, "C6boreystyantZEbCdUeXdQm21YdKpEffiH1yfs7hLbq");
  put(data, 201, "9rBc5NzPre1vKm2Mb4gPPzAcgACYpyWtvJdPaQ1s5Vrb");
  data.writeUInt16LE(10, 235); data.writeBigUInt64LE(1n, 237); data.writeInt32LE(12, 269);
  const pool = pancakeswapModel(K("4sC7TFsmHodm4sfFFBcKXKbL1K2V5pav52zAiokufnQy"), data, [-600, 0, 600]);
  assert.equal(pool.venue, "pancakeswap");
  const ix = swapOf(pool, { owner: K("9ZAPm4VSRNmKmEwVErbtX674nY3BhebJxRwwQKsJTxED"), mintIn: usdt.mint, amount: 158_685n, data,
    ataIn: K("qbkFUHcSaKk5DEmnzsaVVgQcwNooyXEAFpzYAKcCEbp"), ataOut: K("4oFkt8LoCBxrQ8ZoUehNf9h8pA9T64EXocGEEKJ1h7dd") });
  assert.ok(ix.programId.equals(PANCAKESWAP));
  // The observed swap passed the bitmap extension read-only; the shared model marks it writable.
  assert.deepEqual(ix.keys.map(k => k.pubkey.toBase58()), ["9ZAPm4VSRNmKmEwVErbtX674nY3BhebJxRwwQKsJTxED", "HZzqEWHEvSiqYt6PxxLs7zXETmqGuryqfmvggjAkqisp",
    "4sC7TFsmHodm4sfFFBcKXKbL1K2V5pav52zAiokufnQy", "qbkFUHcSaKk5DEmnzsaVVgQcwNooyXEAFpzYAKcCEbp", "4oFkt8LoCBxrQ8ZoUehNf9h8pA9T64EXocGEEKJ1h7dd",
    "C6boreystyantZEbCdUeXdQm21YdKpEffiH1yfs7hLbq", "G3V3RCKKLuK5k3DSwuA8hnakufyubAcd89rh2WNeXK8C", "9rBc5NzPre1vKm2Mb4gPPzAcgACYpyWtvJdPaQ1s5Vrb",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "9EYhY17VYVYNcU3TT6CKECqT9jvGS2yepxkVzuTn8ydo",
    "D3ZXeZp2gnui5fnupLTnnwWnygTsNJ4D9bwvVNramhay", "FQUHSbkqbRqsVAiehPmHkNxA1yJGUZAatryisxTFbxNX"]);
});

test("new venues are validated by owner, discriminator, size and pair", () => {
  const goon = Buffer.alloc(2048); VENUE_LAYOUTS.goonfi.discriminator.copy(goon);
  put(goon, 80, String(usdt.mint)); put(goon, 112, String(usdc.mint));
  const candidate = { venue: "goonfi" as const, address: K("EEUNhHsRoUVgJUFpkupmdF4v7uLUw1zhYLp7u9s8zFqG") };
  assert.equal(validatePoolAccount(candidate, account(goon, GOONFI), usdc, usdt), goon);
  assert.throws(() => validatePoolAccount(candidate, account(goon, MANIFEST), usdc, usdt), /Invalid goonfi/);
  assert.throws(() => validatePoolAccount(candidate, account(goon.subarray(0, 2047), GOONFI), usdc, usdt), /Invalid goonfi/);
  assert.throws(() => validatePoolAccount(candidate, account(Buffer.alloc(2048), GOONFI), usdc, usdt), /Invalid goonfi/);
  const other = Buffer.from(goon); K("So11111111111111111111111111111111111111112").toBuffer().copy(other, 80);
  assert.throws(() => validatePoolAccount(candidate, account(other, GOONFI), usdc, usdt), /requested pair/);
  // Manifest markets have no fixed size, only a header.
  const market = Buffer.alloc(3000); VENUE_LAYOUTS.manifest.discriminator.copy(market);
  put(market, 16, String(usdc.mint)); put(market, 48, String(usdt.mint));
  const manifest = { venue: "manifest" as const, address: K("8sjV1AqBFvFuADBCQHhotaRq5DFFYSjjg1jMyVWMqXvZ") };
  assert.equal(validatePoolAccount(manifest, account(market, MANIFEST), usdc, usdt), market);
  assert.throws(() => validatePoolAccount(manifest, account(market.subarray(0, 60), MANIFEST), usdc, usdt), /Invalid manifest/);
});

test("chain discovery scans by layout, derives Deriverse instruments and reports failed sources", async () => {
  const scans: { program: string; filters: unknown[] }[] = [];
  const found: Record<string, string> = { [String(GOONFI)]: "EEUNhHsRoUVgJUFpkupmdF4v7uLUw1zhYLp7u9s8zFqG",
    [String(JUPLEND_AMM)]: "FfEJYz4hLLFe4KyNyKYCjvfhMfbtpLH3CXpcPe8nhmfy", [String(MANIFEST)]: "8sjV1AqBFvFuADBCQHhotaRq5DFFYSjjg1jMyVWMqXvZ" };
  const scan = { getProgramAccounts: async (program: PublicKey, config: { filters: { memcmp?: { offset: number; bytes: string } }[] }) => {
    scans.push({ program: String(program), filters: config.filters });
    if (program.equals(PANCAKESWAP)) throw new Error("403 Forbidden: Method requires plan upgrade");
    const mintA = config.filters.find(f => f.memcmp?.offset === VENUE_LAYOUTS.goonfi.mintA || f.memcmp?.offset === VENUE_LAYOUTS.manifest.mintA
      || f.memcmp?.offset === VENUE_LAYOUTS.juplend.mintA)?.memcmp?.bytes;
    // Each pool exists in one mint order only.
    const order = program.equals(JUPLEND_AMM) ? String(usdc.mint) : String(usdt.mint);
    return found[String(program)] && mintA === order ? [{ pubkey: K(found[String(program)]), account: account(Buffer.alloc(0), program) }] : [];
  } } as unknown as Connection;
  const state = (mint: PublicKey, id: number) => { const d = Buffer.alloc(88); d.writeUInt32LE(4); d.writeUInt32LE(1, 4); mint.toBuffer().copy(d, 8); d.writeUInt32LE(id, 72); return account(d, DERIVERSE); };
  const chain = { getMultipleAccountsInfo: async (keys: PublicKey[]) => keys.map(key => {
    if (key.equals(deriverseTokenState(usdc.mint))) return state(usdc.mint, 1);
    if (key.equals(deriverseTokenState(usdt.mint))) return state(usdt.mint, 3);
    return key.equals(deriverseInstrAccount(7, 3, 1)) ? account(Buffer.alloc(8), DERIVERSE) : null;
  }) } as unknown as Connection;
  const request = (async (url: URL) => Response.json(String(url).includes("orca.so") ? { data: [] } : { success: true, data: { count: 0, data: [] } })) as typeof fetch;
  const result = await discoverQuotePools(usdc, usdt, request, { connection: chain, scan });
  assert.deepEqual(result.pools.map(p => `${p.venue}:${p.address}`).sort(), ["deriverse:H7o7KCr5n3whqcPsPkeCPWrzivKrarqzidD3kt8fijvB",
    "goonfi:EEUNhHsRoUVgJUFpkupmdF4v7uLUw1zhYLp7u9s8zFqG", "juplend:FfEJYz4hLLFe4KyNyKYCjvfhMfbtpLH3CXpcPe8nhmfy",
    "manifest:8sjV1AqBFvFuADBCQHhotaRq5DFFYSjjg1jMyVWMqXvZ"]);
  assert.deepEqual(result.warnings, ["PancakeSwap discovery failed: 403 Forbidden: Method requires plan upgrade"]);
  const goonScan = scans.find(s => s.program === String(GOONFI))!;
  assert.deepEqual(goonScan.filters[0], { dataSize: 2048 });
  assert.deepEqual((goonScan.filters as { memcmp?: { offset: number } }[]).slice(1).map(f => f.memcmp?.offset), [0, 80, 112], "discriminator and both mints");
  assert.ok(scans.filter(s => s.program === String(MANIFEST)).every(s => !s.filters.some(f => "dataSize" in (f as object))), "Manifest markets vary in size");
  assert.equal(createHash("sha256").update("account:PoolState").digest().subarray(0, 8).toString("hex"), VENUE_LAYOUTS.pancakeswap.discriminator.toString("hex"));
});

test("BisonFi swaps match a Jupiter-routed SOL/USDC swap and expose the oracle's valid-from slot", () => {
  const data = Buffer.alloc(2048);
  VENUE_LAYOUTS.bisonfi.discriminator.copy(data);
  data.writeBigUInt64LE(457_983_174n, 72);
  put(data, 120, "ATRsNGv2nDw7hSMfkUTBoVUDsFDwN7po7KbecyiGWNB4"); put(data, 152, "2Y7HATmn9aJBcxCskE5V2U2epmjvkZmB51zTJBbhj4cU");
  put(data, 184, String(sol.mint)); put(data, 216, String(usdc.mint));
  const pool = bisonfiModel(K("8FnX3xo2yYw3EUE6w3nQA4GfXGS9wpK6oj3veJpbFzLo"), data);
  assert.equal(pool.validFromSlot!(data), 457_983_174n);
  assert.deepEqual(VENUE_PROGRAMS.bisonfi.map(String), [String(BISONFI)]);
  // Inner instruction of Jupiter tx Acj3EiEW…: 15.83045 USDC (token B) in.
  const owner = K("6ekiTTMxdmxyf4hJjPxwuAPAL9WkSf6FepU6UBWP9W6A");
  const ix = swapOf(pool, { owner, mintIn: usdc.mint, amount: 15_830_450n, data,
    ataIn: K("ABX7YgfQ37tpUwLsnuZB1XeQKRqnCTPzcTteeQpaWZsG"), ataOut: K("B1e5FHFQ3KEQ9SSUVR1WNuczu2srzka91Emn8xpFwpmE") });
  assert.ok(ix.programId.equals(BISONFI));
  assert.deepEqual(keysOf(ix), observed(["6ekiTTMxdmxyf4hJjPxwuAPAL9WkSf6FepU6UBWP9W6A:sw", "8FnX3xo2yYw3EUE6w3nQA4GfXGS9wpK6oj3veJpbFzLo:w",
    "ATRsNGv2nDw7hSMfkUTBoVUDsFDwN7po7KbecyiGWNB4:w", "2Y7HATmn9aJBcxCskE5V2U2epmjvkZmB51zTJBbhj4cU:w", "B1e5FHFQ3KEQ9SSUVR1WNuczu2srzka91Emn8xpFwpmE:w",
    "ABX7YgfQ37tpUwLsnuZB1XeQKRqnCTPzcTteeQpaWZsG:w", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:",
    "So11111111111111111111111111111111111111112:", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:", "Sysvar1nstructions1111111111111111111111111:",
    "J1to1yufRnoWn81KYg1XkTWzmKjnYSnmE2VY8DGUJ9Qv:", "4cG31VNF9TzFinNc7BmnjhFvGjxkY3sCETVMtMgbrhPs:"]));
  assert.ok(ix.keys[0].isSigner);
  assert.equal(ix.data.toString("hex"), "02b28df10000000000000000000000000001");
  const reverse = swapOf(pool, { owner, mintIn: sol.mint, amount: 1n, data,
    ataIn: K("B1e5FHFQ3KEQ9SSUVR1WNuczu2srzka91Emn8xpFwpmE"), ataOut: K("ABX7YgfQ37tpUwLsnuZB1XeQKRqnCTPzcTteeQpaWZsG") });
  assert.deepEqual(keysOf(reverse), keysOf(ix), "token A and B user accounts keep their positions in both directions");
  assert.equal(reverse.data[17], 0, "token A in");
});

test("Kipseli legs are one-step Jupiter routes whose inner accounts match an observed Jupiter route", () => {
  const data = Buffer.alloc(864);
  VENUE_LAYOUTS.kipseli.discriminator.copy(data);
  put(data, 72, String(sol.mint)); put(data, 104, String(usdc.mint));
  data.writeBigUInt64LE(457_986_629n, 432);
  const pool = kipseliModel(K("7yD7gfXv49SFxhx9uTc11dEd7Nj98rGWeYMdZJgheZbG"), data);
  assert.equal(pool.validFromSlot!(data), 457_986_629n);
  assert.deepEqual(VENUE_PROGRAMS.kipseli.map(String), [String(KIPSELI), String(JUPITER)]);
  // Ban-list entries ["ban", user] seen in Jupiter txs 4jaF9p3Z… and 4Ku5yJmE…
  assert.deepEqual([kipseliBanEntry(K("4xDsmeTWPNjgSVSS1VTfzFq3iHZhp77ffPkAmkZkdu71")), kipseliBanEntry(K("BoyLcEmBmAb82tnrGsbkFPsyrh51CtgV1tPZ9zCt1Z5U")),
    kipseliBanEntry(K("D4mG4SUy5zU8qCEQa6fL7aez9px5Qsbpaw5VotGR5Za4"))].map(String),
    ["YVhaTb8VZZWHYiPXurkPbwaN7DZ4CWJ2UZcPBgyWLpm", "HXaMm6GL7VZuSom77PAoNY9P2CPb9mR3AuqYsQgAycZV", "5qnohXr39sN1sWYY54c6ei4pL1d6jy9XcfV6EvQwmvm5"]);
  // Jupiter `route` tx 4Ku5yJmE…: step 1 swaps 9 USDC (quote) for SOL on this pool.
  const owner = K("D4mG4SUy5zU8qCEQa6fL7aez9px5Qsbpaw5VotGR5Za4");
  const params = { owner, mintIn: usdc.mint, amount: 9_000_000n, data,
    ataIn: K("Bf2bcq6MC2wDtc2QrCNWzDt9YEc774RPCgKPyw4M6BW4"), ataOut: K("F7ZE3N1o4a1xyfDukdQ5r1B8HoPseaembJVW8f8eVUhP") };
  const ix = swapOf(pool, params);
  assert.ok(ix.programId.equals(JUPITER));
  // Route accounts in the observed order; that route's final output was another token, so its destination account, mint
  // and token program differ (here the user's SOL account, wrapped SOL and SPL Token).
  assert.deepEqual(keysOf(ix).slice(0, 9), observed(["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "D4mG4SUy5zU8qCEQa6fL7aez9px5Qsbpaw5VotGR5Za4:s",
    "Bf2bcq6MC2wDtc2QrCNWzDt9YEc774RPCgKPyw4M6BW4:w", "F7ZE3N1o4a1xyfDukdQ5r1B8HoPseaembJVW8f8eVUhP:w", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4:",
    "So11111111111111111111111111111111111111112:", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4:", "D8cy77BBepLMngZx6ZukaTff5hCt1HrWyKk3Hnd9oitf:",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4:"]));
  assert.deepEqual(keysOf(ix).slice(9), observed(["3TK9D8aoBFYjYZtKCjciPrVrRStsnvo7KmpcJqDavpaU:", "D4mG4SUy5zU8qCEQa6fL7aez9px5Qsbpaw5VotGR5Za4:s",
    "7yD7gfXv49SFxhx9uTc11dEd7Nj98rGWeYMdZJgheZbG:w", "HgFLBWQugxt6qbPaWiwXjrqauXdQmcpMV6H6t8ttD8s1:w", "FzvRbXDdrsYVMa9KohXyQytWNAZAo4PsF8yujDH7KCfG:w",
    "So11111111111111111111111111111111111111112:", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:", "F7ZE3N1o4a1xyfDukdQ5r1B8HoPseaembJVW8f8eVUhP:w",
    "Bf2bcq6MC2wDtc2QrCNWzDt9YEc774RPCgKPyw4M6BW4:w", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA:", "11111111111111111111111111111111:",
    "D4mG4SUy5zU8qCEQa6fL7aez9px5Qsbpaw5VotGR5Za4:s", "5qnohXr39sN1sWYY54c6ei4pL1d6jy9XcfV6EvQwmvm5:", "5qnohXr39sN1sWYY54c6ei4pL1d6jy9XcfV6EvQwmvm5:",
    "J1to1yufRnoWn81KYg1XkTWzmKjnYSnmE2VY8DGUJ9Qv:", "Sysvar1nstructions1111111111111111111111111:"]), "Kipseli program, then its swap accounts as Jupiter passed them");
  assert.deepEqual(ix.keys.flatMap((k, i) => k.isSigner ? [i] : []), [1, 10, 20]);
  // route discriminator, one step (observed bytes b5 00 64 00 01), in_amount, quoted_out 0 (minOut 0), slippage 50 bps, no fee
  assert.equal(ix.data.toString("hex"), "e517cb977ae3ad2a" + "01000000" + "b500640001" + "4054890000000000" + "0000000000000000" + "3200" + "00");
  const reverse = swapOf(pool, { ...params, mintIn: sol.mint, ataIn: params.ataOut, ataOut: params.ataIn });
  assert.deepEqual(keysOf(reverse).slice(9), keysOf(ix).slice(9), "base and quote user accounts keep their positions");
  assert.equal(reverse.data[13], 1, "base in");
  assert.equal(keysOf(reverse)[5].key, String(usdc.mint), "destination mint");
  // Jupiter requires quoted_out - floor(quoted_out * 50 / 10000) out: the smallest quoted_out whose bound is exactly minOut.
  const bound = (quoted: bigint) => quoted - quoted * 50n / 10_000n;
  for (const minOut of [1n, 199n, 1_000n, 9_538_384_882n, 10_011_513_240n]) {
    const quoted = swapOf(pool, { ...params, minOut }).data.readBigUInt64LE(25);
    assert.equal(bound(quoted), minOut);
    assert.ok(bound(quoted - 1n) < minOut);
  }
});

test("BisonFi and Kipseli pools are validated by owner, discriminator, size and pair", () => {
  const bison = Buffer.alloc(2048); VENUE_LAYOUTS.bisonfi.discriminator.copy(bison);
  put(bison, 184, String(sol.mint)); put(bison, 216, String(usdt.mint));
  const candidate = { venue: "bisonfi" as const, address: K("FJnaiidSLXFweWkgbinxEHRykVHsnkzDcYbNDR3RF5LN") };
  assert.equal(validatePoolAccount(candidate, account(bison, BISONFI), usdt, sol), bison);
  assert.throws(() => validatePoolAccount(candidate, account(bison, KIPSELI), usdt, sol), /Invalid bisonfi/);
  assert.throws(() => validatePoolAccount(candidate, account(bison, BISONFI), usdc, sol), /requested pair/);
  const kip = Buffer.alloc(864); VENUE_LAYOUTS.kipseli.discriminator.copy(kip);
  put(kip, 72, String(sol.mint)); put(kip, 104, String(usdc.mint));
  const kipseli = { venue: "kipseli" as const, address: K("7yD7gfXv49SFxhx9uTc11dEd7Nj98rGWeYMdZJgheZbG") };
  assert.equal(validatePoolAccount(kipseli, account(kip, KIPSELI), usdc, sol), kip);
  assert.throws(() => validatePoolAccount(kipseli, account(kip.subarray(0, 863), KIPSELI), usdc, sol), /Invalid kipseli/);
  assert.equal(VENUE_LAYOUTS.kipseli.discriminator.toString("hex"), "f19a6d0411b16dbc", "Anchor account:Pool");
});
