import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import BN from "bn.js";
import { getFlashloanIx } from "@jup-ag/lend/flashloan";
import {
  AddressLookupTableAccount,
  ComputeBudgetProgram,
  Connection,
  Keypair,
  PublicKey,
  TransactionMessage,
  VersionedTransaction,
  type TransactionInstruction,
} from "@solana/web3.js";
import { config } from "./config.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  MAX_COMPUTE_UNITS,
  SOLANA_PACKET_DATA_SIZE,
  USDC_MINT,
} from "./constants.js";
import { toTransactionInstruction } from "./jupiter.js";
import { buildStableSwapInstruction } from "./stable.js";
import type { ApiInstruction, RouteQuote } from "./types.js";

export type PreparedAtomicTransaction = {
  transaction: VersionedTransaction;
  blockhash: string;
  lastValidBlockHeight: number;
  unitsConsumed: number;
  computeUnitLimit: number;
  serializedLength: number;
  logs: string[];
};

async function filterSetupInstructions(
  connection: Connection,
  instructions: ApiInstruction[],
): Promise<TransactionInstruction[]> {
  const result: TransactionInstruction[] = [];
  for (const apiInstruction of instructions) {
    if (apiInstruction.programId !== ASSOCIATED_TOKEN_PROGRAM_ID.toBase58()) {
      result.push(toTransactionInstruction(apiInstruction));
      continue;
    }
    const ata = apiInstruction.accounts.find(
      (account) => account.isWritable && !account.isSigner,
    );
    if (!ata) {
      result.push(toTransactionInstruction(apiInstruction));
      continue;
    }
    const accountInfo = await connection.getAccountInfo(new PublicKey(ata.pubkey), "confirmed");
    if (!accountInfo) result.push(toTransactionInstruction(apiInstruction));
  }
  return result;
}

async function readCustomAltAddresses(): Promise<string[]> {
  const addresses = new Set(config.customAltAddresses);
  if (existsSync(config.customAltFile)) {
    const parsed = JSON.parse(await readFile(config.customAltFile, "utf8")) as {
      address?: string;
      addresses?: string[];
    };
    if (parsed.address) addresses.add(parsed.address);
    for (const address of parsed.addresses ?? []) addresses.add(address);
  }
  return [...addresses];
}

async function loadLookupTables(
  connection: Connection,
  quote: RouteQuote,
): Promise<AddressLookupTableAccount[]> {
  const addresses = new Set([
    ...Object.keys(quote.build.addressesByLookupTableAddress ?? {}),
    ...(await readCustomAltAddresses()),
  ]);
  const tables: AddressLookupTableAccount[] = [];
  for (const address of addresses) {
    const result = await connection.getAddressLookupTable(new PublicKey(address));
    if (!result.value) throw new Error(`Address lookup table is missing: ${address}`);
    tables.push(result.value);
  }
  return tables;
}

function serializeWithSizeGuard(transaction: VersionedTransaction): Uint8Array {
  let serialized: Uint8Array;
  try {
    serialized = transaction.serialize();
  } catch (error) {
    throw new Error(
      `Atomic transaction is larger than Solana permits; create/use the custom ALT. ${(error as Error).message}`,
    );
  }
  if (serialized.length > SOLANA_PACKET_DATA_SIZE) {
    throw new Error(
      `Atomic transaction is ${serialized.length} bytes; maximum is ${SOLANA_PACKET_DATA_SIZE}`,
    );
  }
  return serialized;
}

function buildVersionedTransaction(params: {
  payer: PublicKey;
  blockhash: string;
  lookupTables: AddressLookupTableAccount[];
  instructions: TransactionInstruction[];
  computeUnitLimit: number;
}): VersionedTransaction {
  const message = new TransactionMessage({
    payerKey: params.payer,
    recentBlockhash: params.blockhash,
    instructions: [
      ComputeBudgetProgram.setComputeUnitLimit({ units: params.computeUnitLimit }),
      ComputeBudgetProgram.setComputeUnitPrice({
        microLamports: config.computeUnitPriceMicroLamports,
      }),
      ...params.instructions,
    ],
  }).compileToV0Message(params.lookupTables);
  return new VersionedTransaction(message);
}

export async function prepareAtomicTransaction(params: {
  connection: Connection;
  wallet: PublicKey;
  quote: RouteQuote;
}): Promise<PreparedAtomicTransaction> {
  const { connection, wallet, quote } = params;
  if (quote.principalRaw < config.minTradeRaw || quote.principalRaw > config.maxTradeRaw) {
    throw new Error("Refusing a flash loan outside the configured 10,000–100,000 USDC range");
  }
  if (quote.guaranteedNetProfitRaw < config.minNetProfitRaw) {
    throw new Error("Refusing to construct a transaction below the guaranteed net-profit floor");
  }
  const flashloan = await getFlashloanIx({
    connection,
    signer: wallet,
    asset: USDC_MINT,
    amount: new BN(quote.principalRaw.toString()),
    market: config.flashLoanMarket,
  });
  const stable = await buildStableSwapInstruction({
    wallet,
    assetFrom: quote.venueOrder === "stable_first" ? "USDC" : quote.token,
    assetTo: quote.venueOrder === "stable_first" ? quote.token : "USDC",
    amountRaw:
      quote.venueOrder === "stable_first" ? quote.principalRaw : quote.jupiterMinimumOutputRaw,
  });
  const setupInstructions = await filterSetupInstructions(
    connection,
    quote.build.setupInstructions,
  );
  const jupiterInstructions: TransactionInstruction[] = [
    ...setupInstructions,
    toTransactionInstruction(quote.build.swapInstruction),
    ...(quote.build.cleanupInstruction
      ? [toTransactionInstruction(quote.build.cleanupInstruction)]
      : []),
    ...quote.build.otherInstructions.map(toTransactionInstruction),
    ...(quote.build.tipInstruction
      ? [toTransactionInstruction(quote.build.tipInstruction)]
      : []),
  ];
  const customInstructions =
    quote.venueOrder === "stable_first"
      ? [stable.instruction, ...jupiterInstructions]
      : [...jupiterInstructions, stable.instruction];
  const arbInstructions = [flashloan.borrowIx, ...customInstructions, flashloan.paybackIx];
  const lookupTables = await loadLookupTables(connection, quote);
  const latest = await connection.getLatestBlockhash("confirmed");

  const simulationTransaction = buildVersionedTransaction({
    payer: wallet,
    blockhash: latest.blockhash,
    lookupTables,
    instructions: arbInstructions,
    computeUnitLimit: config.computeUnitLimit,
  });
  const initialSize = serializeWithSizeGuard(simulationTransaction).length;
  const simulation = await connection.simulateTransaction(simulationTransaction, {
    commitment: "confirmed",
    sigVerify: false,
  });
  if (simulation.value.err) {
    const logs = simulation.value.logs?.join("\n") ?? "no logs";
    throw new Error(`Atomic transaction simulation failed: ${JSON.stringify(simulation.value.err)}\n${logs}`);
  }
  const unitsConsumed = simulation.value.unitsConsumed ?? config.computeUnitLimit;
  const bufferedLimit = Math.min(
    MAX_COMPUTE_UNITS,
    Math.max(200_000, Math.ceil(unitsConsumed * config.computeUnitBuffer)),
  );
  const finalTransaction = buildVersionedTransaction({
    payer: wallet,
    blockhash: latest.blockhash,
    lookupTables,
    instructions: arbInstructions,
    computeUnitLimit: bufferedLimit,
  });
  const finalSize = serializeWithSizeGuard(finalTransaction).length;
  return {
    transaction: finalTransaction,
    blockhash: latest.blockhash,
    lastValidBlockHeight: latest.lastValidBlockHeight,
    unitsConsumed,
    computeUnitLimit: bufferedLimit,
    serializedLength: Math.max(initialSize, finalSize),
    logs: simulation.value.logs ?? [],
  };
}

export function signPreparedTransaction(
  prepared: PreparedAtomicTransaction,
  keypair: Keypair,
): PreparedAtomicTransaction {
  prepared.transaction.sign([keypair]);
  serializeWithSizeGuard(prepared.transaction);
  return prepared;
}
