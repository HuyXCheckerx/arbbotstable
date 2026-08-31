import "dotenv/config";

import { createHash, randomUUID } from "node:crypto";
import {
  AddressLookupTableAccount,
  ComputeBudgetProgram,
  Connection,
  Keypair,
  PublicKey,
  SystemProgram,
  TransactionInstruction,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import bs58 from "bs58";
import fetch from "node-fetch";

export const USDC_MINT = new PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");
export const PYUSD_MINT = new PublicKey("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo");
export const USDG_MINT = new PublicKey("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH");
export const STABLE_PROGRAM_ID = new PublicKey("2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG");

const MAX_WIRE_TRANSACTION_BYTES = 1232;
const JUPITER_API_BASE = "https://api.jup.ag/swap/v1";
const JUPITER_LITE_API_BASE = "https://lite-api.jup.ag/swap/v1";
const SINGLE_CHAIN_SWAP_DISCRIMINATOR = createHash("sha256")
  .update("global:single_chain_swap")
  .digest()
  .subarray(0, 8);

function loadKeypair(secret: string): Keypair {
  secret = secret.trim();
  if (secret.startsWith("[")) {
    return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(secret)));
  }
  const decoded = bs58.decode(secret);
  return decoded.length === 64 ? Keypair.fromSecretKey(decoded) : Keypair.fromSeed(decoded);
}

function formatRaw(value: bigint, decimals = 6): string {
  const negative = value < 0n;
  const absolute = negative ? -value : value;
  const scale = 10n ** BigInt(decimals);
  const whole = absolute / scale;
  const fraction = (absolute % scale).toString().padStart(decimals, "0");
  const trimmed = fraction.replace(/0+$/, "");
  return `${negative ? "-" : ""}${whole}${trimmed ? `.${trimmed}` : ""}`;
}

function tokenProgramForMint(mint: PublicKey): PublicKey {
  return mint.equals(PYUSD_MINT) || mint.equals(USDG_MINT)
    ? TOKEN_2022_PROGRAM_ID
    : TOKEN_PROGRAM_ID;
}

function unsignedLe(value: bigint): Buffer {
  const buffer = Buffer.alloc(8);
  buffer.writeBigUInt64LE(value);
  return buffer;
}

function signedLe(value: bigint): Buffer {
  const buffer = Buffer.alloc(8);
  buffer.writeBigInt64LE(value);
  return buffer;
}

function integerField(value: number | string | undefined, name: string): bigint {
  if (value === undefined || !/^\d+$/.test(String(value))) {
    throw new Error(`Stable.com order has invalid ${name}`);
  }
  return BigInt(value);
}

function buildStableSwapInstruction(
  wallet: PublicKey,
  inputRaw: bigint,
  expectedOutputRaw: bigint,
  order: any,
  inputMint: PublicKey,
  outputMint: PublicKey,
): TransactionInstruction {
  const signatureHex = order.maintainerSignature?.replace(/^0x/, "");
  const signatureBytes = Buffer.from(signatureHex, "hex");
  const maintainerSignature = signatureBytes.subarray(0, 64);
  const recoveryId = signatureBytes.length === 65 ? signatureBytes[64] : Number(order.recoveryId ?? 0);

  const nonce = integerField(order.nonce, "nonce");
  const deadline = integerField(order.deadline, "deadline");
  const executionFeeLamports = integerField(order.executionFeeNative ?? order.nativeFee ?? 0, "executionFeeNative");

  const [mainState] = PublicKey.findProgramAddressSync([Buffer.from("main_state")], STABLE_PROGRAM_ID);
  const [nonceAccount] = PublicKey.findProgramAddressSync([Buffer.from("nonce"), wallet.toBuffer()], STABLE_PROGRAM_ID);
  const [nativeFeeAccount] = PublicKey.findProgramAddressSync([Buffer.from("native_fee")], STABLE_PROGRAM_ID);
  const [inputPool] = PublicKey.findProgramAddressSync([Buffer.from("pool"), inputMint.toBuffer()], STABLE_PROGRAM_ID);
  const [outputPool] = PublicKey.findProgramAddressSync([Buffer.from("pool"), outputMint.toBuffer()], STABLE_PROGRAM_ID);

  const inputTokenProgram = tokenProgramForMint(inputMint);
  const outputTokenProgram = tokenProgramForMint(outputMint);

  const userInputAta = getAssociatedTokenAddressSync(inputMint, wallet, false, inputTokenProgram);
  const userOutputAta = getAssociatedTokenAddressSync(outputMint, wallet, false, outputTokenProgram);
  const poolInputAta = getAssociatedTokenAddressSync(inputMint, inputPool, true, inputTokenProgram);
  const poolOutputAta = getAssociatedTokenAddressSync(outputMint, outputPool, true, outputTokenProgram);

  const data = Buffer.concat([
    SINGLE_CHAIN_SWAP_DISCRIMINATOR,
    unsignedLe(inputRaw),
    unsignedLe(executionFeeLamports),
    maintainerSignature,
    unsignedLe(nonce),
    signedLe(deadline),
    Buffer.from([recoveryId]),
  ]);

  return new TransactionInstruction({
    programId: STABLE_PROGRAM_ID,
    keys: [
      { pubkey: wallet, isSigner: true, isWritable: true },
      { pubkey: wallet, isSigner: true, isWritable: true },
      { pubkey: nonceAccount, isSigner: false, isWritable: true },
      { pubkey: mainState, isSigner: false, isWritable: false },
      { pubkey: nativeFeeAccount, isSigner: false, isWritable: true },
      { pubkey: inputPool, isSigner: false, isWritable: true },
      { pubkey: inputMint, isSigner: false, isWritable: false },
      { pubkey: poolInputAta, isSigner: false, isWritable: true },
      { pubkey: userInputAta, isSigner: false, isWritable: true },
      { pubkey: outputPool, isSigner: false, isWritable: true },
      { pubkey: outputMint, isSigner: false, isWritable: false },
      { pubkey: poolOutputAta, isSigner: false, isWritable: true },
      { pubkey: wallet, isSigner: false, isWritable: false },
      { pubkey: userOutputAta, isSigner: false, isWritable: true },
      { pubkey: inputTokenProgram, isSigner: false, isWritable: false },
      { pubkey: outputTokenProgram, isSigner: false, isWritable: false },
      { pubkey: ASSOCIATED_TOKEN_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data,
  });
}

function decodeJupiterInstruction(instruction: any): TransactionInstruction {
  return new TransactionInstruction({
    programId: new PublicKey(instruction.programId),
    keys: instruction.accounts.map((account: any) => ({
      pubkey: new PublicKey(account.pubkey),
      isSigner: Boolean(account.isSigner),
      isWritable: Boolean(account.isWritable),
    })),
    data: Buffer.from(instruction.data, "base64"),
  });
}

function buildSwapLeg(response: any) {
  const instructions: TransactionInstruction[] = [];
  for (const group of [
    response.setupInstructions,
    [response.swapInstruction],
    response.cleanupInstruction ? [response.cleanupInstruction] : undefined,
    response.otherInstructions,
  ]) {
    if (!group) continue;
    for (const item of group) {
      if (item.programId === ComputeBudgetProgram.programId.toBase58()) continue;
      instructions.push(decodeJupiterInstruction(item));
    }
  }
  return {
    instructions,
    lookupTableAddresses: (response.addressLookupTableAddresses ?? []).map((addr: string) => new PublicKey(addr)),
  };
}

async function fetchLookupTables(connection: Connection, addresses: PublicKey[]): Promise<AddressLookupTableAccount[]> {
  const unique = [...new Map(addresses.map((addr) => [addr.toBase58(), addr])).values()];
  if (!unique.length) return [];
  const responses = await Promise.all(
    unique.map(async (address) => ({
      address,
      result: await connection.getAddressLookupTable(address),
    })),
  );
  return responses.filter((entry) => entry.result.value !== null).map((entry) => entry.result.value as AddressLookupTableAccount);
}

async function main() {
  const args = process.argv.slice(2);
  const send = args.includes("--send");
  const confirmArg = args[args.indexOf("--confirm-mainnet") + 1];

  const rpcUrl = process.env.SOLANA_RPC_URL || "https://solana-rpc.publicnode.com";
  const connection = new Connection(rpcUrl, "confirmed");

  const secret = process.env.SOL_FLASH_ARB_PRIVATE_KEY || process.env.SOLANA_PRIVATE_KEY!;
  const keypair = loadKeypair(secret);
  const wallet = keypair.publicKey;

  const tradeSizeArg = args.find((a) => a.startsWith("--size="))?.split("=")[1] || "10000";
  const tradeSize = parseFloat(tradeSizeArg);
  const loanAmountRaw = BigInt(Math.floor(tradeSize * 1e6));

  console.log(`=== ATOMIC 2-HOP ARBITRAGE (Wallet-Funded Single Transaction) ===`);
  console.log(`Wallet: ${wallet.toBase58()}`);
  console.log(`Trade Size: ${tradeSize.toLocaleString()} PYUSD`);

  const jupApiKey = process.env.SOL_FLASH_ARB_JUPITER_API_KEY;
  const jupBase = jupApiKey ? JUPITER_API_BASE : JUPITER_LITE_API_BASE;
  const headers: Record<string, string> = { accept: "application/json" };
  if (jupApiKey) headers["x-api-key"] = jupApiKey;

  // 1. Stable.com Leg (PYUSD -> USDG)
  console.log(`[1/3] Quoting Stable.com...`);
  const stableStatusRes = await fetch("https://api-defi.stable.com/swap/status", {
    method: "POST",
    headers: { "accept": "application/json", "content-type": "application/json", origin: "https://stable.com", referer: "https://stable.com/" },
    body: JSON.stringify({ chainFrom: "102", assetFrom: "PYUSD", chainTo: "102", assetTo: "USDG", gasLess: false, amountFrom: formatRaw(loanAmountRaw), addressFrom: wallet.toBase58(), addressTo: wallet.toBase58() }),
  });
  const stableStatus = (await stableStatusRes.json()) as any;
  const stableOutRaw = BigInt(Math.floor(parseFloat(stableStatus?.asset?.amountTo || formatRaw(loanAmountRaw)) * 1e6));
  console.log(`Leg 1 (Stable.com): ${formatRaw(loanAmountRaw)} PYUSD -> ${formatRaw(stableOutRaw)} USDG`);

  // 2. Jupiter Leg 1 (USDG -> USDC)
  console.log(`[2/3] Quoting Jupiter Leg 1 (USDG -> USDC)...`);
  const jup1QuoteRes = await fetch(`${jupBase}/quote?inputMint=${USDG_MINT.toBase58()}&outputMint=${USDC_MINT.toBase58()}&amount=${stableOutRaw}&slippageBps=2`, { headers });
  const jup1Quote = (await jup1QuoteRes.json()) as any;
  const usdcExpectedRaw = BigInt(jup1Quote.outAmount);
  console.log(`Leg 2 (Jupiter): ${formatRaw(stableOutRaw)} USDG -> ${formatRaw(usdcExpectedRaw)} USDC [${jup1Quote.routePlan[0]?.swapInfo?.label}]`);

  // 3. Jupiter Leg 2 (USDC -> PYUSD)
  console.log(`[3/3] Quoting Jupiter Leg 2 (USDC -> PYUSD)...`);
  const jup2QuoteRes = await fetch(`${jupBase}/quote?inputMint=${USDC_MINT.toBase58()}&outputMint=${PYUSD_MINT.toBase58()}&amount=${usdcExpectedRaw}&slippageBps=2`, { headers });
  const jup2Quote = (await jup2QuoteRes.json()) as any;
  const finalPyusdExpectedRaw = BigInt(jup2Quote.outAmount);
  console.log(`Leg 3 (Jupiter): ${formatRaw(usdcExpectedRaw)} USDC -> ${formatRaw(finalPyusdExpectedRaw)} PYUSD [${jup2Quote.routePlan[0]?.swapInfo?.label}]`);

  const grossProfitRaw = finalPyusdExpectedRaw - loanAmountRaw;
  console.log(`--------------------------------------------------`);
  console.log(`Expected Net Profit: ${formatRaw(grossProfitRaw)} PYUSD (+${(Number(grossProfitRaw)/1e6).toFixed(6)})`);
  console.log(`--------------------------------------------------`);

  if (grossProfitRaw <= 0n) {
    throw new Error(`Gross profit is non-positive: ${formatRaw(grossProfitRaw)} PYUSD`);
  }

  // Create Stable.com signed order
  const orderRes = await fetch("https://api-defi.stable.com/swap/create/singleChain", {
    method: "POST",
    headers: { "accept": "application/json", "content-type": "application/json", origin: "https://stable.com", referer: "https://stable.com/" },
    body: JSON.stringify({ chainFrom: "102", assetFrom: "PYUSD", chainTo: "102", assetTo: "USDG", gasLess: false, amountFrom: formatRaw(loanAmountRaw), amountTo: formatRaw(stableOutRaw), addressFrom: wallet.toBase58(), addressTo: wallet.toBase58(), device: randomUUID() }),
  });
  const stableOrderData = (await orderRes.json()) as any;
  const stableOrder = stableOrderData.maintainerSignature ? stableOrderData : stableOrderData.data;
  if (!stableOrder?.maintainerSignature) {
    throw new Error(`Stable.com order failed: ${JSON.stringify(stableOrderData)}`);
  }

  const stableIx = buildStableSwapInstruction(wallet, loanAmountRaw, stableOutRaw, stableOrder, PYUSD_MINT, USDG_MINT);

  // Fetch Jupiter swap instructions
  const [jup1SwapRes, jup2SwapRes] = await Promise.all([
    fetch(`${jupBase}/swap-instructions`, { method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify({ userPublicKey: wallet.toBase58(), payer: wallet.toBase58(), quoteResponse: jup1Quote, wrapAndUnwrapSol: false, useSharedAccounts: false, dynamicComputeUnitLimit: false, skipUserAccountsRpcCalls: false }) }),
    fetch(`${jupBase}/swap-instructions`, { method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify({ userPublicKey: wallet.toBase58(), payer: wallet.toBase58(), quoteResponse: jup2Quote, wrapAndUnwrapSol: false, useSharedAccounts: false, dynamicComputeUnitLimit: false, skipUserAccountsRpcCalls: false }) }),
  ]);

  const jup1Swap = (await jup1SwapRes.json()) as any;
  const jup2Swap = (await jup2SwapRes.json()) as any;

  const leg1Swap = buildSwapLeg(jup1Swap);
  const leg2Swap = buildSwapLeg(jup2Swap);

  const [alts1, alts2] = await Promise.all([
    fetchLookupTables(connection, leg1Swap.lookupTableAddresses),
    fetchLookupTables(connection, leg2Swap.lookupTableAddresses),
  ]);

  const mergedAlts: AddressLookupTableAccount[] = [];
  const seenAlts = new Set<string>();
  for (const alt of [...alts1, ...alts2]) {
    const key = alt.key.toBase58();
    if (!seenAlts.has(key)) {
      seenAlts.add(key);
      mergedAlts.push(alt);
    }
  }

  const latestBlockhash = await connection.getLatestBlockhash("confirmed");
  const messageV0 = new TransactionMessage({
    payerKey: wallet,
    recentBlockhash: latestBlockhash.blockhash,
    instructions: [
      ComputeBudgetProgram.setComputeUnitLimit({ units: 600_000 }),
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 25_000 }),
      stableIx,
      ...leg1Swap.instructions,
      ...leg2Swap.instructions,
    ],
  }).compileToV0Message(mergedAlts);

  const tx = new VersionedTransaction(messageV0);
  tx.sign([keypair]);

  const wireSize = tx.serialize().length;
  console.log(`Packed Wire Transaction Size: ${wireSize}/${MAX_WIRE_TRANSACTION_BYTES} bytes`);
  if (wireSize > MAX_WIRE_TRANSACTION_BYTES) {
    throw new Error(`Transaction is ${wireSize} bytes, exceeding Solana limit of ${MAX_WIRE_TRANSACTION_BYTES}!`);
  }

  console.log(`Simulating atomic transaction on RPC...`);
  const sim = await connection.simulateTransaction(tx, { commitment: "confirmed", sigVerify: true });
  if (sim.value.err) {
    console.error("Simulation logs:", sim.value.logs);
    throw new Error(`Simulation failed: ${JSON.stringify(sim.value.err)}`);
  }
  console.log(`Simulation PASSED: Consumed ${sim.value.unitsConsumed} compute units!`);

  if (!send) {
    console.log(`\n[DRY RUN COMPLETE] To broadcast onchain, pass: --send --confirm-mainnet EXECUTE_2HOP_ARB`);
    return;
  }

  if (confirmArg !== "EXECUTE_2HOP_ARB") {
    throw new Error("Missing --confirm-mainnet EXECUTE_2HOP_ARB confirmation flag");
  }

  console.log(`Broadcasting transaction on mainnet...`);
  const sig = await connection.sendRawTransaction(tx.serialize(), { skipPreflight: true, preflightCommitment: "confirmed" });
  console.log(`Submitted: https://solscan.io/tx/${sig}`);

  const confirmation = await connection.confirmTransaction(
    { signature: sig, blockhash: latestBlockhash.blockhash, lastValidBlockHeight: latestBlockhash.lastValidBlockHeight },
    "confirmed"
  );

  if (confirmation.value.err) {
    throw new Error(`Transaction reverted: ${JSON.stringify(confirmation.value.err)}`);
  }
  console.log(`[+] Confirmed 2-Hop Arbitrage onchain: ${sig}`);
}

main().catch((err) => {
  console.error(`ERROR: ${err.message || err}`);
  process.exit(1);
});
