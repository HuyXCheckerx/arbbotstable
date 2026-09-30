import "dotenv/config";

import { createHash, randomUUID } from "node:crypto";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  AssetTag,
  Bank,
  MARGINFI_IDL,
  MarginfiAccountWrapper,
  Project0Client,
  deriveMarginfiAccount,
  getConfig,
} from "@0dotxyz/p0-ts-sdk";
// @ts-ignore
import * as bufferLayout from "buffer-layout";

// Defensively patch buffer-layout Union.decode to avoid crashes on unknown enum discriminators
if ((bufferLayout as any)?.Union?.prototype?.decode) {
  const origUnionDecode = (bufferLayout as any).Union.prototype.decode;
  (bufferLayout as any).Union.prototype.decode = function (b: any, offset: any) {
    const dlo = this.discriminator;
    const discr = dlo.decode(b, offset ?? 0);
    if (this.registry[discr] === undefined && !this.defaultLayout) {
      const dest = this.makeDestinationObject();
      dest[dlo.property] = discr;
      return dest;
    }
    return origUnionDecode.call(this, b, offset);
  };
}

// Patch Marginfi IDL enums with placeholder variants for recently added on-chain types
for (const t of (MARGINFI_IDL as any)?.types ?? []) {
  if (t.type && t.type.kind === "enum") {
    while (t.type.variants.length < 64) {
      t.type.variants.push({ name: `Unknown${t.name}${t.type.variants.length}` });
    }
  }
}
import {
  AddressLookupTableAccount,
  Commitment,
  ComputeBudgetProgram,
  Connection,
  Keypair,
  PublicKey,
  SystemProgram,
  Transaction,
  TransactionExpiredBlockheightExceededError,
  TransactionInstruction,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
// @ts-ignore
import BN from "bn.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountIdempotentInstruction,
  createTransferCheckedInstruction,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import bs58 from "bs58";

export const USDC_MINT = new PublicKey(
  "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
);
export const USDT_MINT = new PublicKey(
  "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
);
export const PYUSD_MINT = new PublicKey(
  "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
);
export const USDG_MINT = new PublicKey(
  "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",
);
export const MAINNET_GENESIS_HASH =
  "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d";
export const STABLE_PROGRAM_ID = new PublicKey(
  "2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG",
);
export const KAMINO_PROGRAM_ID = new PublicKey(
  "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD",
);
export const KAMINO_MAIN_MARKET = new PublicKey(
  "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF",
);
export const KAMINO_MAIN_MARKET_AUTHORITY = new PublicKey(
  "9DrvZvyWh1HuAoZxvYWMvkf2XCzryCpGgHqrMjyDWpmo",
);
export const SYSVAR_INSTRUCTIONS = new PublicKey(
  "Sysvar1nstructions1111111111111111111111111",
);

export interface KaminoReserveInfo {
  reserve: PublicKey;
  supplyVault: PublicKey;
  feeVault: PublicKey;
  tokenProgram: PublicKey;
  feeBps: number;
}

export const KAMINO_RESERVES: Record<string, KaminoReserveInfo> = {
  [PYUSD_MINT.toBase58()]: {
    reserve: new PublicKey("2gc9Dm1eB6UgVYFBUN9bWks6Kes9PbWSaPaa9DqyvEiN"),
    supplyVault: new PublicKey("Gm2itCNPBpBSSrgCA194pmErjwHAFVpvBBFvpdTF5LuJ"),
    feeVault: new PublicKey("BcLJRx7GbyX2Jj8RFpYDnEE47Tm36wSskLnm7ALarEC1"),
    tokenProgram: TOKEN_2022_PROGRAM_ID,
    feeBps: 0,
  },
  [USDG_MINT.toBase58()]: {
    reserve: new PublicKey("ESCkPWKHmgNE7Msf77n9yzqJd5kQVWWGy3o5Mgxhvavp"),
    supplyVault: new PublicKey("DGBo8HmL7pBWBZLGePjHQ73JKRE37HVZ7ZbjA2FYUZHZ"),
    feeVault: new PublicKey("39M8hy7EUypjxsHqsZQmNQqXGpknCwoCXzF7r8WMBbAs"),
    tokenProgram: TOKEN_2022_PROGRAM_ID,
    feeBps: 0,
  },
  [USDC_MINT.toBase58()]: {
    reserve: new PublicKey("D6q6wuQSrifJKZYpR1M8R4YawnLDtDsMmWM1NbBmgJ59"),
    supplyVault: new PublicKey("Bgq7trRgVMeq33yt235zM2onQ4bRDBsY5EWiTetF4qw6"),
    feeVault: new PublicKey("BbDUrk1bVtSixgQsPLBJFZEF7mwGstnD5joA1WzYvYFX"),
    tokenProgram: TOKEN_PROGRAM_ID,
    feeBps: 0.1,
  },
  [USDT_MINT.toBase58()]: {
    reserve: new PublicKey("H3t6qZ1JkguCNTi9uzVKqQ7dvt2cum4XiXWom6Gn5e5S"),
    supplyVault: new PublicKey("2Eff8Udy2G2gzNcf2619AnTx3xM4renEv4QrHKjS1o9N"),
    feeVault: new PublicKey("ARCZqsnUpvPffquPjZR3sxpvScLQdbfZ5BGf3SZvyij7"),
    tokenProgram: TOKEN_PROGRAM_ID,
    feeBps: 0.1,
  },
};

export function getKaminoReserveInfo(mint: PublicKey): KaminoReserveInfo {
  const info = KAMINO_RESERVES[mint.toBase58()];
  if (!info) {
    throw new Error(
      `No known Kamino reserve configured for mint ${mint.toBase58()}`,
    );
  }
  return info;
}

export function kaminoFlashLoanFeeRaw(
  loanAmountRaw: bigint,
  loanMint: PublicKey,
): bigint {
  const info = getKaminoReserveInfo(loanMint);
  if (info.feeBps <= 0) return 0n;
  const feeBasisPointsScaled = BigInt(Math.round(info.feeBps * 10_000));
  return (loanAmountRaw * feeBasisPointsScaled) / 100_000_000n;
}

export function isMarginfiBorrowError(error: unknown): boolean {
  const msg = errorMessage(error);
  return (
    msg.includes("6026") ||
    msg.includes("0x178a") ||
    msg.includes("IllegalUtilizationRatio") ||
    msg.includes("LendingAccountBorrow") ||
    msg.includes("lending_account_borrow") ||
    msg.includes("MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA") ||
    msg.includes("Marginfi") ||
    msg.includes("marginfi") ||
    msg.includes("makeBorrowIx")
  );
}

/** Borrowable principal kept below a lending vault's full balance. */
export function flashHeadroomRaw(vaultRaw: bigint): bigint {
  return (vaultRaw * 95n) / 100n;
}

/** SPL Token and Token-2022 accounts share the base layout: amount is a u64 at offset 64. */
export function tokenAccountAmountRaw(data: Buffer | Uint8Array): bigint {
  if (data.length < 72) {
    throw new Error(`Token account data is ${data.length} bytes; expected at least 72`);
  }
  return Buffer.from(data.buffer, data.byteOffset, data.byteLength).readBigUInt64LE(64);
}

export async function getKaminoAvailableLiquidity(
  connection: Connection,
  mint: PublicKey,
): Promise<bigint> {
  const info = getKaminoReserveInfo(mint);
  try {
    const bal = await connection.getTokenAccountBalance(info.supplyVault);
    return flashHeadroomRaw(BigInt(bal.value.amount));
  } catch (err) {
    throw new Error(
      `Failed to fetch Kamino supply vault balance for ${mint.toBase58()}: ${errorMessage(err)}`,
    );
  }
}

const TOKEN_DECIMALS = 6;
const LAMPORTS_PER_SOL = 1_000_000_000;
const MAX_WIRE_TRANSACTION_BYTES = 1232;
const JUPITER_API_BASE = "https://api.jup.ag/swap/v1";
const JUPITER_LITE_API_BASE = "https://lite-api.jup.ag/swap/v1";
const SINGLE_CHAIN_SWAP_DISCRIMINATOR = createHash("sha256")
  .update("global:single_chain_swap")
  .digest()
  .subarray(0, 8);

type JsonRecord = Record<string, unknown>;

export interface JupiterQuote {
  inputMint: string;
  outputMint: string;
  inAmount: string;
  outAmount: string;
  otherAmountThreshold: string;
  swapMode: string;
  slippageBps: number;
  priceImpactPct?: string;
  routePlan: unknown[];
  contextSlot?: number;
  timeTaken?: number;
  provider?: "MetaMatcha" | "DFlow" | "Jupiter";
  aggregator?: string;
  serializedTransaction?: string;
  candidates?: JupiterQuote[];
  [key: string]: unknown;
}

interface JupiterInstructionJson {
  programId: string;
  accounts: Array<{
    pubkey: string;
    isSigner: boolean;
    isWritable: boolean;
  }>;
  data: string;
}

interface JupiterSwapInstructions {
  computeBudgetInstructions?: JupiterInstructionJson[];
  setupInstructions?: JupiterInstructionJson[];
  swapInstruction: JupiterInstructionJson;
  cleanupInstruction?: JupiterInstructionJson | null;
  otherInstructions?: JupiterInstructionJson[];
  addressLookupTableAddresses?: string[];
  error?: string;
  [key: string]: unknown;
}

export interface StableStatusAsset {
  asset: string;
  precision: number;
  balance: number;
  min: number;
  max: number;
  nativeFee: number;
  tokenFee: number;
  nativeFeeUsd?: number;
  amountFrom: string;
  amountTo: string;
  executionFeeNative: string;
  executionFeeUSD?: string;
  amlFeeUSD?: string;
  [key: string]: unknown;
}

interface StableStatusResponse {
  asset?: StableStatusAsset;
  data?: { asset?: StableStatusAsset } | StableStatusAsset;
  [key: string]: unknown;
}

export interface StableOrder {
  maintainerSignature: string;
  recoveryId?: number | string;
  nonce: number | string;
  deadline: number | string;
  executionFeeNative?: number | string;
  nativeFee?: number | string;
  amountFrom?: string;
  amountTo?: string;
  [key: string]: unknown;
}

interface StableOrderResponse {
  data?: StableOrder;
  [key: string]: unknown;
}

interface StableQuote {
  inputRaw: bigint;
  outputRaw: bigint;
  tokenFeeRaw: bigint;
  nativeFeeSol: number;
  status: StableStatusAsset;
}

interface SizedCycle {
  loanAmountRaw: bigint;
  firstQuote: JupiterQuote;
  secondJupiterQuote?: JupiterQuote;
  stableQuote: StableQuote;
  cycle: ConservativeCycle;
  capacityRaw: bigint;
  usableCapacityRaw: bigint;
  capacityAdjusted: boolean;
}

export interface StableLeg {
  instruction: TransactionInstruction;
  outputRaw: bigint;
  executionFeeLamports: bigint;
  nonce: bigint;
  deadline: bigint;
}

export interface ConservativeCycle {
  firstLegMinimumRaw: bigint;
  secondLegMinimumRaw: bigint;
  grossProfitRaw: bigint;
}

interface Config {
  rpcUrl: string;
  rpcFallbacks: string[];
  keypair: Keypair;
  subKeypair?: Keypair;
  dexProvider: "metamatcha" | "dflow" | "jupiter";
  dflowApiBase: string;
  matchaApiBase: string;
  matchaAggregators: string[];
  matchaPython: string;
  matchaHelperPath: string;
  jupiterApiBase: string;
  jupiterApiKeys: string[];
  stableApiBase: string;
  stableChainId: string;
  loanSymbol: string;
  loanMint: PublicKey;
  intermediateSymbol: string;
  intermediateMint: PublicKey;
  stableMaxExecutionFeeLamports: bigint;
  maximumLoanAmountRaw: bigint;
  stableCapacityBufferRaw: bigint;
  stableCapacitySizingAttempts: number;
  minimumGrossProfitRaw: bigint;
  minimumNetProfitRaw: bigint;
  maxAccounts: number;
  onlyDirectRoutes: boolean;
  httpTimeoutMs: number;
  httpAttempts: number;
  marginfiAccount?: PublicKey;
  probeComputeUnitLimit: number;
  computeUnitSafetyBps: number;
  computeUnitPriceMicroLamports: number;
  solUsdUrl: string;
  solUsdBufferBps: number;
  outputPath: string;
  commitment: Commitment;
  provider: "marginfi" | "kamino" | "solend" | "auto";
  swapOrder: "dex-first" | "stable-first";
  customLookupTableAddresses: PublicKey[];
}

export interface CliOptions {
  quoteOnly: boolean;
  send: boolean;
  createMarginfiAccount: boolean;
  setupSubAtas?: boolean;
  provider?: "marginfi" | "kamino" | "solend" | "auto";
  dexProvider?: "metamatcha" | "dflow" | "jupiter";
  swapOrder?: "dex-first" | "stable-first";
  lookupTable?: string;
  confirmation?: string;
}

export interface HttpRetryConfig {
  httpTimeoutMs: number;
  httpAttempts: number;
}

interface SwapLeg {
  instructions: TransactionInstruction[];
  lookupTables: AddressLookupTableAccount[];
  ignoredComputeBudgetInstructionCount: number;
}

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required in .env`);
  return value;
}

function envInt(name: string, fallback: number, minimum = 0): number {
  const raw = process.env[name]?.trim();
  const value = raw ? Number(raw) : fallback;
  if (!Number.isSafeInteger(value) || value < minimum) {
    throw new Error(`${name} must be an integer >= ${minimum}`);
  }
  return value;
}

function envBool(name: string, fallback: boolean): boolean {
  const raw = process.env[name]?.trim().toLowerCase();
  if (!raw) return fallback;
  if (["1", "true", "yes", "on"].includes(raw)) return true;
  if (["0", "false", "no", "off"].includes(raw)) return false;
  throw new Error(`${name} must be true or false`);
}

function configuredChoice<T extends string>(
  name: string,
  fallback: T,
  choices: readonly T[],
): T {
  const value = (process.env[name]?.trim().toLowerCase() || fallback) as T;
  if (!choices.includes(value)) {
    throw new Error(`${name} must be one of: ${choices.join(", ")}`);
  }
  return value;
}

export function resolveIntermediateMint(symbol: string): PublicKey {
  if (symbol === "USDC") return USDC_MINT;
  if (symbol === "PYUSD") return PYUSD_MINT;
  if (symbol === "USDG") return USDG_MINT;
  if (symbol === "USDT") return USDT_MINT;
  throw new Error(
    `SOL_FLASH_ARB_INTERMEDIATE_TOKEN must be USDC, PYUSD, USDG, or USDT; received ${symbol}`,
  );
}

export function resolveLoanMint(symbol: string): PublicKey {
  if (symbol === "USDC") return USDC_MINT;
  if (symbol === "PYUSD") return PYUSD_MINT;
  if (symbol === "USDG") return USDG_MINT;
  throw new Error(
    `SOL_FLASH_ARB_LOAN_TOKEN must be USDC, PYUSD, or USDG; received ${symbol}`,
  );
}

export function isTwoHopJupiterRoute(
  _loanMint: PublicKey,
  _intermediateMint: PublicKey,
): boolean {
  // Both MetaMatcha and current Jupiter quote responses can encode their own
  // internal USDC hop in one swap transaction. Splitting it into two external
  // legs is larger and makes the atomic bundle less likely to fit in 1,232 B.
  return false;
}

export function jupiterRouteConstraints(
  loanMint: PublicKey,
  intermediateMint: PublicKey,
  maxAccounts: number,
  onlyDirectRoutes: boolean,
): Pick<Config, "maxAccounts" | "onlyDirectRoutes"> {
  const isPyusdUsdgPair =
    (loanMint.equals(PYUSD_MINT) && intermediateMint.equals(USDG_MINT)) ||
    (loanMint.equals(USDG_MINT) && intermediateMint.equals(PYUSD_MINT));
  return {
    maxAccounts: isPyusdUsdgPair ? Math.max(24, maxAccounts) : maxAccounts,
    onlyDirectRoutes: isPyusdUsdgPair ? false : onlyDirectRoutes,
  };
}

export function stableInputSymbol(
  swapOrder: "dex-first" | "stable-first",
  loanSymbol: string,
  intermediateSymbol: string,
): string {
  return swapOrder === "stable-first" ? loanSymbol : intermediateSymbol;
}

export function stableOutputSymbol(
  swapOrder: "dex-first" | "stable-first",
  loanSymbol: string,
  intermediateSymbol: string,
): string {
  return swapOrder === "stable-first" ? intermediateSymbol : loanSymbol;
}

export function intermediateTokenProgram(mint: PublicKey): PublicKey {
  return mint.equals(PYUSD_MINT) || mint.equals(USDG_MINT)
    ? TOKEN_2022_PROGRAM_ID
    : TOKEN_PROGRAM_ID;
}

export function parseUiAmountToRaw(value: string, decimals = 6): bigint {
  const normalized = value.trim();
  const match = /^(\d+)(?:\.(\d+))?$/.exec(normalized);
  if (!match) throw new Error(`Invalid positive token amount: ${value}`);
  const fraction = match[2] ?? "";
  if (fraction.length > decimals) {
    throw new Error(`${value} has more than ${decimals} decimal places`);
  }
  const scale = 10n ** BigInt(decimals);
  const raw =
    BigInt(match[1]) * scale +
    BigInt(fraction.padEnd(decimals, "0") || "0");
  if (raw <= 0n) throw new Error("Token amount must be positive");
  return raw;
}

export function parseDecimalToRawFloor(value: string, decimals = 6): bigint {
  const normalized = value.trim();
  const match = /^(\d+)(?:\.(\d+))?$/.exec(normalized);
  if (!match) throw new Error(`Invalid non-negative token amount: ${value}`);
  const fraction = (match[2] ?? "").slice(0, decimals).padEnd(decimals, "0");
  return BigInt(match[1]) * 10n ** BigInt(decimals) + BigInt(fraction || "0");
}

export function parseDecimalToRawCeil(value: string, decimals = 6): bigint {
  const normalized = value.trim();
  const match = /^(\d+)(?:\.(\d+))?$/.exec(normalized);
  if (!match) throw new Error(`Invalid non-negative token amount: ${value}`);
  const scale = 10n ** BigInt(decimals);
  const allFraction = match[2] ?? "";
  const keptFraction = allFraction.slice(0, decimals).padEnd(decimals, "0");
  const discardedFraction = allFraction.slice(decimals);
  const rounded = /[1-9]/.test(discardedFraction);
  return BigInt(match[1]) * scale +
    BigInt(keptFraction || "0") +
    (rounded ? 1n : 0n);
}

export function formatRaw(value: bigint, decimals = 6): string {
  const negative = value < 0n;
  const absolute = negative ? -value : value;
  const scale = 10n ** BigInt(decimals);
  const whole = absolute / scale;
  const fraction = (absolute % scale).toString().padStart(decimals, "0");
  const trimmed = fraction.replace(/0+$/, "");
  return `${negative ? "-" : ""}${whole}${trimmed ? `.${trimmed}` : ""}`;
}

export function conservativeCycle(
  firstQuote: Pick<JupiterQuote, "otherAmountThreshold">,
  stableOutputRaw: bigint,
  loanAmountRaw: bigint,
): ConservativeCycle {
  const firstLegMinimumRaw = BigInt(firstQuote.otherAmountThreshold);
  return {
    firstLegMinimumRaw,
    secondLegMinimumRaw: stableOutputRaw,
    grossProfitRaw: stableOutputRaw - loanAmountRaw,
  };
}

export function capacityLimitedLoanAmount(
  loanAmountRaw: bigint,
  stableInputRaw: bigint,
  usableCapacityRaw: bigint,
): bigint {
  if (loanAmountRaw <= 0n || stableInputRaw <= 0n || usableCapacityRaw <= 0n) {
    throw new Error("Invalid Stable.com capacity sizing values");
  }
  if (stableInputRaw <= usableCapacityRaw) return loanAmountRaw;
  const adjusted = (loanAmountRaw * usableCapacityRaw) / stableInputRaw;
  if (adjusted <= 0n || adjusted >= loanAmountRaw) {
    throw new Error("Stable.com capacity is too small to size this route");
  }
  return adjusted;
}

export function capacityLimitedStableFirstLoanAmount(
  loanAmountRaw: bigint,
  stableInputRaw: bigint,
  usableCapacityRaw: bigint,
): bigint {
  return stableInputRaw > usableCapacityRaw
    ? capacityLimitedLoanAmount(loanAmountRaw, stableInputRaw, usableCapacityRaw)
    : loanAmountRaw;
}

export function bufferedFeeRaw(
  feeLamports: bigint,
  solUsd: number,
  bufferBps: number,
): bigint {
  if (!Number.isFinite(solUsd) || solUsd <= 0) {
    throw new Error("SOL/USD price must be positive");
  }
  const scaledSolUsd = BigInt(Math.ceil(solUsd * 1_000_000));
  const numerator =
    feeLamports * scaledSolUsd * BigInt(10_000 + Math.max(0, bufferBps));
  const denominator = BigInt(LAMPORTS_PER_SOL) * 10_000n;
  return (numerator + denominator - 1n) / denominator;
}

export function effectiveScaledMinimumProfitRaw(
  baseMinimumRaw: bigint,
  actualLoanRaw: bigint,
  configuredMaxLoanRaw: bigint,
  absoluteSafetyFloorRaw = 10_000n,
): bigint {
  if (configuredMaxLoanRaw <= 0n || actualLoanRaw >= configuredMaxLoanRaw) {
    return baseMinimumRaw;
  }
  const scaled = (baseMinimumRaw * actualLoanRaw) / configuredMaxLoanRaw;
  const floorLimit =
    baseMinimumRaw < absoluteSafetyFloorRaw
      ? baseMinimumRaw
      : absoluteSafetyFloorRaw;
  return scaled > floorLimit ? scaled : floorLimit;
}

export function finalComputeUnitLimit(
  unitsConsumed: number,
  safetyBps: number,
  maximum: number,
): number {
  if (!Number.isFinite(unitsConsumed) || unitsConsumed <= 0) {
    throw new Error("Simulation did not report positive compute usage");
  }
  const padded = Math.ceil((unitsConsumed * (10_000 + safetyBps)) / 10_000);
  return Math.min(maximum, Math.max(200_000, padded));
}

export function decodeJupiterInstruction(
  instruction: JupiterInstructionJson,
): TransactionInstruction {
  return new TransactionInstruction({
    programId: new PublicKey(instruction.programId),
    keys: instruction.accounts.map((account) => ({
      pubkey: new PublicKey(account.pubkey),
      isSigner: account.isSigner,
      isWritable: account.isWritable,
    })),
    data: Buffer.from(instruction.data, "base64"),
  });
}

function loadKeypair(secret: string): Keypair {
  let bytes: Uint8Array;
  try {
    if (secret.trim().startsWith("[")) {
      const parsed = JSON.parse(secret) as unknown;
      if (!Array.isArray(parsed) || !parsed.every(Number.isInteger)) {
        throw new Error("JSON key must be an integer array");
      }
      bytes = Uint8Array.from(parsed as number[]);
    } else {
      bytes = bs58.decode(secret.trim());
    }
    if (bytes.length === 64) return Keypair.fromSecretKey(bytes);
    if (bytes.length === 32) return Keypair.fromSeed(bytes);
  } catch (error) {
    throw new Error(
      `SOLANA_PRIVATE_KEY is not valid JSON-array or base58 key material: ${errorMessage(error)}`,
    );
  }
  throw new Error(`SOLANA_PRIVATE_KEY decoded to ${bytes.length} bytes; expected 32 or 64`);
}

export function deriveSubAccountKeypair(masterKeypair: Keypair, index = 1): Keypair {
  const seed = createHash("sha256")
    .update(masterKeypair.secretKey)
    .update(Buffer.from(`matcha_sub_taker_v${index}`))
    .digest();
  return Keypair.fromSeed(new Uint8Array(seed));
}

export async function ensureSubAccountAtas(
  connection: Connection,
  masterKeypair: Keypair,
  subKeypair: Keypair,
): Promise<void> {
  const mints = [
    { mint: USDC_MINT, program: intermediateTokenProgram(USDC_MINT), name: "USDC" },
    { mint: PYUSD_MINT, program: intermediateTokenProgram(PYUSD_MINT), name: "PYUSD" },
    { mint: USDG_MINT, program: intermediateTokenProgram(USDG_MINT), name: "USDG" },
  ];
  const missingIxs: TransactionInstruction[] = [];
  for (const { mint, program, name } of mints) {
    const ata = getAssociatedTokenAddressSync(mint, subKeypair.publicKey, false, program);
    const info = await connection.getAccountInfo(ata);
    if (!info) {
      console.log(`Preparing sub-account ATA for ${name} (${ata.toBase58()})...`);
      missingIxs.push(
        createAssociatedTokenAccountIdempotentInstruction(
          masterKeypair.publicKey,
          ata,
          subKeypair.publicKey,
          mint,
          program,
        ),
      );
    }
  }
  if (!missingIxs.length) {
    console.log("All sub-account ATAs already initialized.");
    return;
  }
  const latest = await connection.getLatestBlockhash("confirmed");
  const msg = new TransactionMessage({
    payerKey: masterKeypair.publicKey,
    recentBlockhash: latest.blockhash,
    instructions: missingIxs,
  }).compileToV0Message();
  const tx = new VersionedTransaction(msg);
  tx.sign([masterKeypair]);
  const sig = await connection.sendTransaction(tx);
  await connection.confirmTransaction(
    {
      signature: sig,
      blockhash: latest.blockhash,
      lastValidBlockHeight: latest.lastValidBlockHeight,
    },
    "confirmed",
  );
  console.log(`Sub-account ATAs initialized successfully! Signature: ${sig}`);
}

function readConfig(cli: CliOptions): Config {
  const accountValue = process.env.SOL_FLASH_ARB_MARGINFI_ACCOUNT?.trim();
  const apiKeys = (process.env.JUP_API_KEYS || process.env.JUP_API_KEY || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  const intermediateSymbol =
    process.env.SOL_FLASH_ARB_INTERMEDIATE_TOKEN?.trim().toUpperCase() || "PYUSD";
  const intermediateMint = resolveIntermediateMint(intermediateSymbol);
  const loanSymbol =
    process.env.SOL_FLASH_ARB_LOAN_TOKEN?.trim().toUpperCase() || "USDC";
  const loanMint = resolveLoanMint(loanSymbol);
  if (loanMint.equals(intermediateMint)) {
    throw new Error("SOL_FLASH_ARB_LOAN_TOKEN must differ from SOL_FLASH_ARB_INTERMEDIATE_TOKEN");
  }
  const provider =
    cli.provider ??
    configuredChoice(
      "SOL_FLASH_ARB_PROVIDER",
      "auto",
      ["marginfi", "kamino", "solend", "auto"] as const,
    );
  const swapOrder =
    cli.swapOrder ??
    configuredChoice(
      "SOL_FLASH_ARB_SWAP_ORDER",
      "stable-first",
      ["dex-first", "stable-first"] as const,
    );
  const dexProvider =
    cli.dexProvider ??
    configuredChoice(
      "SOL_FLASH_ARB_DEX_PROVIDER",
      "metamatcha",
      ["metamatcha", "dflow", "jupiter"] as const,
    );
  const capacitySymbol = stableInputSymbol(
    swapOrder,
    loanSymbol,
    intermediateSymbol,
  );
  const capacityBuffer =
    process.env[`SOL_FLASH_ARB_STABLE_CAPACITY_BUFFER_${capacitySymbol}`] ||
    "1";
  const configuredMaxAccounts = envInt("SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS", 20, 1);
  const configuredDirectRoutes = envBool("SOL_FLASH_ARB_ONLY_DIRECT_ROUTES", true);
  const routeConstraints = dexProvider === "jupiter"
    ? jupiterRouteConstraints(
        loanMint,
        intermediateMint,
        configuredMaxAccounts,
        configuredDirectRoutes,
      )
    : { maxAccounts: configuredMaxAccounts, onlyDirectRoutes: configuredDirectRoutes };
  const rawRpc = (process.env.SOLANA_RPC_URL || "").trim();
  const rawFallbacks = (process.env.SOLANA_RPC_FALLBACKS || "").trim();
  const allRpcCandidates = [
    ...rawRpc.split(",").map((s) => s.trim()),
    ...rawFallbacks.split(",").map((s) => s.trim()),
  ].filter(Boolean);
  const primaryRpc = allRpcCandidates[0];
  if (!primaryRpc) {
    throw new Error("Missing required environment variable: SOLANA_RPC_URL");
  }
  const rpcFallbacks = allRpcCandidates.slice(1);
  const keypair = loadKeypair(requiredEnv("SOLANA_PRIVATE_KEY"));
  const subTakerIndex = envInt("SOL_FLASH_ARB_SUB_TAKER_INDEX", 1, 1);
  const useSubTaker = process.env.SOL_FLASH_ARB_USE_SUB_TAKER !== "false";
  const subKeypair = useSubTaker ? deriveSubAccountKeypair(keypair, subTakerIndex) : undefined;
  return {
    rpcUrl: primaryRpc,
    rpcFallbacks,
    keypair,
    subKeypair,
    dexProvider,
    dflowApiBase: (
      process.env.SOL_FLASH_ARB_DFLOW_BASE_URL || "https://dev-quote-api.dflow.net"
    ).replace(/\/$/, ""),
    matchaApiBase: (
      process.env.SOL_FLASH_ARB_MATCHA_BASE_URL || "https://meta.matcha.xyz"
    ).replace(/\/$/, ""),
    matchaAggregators: (
      process.env.SOL_FLASH_ARB_MATCHA_AGGREGATORS || "0x,DFlow,Jupiter,OKX"
    ).split(",").map((value) => value.trim()).filter(Boolean),
    matchaPython:
      process.env.SOL_FLASH_ARB_MATCHA_PYTHON ||
      (() => {
        const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
        const venvEth = path.join(
          root,
          ".venv-eth",
          process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
        );
        if (fs.existsSync(venvEth)) return venvEth;
        const venv = path.join(
          root,
          ".venv",
          process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
        );
        if (fs.existsSync(venv)) return venv;
        return process.platform === "win32" ? "python" : "python3";
      })(),
    matchaHelperPath:
      process.env.SOL_FLASH_ARB_MATCHA_HELPER ||
      path.join(path.dirname(fileURLToPath(import.meta.url)), "metamatcha_solana.py"),
    jupiterApiBase: (
      process.env.SOL_FLASH_ARB_JUPITER_API_BASE ||
      (apiKeys.length ? JUPITER_API_BASE : JUPITER_LITE_API_BASE)
    ).replace(/\/$/, ""),
    jupiterApiKeys: apiKeys,
    stableApiBase: (
      process.env.SOL_FLASH_ARB_STABLE_API_BASE ||
      "https://api-defi.stable.com"
    ).replace(/\/$/, ""),
    stableChainId: process.env.SOL_FLASH_ARB_STABLE_CHAIN_ID || "102",
    loanSymbol,
    loanMint,
    intermediateSymbol,
    intermediateMint,
    stableMaxExecutionFeeLamports: BigInt(
      envInt("SOL_FLASH_ARB_STABLE_MAX_EXECUTION_FEE_LAMPORTS", 100_000, 0),
    ),
    maximumLoanAmountRaw: parseUiAmountToRaw(
      process.env[`SOL_FLASH_ARB_AMOUNT_${loanSymbol}`] ||
        process.env.SOL_FLASH_ARB_AMOUNT_USDC ||
        "100000",
    ),
    stableCapacityBufferRaw: parseDecimalToRawFloor(
      capacityBuffer,
    ),
    stableCapacitySizingAttempts: envInt(
      "SOL_FLASH_ARB_STABLE_CAPACITY_SIZING_ATTEMPTS",
      5,
      1,
    ),
    minimumGrossProfitRaw: parseUiAmountToRaw(
      process.env[`SOL_FLASH_ARB_MIN_GROSS_PROFIT_${loanSymbol}`] ||
        process.env.SOL_FLASH_ARB_MIN_GROSS_PROFIT_USDC ||
        "1",
    ),
    minimumNetProfitRaw: parseUiAmountToRaw(
      process.env[`SOL_FLASH_ARB_MIN_NET_PROFIT_${loanSymbol}`] ||
        process.env.SOL_FLASH_ARB_MIN_NET_PROFIT_USDC ||
        "1",
    ),
    maxAccounts: routeConstraints.maxAccounts,
    onlyDirectRoutes: routeConstraints.onlyDirectRoutes,
    httpTimeoutMs: envInt("SOL_FLASH_ARB_HTTP_TIMEOUT_MS", 15_000, 1),
    httpAttempts: envInt("SOL_FLASH_ARB_HTTP_ATTEMPTS", 3, 1),
    marginfiAccount: accountValue ? new PublicKey(accountValue) : undefined,
    probeComputeUnitLimit: envInt("SOL_FLASH_ARB_MAX_COMPUTE_UNITS", 1_400_000, 200_000),
    computeUnitSafetyBps: envInt("SOL_FLASH_ARB_COMPUTE_SAFETY_BPS", 1_500, 0),
    computeUnitPriceMicroLamports: envInt(
      "SOL_FLASH_ARB_CU_PRICE_MICROLAMPORTS",
      10_000,
      0,
    ),
    solUsdUrl:
      process.env.SOL_FLASH_ARB_SOL_USD_URL ||
      "https://data-api.binance.vision/api/v3/ticker/price?symbol=SOLUSDC",
    solUsdBufferBps: envInt("SOL_FLASH_ARB_SOL_PRICE_BUFFER_BPS", 100, 0),
    outputPath: process.env.SOL_FLASH_ARB_OUTPUT_PATH || "/tmp/solana-flash-arb-plan.json",
    commitment: "confirmed",
    provider,
    swapOrder,
    customLookupTableAddresses: (
      process.env.SOL_FLASH_ARB_CUSTOM_ALT || cli.lookupTable || ""
    )
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => new PublicKey(s)),
  };
}

export function parseCli(argv: string[]): CliOptions {
  const options: CliOptions = {
    quoteOnly: false,
    send: false,
    createMarginfiAccount: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--quote-only") options.quoteOnly = true;
    else if (arg === "--send") options.send = true;
    else if (arg === "--create-marginfi-account") options.createMarginfiAccount = true;
    else if (arg === "--setup-sub-atas") options.setupSubAtas = true;
    else if (arg === "--provider") {
      const p = argv[++index]?.toLowerCase();
      if (p !== "marginfi" && p !== "kamino" && p !== "solend" && p !== "auto") {
        throw new Error("--provider must be marginfi, kamino, solend, or auto");
      }
      options.provider = p;
    }
    else if (arg === "--dex-provider") {
      const p = argv[++index]?.toLowerCase();
      if (p !== "metamatcha" && p !== "dflow" && p !== "jupiter") {
        throw new Error("--dex-provider must be metamatcha, dflow, or jupiter");
      }
      options.dexProvider = p;
    }
    else if (arg === "--swap-order") {
      const order = argv[++index]?.toLowerCase();
      if (order !== "dex-first" && order !== "stable-first") {
        throw new Error("--swap-order must be dex-first or stable-first");
      }
      options.swapOrder = order;
    }
    else if (arg === "--lookup-table" || arg === "--alt") {
      options.lookupTable = argv[++index];
    }
    else if (arg === "--confirm-mainnet") options.confirmation = argv[++index];
    else if (arg === "--help" || arg === "-h") {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return options;
}

function printHelp(): void {
  console.log(`Usage:
  npm run solana:quote
  npm run solana:flash
  npm run solana:flash -- --dex-provider jupiter
  npm run solana:flash -- --send --confirm-mainnet EXECUTE_SOLANA_FLASH_ARB
  npm run solana:flash -- --create-marginfi-account --send --confirm-mainnet CREATE_MARGINFI_ACCOUNT

All amounts, RPC settings, API keys, fee settings, and account addresses come from .env.`);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function isBlockheightExpiry(error: unknown): boolean {
  return (
    error instanceof TransactionExpiredBlockheightExceededError ||
    (error instanceof Error &&
      (error.name === "TransactionExpiredBlockheightExceededError" ||
        /block height exceeded/i.test(error.message)))
  );
}

export function classifySignatureStatus(
  status: {
    err: unknown;
    confirmationStatus?: "processed" | "confirmed" | "finalized" | null;
  } | null,
): "missing" | "ambiguous" | "confirmed" | "reverted" {
  if (!status) return "missing";
  if (status.err !== null) return "reverted";
  if (
    status.confirmationStatus === "confirmed" ||
    status.confirmationStatus === "finalized"
  ) {
    return "confirmed";
  }
  return "ambiguous";
}

class HttpResponseError extends Error {
  constructor(
    message: string,
    readonly retryable: boolean,
    readonly retryAfterMs?: number,
  ) {
    super(message);
  }
}

function writePlan(outputPath: string, plan: JsonRecord): void {
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, `${JSON.stringify(plan, null, 2)}\n`, {
    mode: 0o600,
  });
}

// Each sniper check is a separate process. These small files carry facts
// between checks so every process does not rediscover them over the network.
// They are advisory: a missing or unreadable file only costs the lookup.
function runtimeStatePath(name: string): string {
  const directory =
    process.env.SOL_FLASH_ARB_STATE_DIR ||
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "logs");
  return path.join(directory, name);
}

function readRuntimeState<T>(name: string): T | undefined {
  try {
    return JSON.parse(fs.readFileSync(runtimeStatePath(name), "utf8")) as T;
  } catch {
    return undefined;
  }
}

function writeRuntimeState(name: string, value: unknown): void {
  const target = runtimeStatePath(name);
  const temporary = `${target}.${process.pid}.tmp`;
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(temporary, `${JSON.stringify(value)}\n`, { mode: 0o600 });
    fs.renameSync(temporary, target);
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    console.warn(`Could not update ${path.basename(target)}: ${errorMessage(error)}`);
  }
}

const METAMATCHA_DENIAL_STATE = "solana-metamatcha-denial.json";
const METAMATCHA_ACCESS_DENIAL =
  /\bHTTP\s+(?:401|403)\b|Vercel Security Checkpoint|access blocked by Vercel|x-vercel-mitigated=challenge/i;

function metaMatchaDenialCooldownMs(): number {
  const seconds = Number(process.env.SNIPER_PROVIDER_ACCESS_COOLDOWN_SECONDS ?? "3600");
  return Number.isFinite(seconds) && seconds >= 0 ? seconds * 1_000 : 3_600_000;
}

/** Milliseconds until MetaMatcha may be asked again after an access denial. */
export function metaMatchaDenialRemainingMs(now = Date.now()): number {
  const state = readRuntimeState<{ until?: number }>(METAMATCHA_DENIAL_STATE);
  const until = Number(state?.until ?? 0);
  return Number.isFinite(until) ? Math.max(0, until - now) : 0;
}

export function recordMetaMatchaDenial(detail: string, now = Date.now()): void {
  writeRuntimeState(METAMATCHA_DENIAL_STATE, {
    until: now + metaMatchaDenialCooldownMs(),
    recordedAt: new Date(now).toISOString(),
    detail: detail.slice(0, 300),
  });
}

const MARGINFI_BANK_STATE = "solana-marginfi-banks.json";

interface CachedMarginfiBank {
  bank: string;
  liquidityVault: string;
}

function cachedMarginfiVault(mint: PublicKey): PublicKey | undefined {
  const banks = readRuntimeState<Record<string, CachedMarginfiBank>>(MARGINFI_BANK_STATE);
  try {
    const vault = banks?.[mint.toBase58()]?.liquidityVault;
    return vault ? new PublicKey(vault) : undefined;
  } catch {
    return undefined;
  }
}

function rememberMarginfiBank(mint: PublicKey, bank: Bank): void {
  const entry = {
    bank: bank.address.toBase58(),
    liquidityVault: bank.liquidityVault.toBase58(),
  };
  const banks = readRuntimeState<Record<string, CachedMarginfiBank>>(MARGINFI_BANK_STATE) ?? {};
  const existing = banks[mint.toBase58()];
  if (existing?.bank === entry.bank && existing.liquidityVault === entry.liquidityVault) return;
  banks[mint.toBase58()] = entry;
  writeRuntimeState(MARGINFI_BANK_STATE, banks);
}

function responseRetryAfterMs(response: Response): number | undefined {
  const value = response.headers.get("retry-after")?.trim();
  if (!value) return undefined;
  if (/^\d+(?:\.\d+)?$/.test(value)) {
    return Math.max(0, Math.ceil(Number(value) * 1_000));
  }
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, date - Date.now()) : undefined;
}

export async function fetchJson<T>(
  url: string,
  init: RequestInit,
  config: HttpRetryConfig,
  description: string,
): Promise<T> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= config.httpAttempts; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), config.httpTimeoutMs);
    try {
      const response = await fetch(url, { ...init, signal: controller.signal });
      const body = await response.text();
      if (!response.ok) {
        const retryable = response.status === 429 || response.status >= 500;
        const excerpt = body.replace(/\s+/g, " ").slice(0, 300);
        const failure = `${description} returned HTTP ${response.status}${excerpt ? `: ${excerpt}` : ""}`;
        throw new HttpResponseError(
          failure,
          retryable,
          responseRetryAfterMs(response),
        );
      } else {
        return JSON.parse(body) as T;
      }
    } catch (error) {
      if (error instanceof HttpResponseError && !error.retryable) {
        throw new Error(`${description} failed: ${error.message}`);
      }
      lastError = error;
      if (attempt === config.httpAttempts) break;
    } finally {
      clearTimeout(timeout);
    }
    const retryAfterMs =
      lastError instanceof HttpResponseError ? lastError.retryAfterMs : undefined;
    const isServerError =
      lastError instanceof HttpResponseError &&
      (lastError.message.includes("HTTP 500") || lastError.message.includes("HTTP 520"));
    const delayBase = isServerError ? 1000 : 400;
    const delayMs = Math.min(30_000, Math.max(retryAfterMs ?? 0, attempt * delayBase));
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  throw new Error(`${description} failed: ${errorMessage(lastError)}`);
}

function jupiterHeaders(config: Config, requestNumber: number): HeadersInit {
  const headers: Record<string, string> = { accept: "application/json" };
  if (config.jupiterApiKeys.length) {
    headers["x-api-key"] =
      config.jupiterApiKeys[requestNumber % config.jupiterApiKeys.length];
  }
  return headers;
}

export function shouldFallbackToJupiterLite(
  apiBase: string,
  error: unknown,
): boolean {
  try {
    return (
      new URL(apiBase).hostname === "api.jup.ag" &&
      /HTTP 401\b/.test(errorMessage(error))
    );
  } catch {
    return false;
  }
}

function stableHeaders(): HeadersInit {
  return {
    accept: "application/json, text/plain, */*",
    "content-type": "application/json",
    origin: "https://stable.com",
    referer: "https://stable.com/",
    "user-agent":
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua":
      '"Not;A=Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": process.platform === "win32" ? '"Windows"' : '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
  };
}

function stableRequestPayload(
  config: Config,
  wallet: PublicKey,
  inputRaw: bigint,
  outputRaw?: bigint,
): JsonRecord {
  const isStableFirst = config.swapOrder === "stable-first";
  const assetFrom = isStableFirst ? config.loanSymbol : config.intermediateSymbol;
  const assetTo = isStableFirst ? config.intermediateSymbol : config.loanSymbol;
  const payload: JsonRecord = {
    chainFrom: config.stableChainId,
    assetFrom,
    chainTo: config.stableChainId,
    assetTo,
    gasLess: false,
    amountFrom: formatRaw(inputRaw),
    addressFrom: wallet.toBase58(),
    addressTo: wallet.toBase58(),
  };
  if (outputRaw !== undefined) payload.amountTo = formatRaw(outputRaw);
  return payload;
}

function unwrapStableStatus(response: StableStatusResponse): StableStatusAsset {
  if (response.asset) return response.asset;
  if (response.data && "amountTo" in response.data) {
    return response.data as StableStatusAsset;
  }
  if (
    response.data &&
    "asset" in response.data &&
    typeof response.data.asset === "object" &&
    response.data.asset
  ) {
    return response.data.asset;
  }
  throw new Error("Stable.com status response omitted asset quote data");
}

async function fetchStableStatus(
  config: Config,
  wallet: PublicKey,
  inputRaw: bigint,
): Promise<StableStatusAsset> {
  const response = await fetchJson<StableStatusResponse>(
    `${config.stableApiBase}/swap/status`,
    {
      method: "POST",
      headers: stableHeaders(),
      body: JSON.stringify(stableRequestPayload(config, wallet, inputRaw)),
    },
    config,
    "Stable.com status",
  );
  return unwrapStableStatus(response);
}

export interface StableCapacity {
  capacityRaw: bigint;
  minimumRaw: bigint;
  usableCapacityRaw: bigint;
}

/** Usable Stable.com pool capacity; throws when no valid order can fit. */
export function stableCapacity(
  status: Pick<StableStatusAsset, "balance" | "min" | "max">,
  bufferRaw: bigint,
  symbol: string,
): StableCapacity {
  return stableCapacityRaw(
    parseDecimalToRawFloor(String(status.balance)),
    parseDecimalToRawCeil(String(status.min)),
    parseDecimalToRawFloor(String(status.max)),
    bufferRaw,
    symbol,
  );
}

export function stableCapacityRaw(
  capacityRaw: bigint,
  minimumRaw: bigint,
  maximumRaw: bigint,
  bufferRaw: bigint,
  symbol: string,
): StableCapacity {
  if (capacityRaw <= 0n) {
    throw new Error(`Stable.com pool has no remaining ${symbol} capacity`);
  }
  const capacityAfterBufferRaw =
    capacityRaw > bufferRaw
      ? capacityRaw - bufferRaw
      : (capacityRaw > 1n ? capacityRaw - 1n : capacityRaw);
  const usableCapacityRaw =
    maximumRaw > 0n && maximumRaw < capacityAfterBufferRaw
      ? maximumRaw
      : capacityAfterBufferRaw;
  if (usableCapacityRaw < minimumRaw) {
    throw new Error(
      `Stable.com usable capacity ${formatRaw(usableCapacityRaw)} ${symbol} is below its ${formatRaw(minimumRaw)} ${symbol} minimum order`,
    );
  }
  return { capacityRaw, minimumRaw, usableCapacityRaw };
}

// Stable.com's reported pool balance is its on-chain vault minus a fixed
// protocol floor, rounded down to 0.01. Reading the vault answers the capacity
// question without an API request.
export const STABLE_POOL_PROTOCOL_FLOOR_RAW = 1_800_000n;
const STABLE_BALANCE_STEP_RAW = 10_000n;

/** Token account that holds Stable.com's pool of `mint` (the swap's payout source). */
export function stablePoolVault(mint: PublicKey): PublicKey {
  const [pool] = PublicKey.findProgramAddressSync(
    [Buffer.from("pool"), mint.toBuffer()],
    STABLE_PROGRAM_ID,
  );
  return getAssociatedTokenAddressSync(mint, pool, true, intermediateTokenProgram(mint));
}

export function stableReportedBalanceRaw(vaultRaw: bigint): bigint {
  const available =
    vaultRaw > STABLE_POOL_PROTOCOL_FLOOR_RAW ? vaultRaw - STABLE_POOL_PROTOCOL_FLOOR_RAW : 0n;
  return available - (available % STABLE_BALANCE_STEP_RAW);
}

/** Order limits and fees that /swap/status reports and that rarely change. */
export interface StableTerms {
  minimumRaw: bigint;
  maximumRaw: bigint;
  /** Token fee per million units of input, rounded up. */
  tokenFeePpm: bigint;
  nativeFeeSol: number;
}

const STABLE_TERMS_STATE = "solana-stable-terms.json";

interface StoredStableTerms {
  minimumRaw: string;
  maximumRaw: string;
  tokenFeePpm: string;
  nativeFeeSol: number;
  recordedAt: number;
}

function stableTermsKey(config: Pick<Config, "swapOrder" | "loanSymbol" | "intermediateSymbol">): string {
  return `${stableInputSymbol(config.swapOrder, config.loanSymbol, config.intermediateSymbol)}>` +
    stableOutputSymbol(config.swapOrder, config.loanSymbol, config.intermediateSymbol);
}

function stableTermsTtlMs(): number {
  const seconds = Number(process.env.SOL_FLASH_ARB_STABLE_TERMS_TTL_SECONDS ?? "21600");
  return Number.isFinite(seconds) && seconds >= 0 ? seconds * 1_000 : 21_600_000;
}

export function stableTermsFromStatus(status: StableStatusAsset): StableTerms {
  const amountFromRaw = parseDecimalToRawFloor(String(status.amountFrom));
  const tokenFeeRaw = parseDecimalToRawCeil(String(status.tokenFee ?? 0));
  return {
    minimumRaw: parseDecimalToRawCeil(String(status.min)),
    maximumRaw: parseDecimalToRawFloor(String(status.max)),
    tokenFeePpm:
      amountFromRaw > 0n ? (tokenFeeRaw * 1_000_000n + amountFromRaw - 1n) / amountFromRaw : 0n,
    nativeFeeSol: Number(status.nativeFee ?? 0),
  };
}

export function rememberStableTerms(
  config: Pick<Config, "swapOrder" | "loanSymbol" | "intermediateSymbol">,
  status: StableStatusAsset,
  now = Date.now(),
): void {
  const terms = stableTermsFromStatus(status);
  const stored = readRuntimeState<Record<string, StoredStableTerms>>(STABLE_TERMS_STATE) ?? {};
  stored[stableTermsKey(config)] = {
    minimumRaw: terms.minimumRaw.toString(),
    maximumRaw: terms.maximumRaw.toString(),
    tokenFeePpm: terms.tokenFeePpm.toString(),
    nativeFeeSol: terms.nativeFeeSol,
    recordedAt: now,
  };
  writeRuntimeState(STABLE_TERMS_STATE, stored);
}

export function cachedStableTerms(
  config: Pick<Config, "swapOrder" | "loanSymbol" | "intermediateSymbol">,
  now = Date.now(),
): StableTerms | undefined {
  const entry = readRuntimeState<Record<string, StoredStableTerms>>(STABLE_TERMS_STATE)?.[
    stableTermsKey(config)
  ];
  if (!entry || !(now - Number(entry.recordedAt) < stableTermsTtlMs())) return undefined;
  try {
    return {
      minimumRaw: BigInt(entry.minimumRaw),
      maximumRaw: BigInt(entry.maximumRaw),
      tokenFeePpm: BigInt(entry.tokenFeePpm),
      nativeFeeSol: Number(entry.nativeFeeSol) || 0,
    };
  } catch {
    return undefined;
  }
}

/**
 * The quote /swap/status would return, computed from the on-chain pool and the
 * cached terms. It drives sizing and the profit decision; a route that passes
 * is confirmed with Stable.com before any order is created.
 */
export function estimatedStableQuote(
  inputRaw: bigint,
  reportedBalanceRaw: bigint,
  terms: StableTerms,
  outputSymbol: string,
): StableQuote {
  const tokenFeeRaw = (inputRaw * terms.tokenFeePpm + 999_999n) / 1_000_000n;
  const outputRaw = inputRaw - tokenFeeRaw;
  return {
    inputRaw,
    outputRaw,
    tokenFeeRaw,
    nativeFeeSol: terms.nativeFeeSol,
    status: {
      asset: outputSymbol,
      precision: 6,
      balance: Number(formatRaw(reportedBalanceRaw)),
      min: Number(formatRaw(terms.minimumRaw)),
      max: Number(formatRaw(terms.maximumRaw)),
      nativeFee: terms.nativeFeeSol,
      tokenFee: Number(formatRaw(tokenFeeRaw)),
      amountFrom: formatRaw(inputRaw),
      amountTo: formatRaw(outputRaw),
      executionFeeNative: "0",
      estimated: true,
    },
  };
}

type StableQuoter = (inputRaw: bigint) => Promise<StableQuote>;

async function getStableQuote(
  config: Config,
  wallet: PublicKey,
  inputRaw: bigint,
): Promise<StableQuote> {
  const status = await fetchStableStatus(config, wallet, inputRaw);
  const quotedInputRaw = parseDecimalToRawFloor(status.amountFrom);
  if (quotedInputRaw !== inputRaw) {
    throw new Error(
      `Stable.com changed the requested input to ${status.amountFrom} ${stableInputSymbol(config.swapOrder, config.loanSymbol, config.intermediateSymbol)}`,
    );
  }
  const outputRaw = parseDecimalToRawFloor(status.amountTo);
  if (outputRaw <= 0n) {
    throw new Error(
      `Stable.com returned no ${stableOutputSymbol(config.swapOrder, config.loanSymbol, config.intermediateSymbol)} output`,
    );
  }
  return {
    inputRaw,
    outputRaw,
    tokenFeeRaw: parseDecimalToRawFloor(String(status.tokenFee ?? 0)),
    nativeFeeSol: Number(status.nativeFee ?? 0),
    status,
  };
}

function integerField(value: number | string | undefined, name: string): bigint {
  if (value === undefined || !/^\d+$/.test(String(value))) {
    throw new Error(`Stable.com order has invalid ${name}`);
  }
  return BigInt(value);
}

function unsignedLe(value: bigint): Buffer {
  if (value < 0n || value > 0xffff_ffff_ffff_ffffn) {
    throw new Error(`Unsigned 64-bit value is out of range: ${value}`);
  }
  const buffer = Buffer.alloc(8);
  buffer.writeBigUInt64LE(value);
  return buffer;
}

function signedLe(value: bigint): Buffer {
  if (value < -0x8000_0000_0000_0000n || value > 0x7fff_ffff_ffff_ffffn) {
    throw new Error(`Signed 64-bit value is out of range: ${value}`);
  }
  const buffer = Buffer.alloc(8);
  buffer.writeBigInt64LE(value);
  return buffer;
}

export function buildStableSwapInstruction(
  wallet: PublicKey,
  inputRaw: bigint,
  expectedOutputRaw: bigint,
  order: StableOrder,
  maximumExecutionFeeLamports: bigint,
  intermediateMint: PublicKey = PYUSD_MINT,
  outputMint: PublicKey = USDC_MINT,
): StableLeg {
  const signatureHex = order.maintainerSignature?.replace(/^0x/, "");
  if (!signatureHex || !/^[0-9a-fA-F]+$/.test(signatureHex)) {
    throw new Error("Stable.com order has an invalid maintainer signature");
  }
  const signatureBytes = Buffer.from(signatureHex, "hex");
  let maintainerSignature: Buffer;
  let recoveryId: number;
  if (signatureBytes.length === 65) {
    maintainerSignature = signatureBytes.subarray(0, 64);
    recoveryId = signatureBytes[64];
  } else if (signatureBytes.length === 64) {
    maintainerSignature = signatureBytes;
    recoveryId = Number(order.recoveryId ?? 0);
  } else {
    throw new Error(
      `Stable.com maintainer signature is ${signatureBytes.length} bytes; expected 64 or 65`,
    );
  }
  if (!Number.isSafeInteger(recoveryId) || recoveryId < 0 || recoveryId > 255) {
    throw new Error("Stable.com order has an invalid recoveryId");
  }

  if (
    order.amountFrom !== undefined &&
    parseDecimalToRawFloor(order.amountFrom) !== inputRaw
  ) {
    throw new Error("Stable.com signed order input differs from its status quote");
  }
  if (
    order.amountTo !== undefined &&
    parseDecimalToRawFloor(order.amountTo) !== expectedOutputRaw
  ) {
    throw new Error("Stable.com signed order output differs from its status quote");
  }

  const nonce = integerField(order.nonce, "nonce");
  const deadline = integerField(order.deadline, "deadline");
  const executionFeeLamports = integerField(
    order.executionFeeNative ?? order.nativeFee ?? 0,
    "executionFeeNative",
  );
  if (executionFeeLamports > maximumExecutionFeeLamports) {
    throw new Error(
      `Stable.com execution fee ${executionFeeLamports} lamports exceeds configured maximum ${maximumExecutionFeeLamports}`,
    );
  }

  const [mainState] = PublicKey.findProgramAddressSync(
    [Buffer.from("main_state")],
    STABLE_PROGRAM_ID,
  );
  const [nonceAccount] = PublicKey.findProgramAddressSync(
    [Buffer.from("nonce"), wallet.toBuffer()],
    STABLE_PROGRAM_ID,
  );
  const [nativeFeeAccount] = PublicKey.findProgramAddressSync(
    [Buffer.from("native_fee")],
    STABLE_PROGRAM_ID,
  );
  const [inputPool] = PublicKey.findProgramAddressSync(
    [Buffer.from("pool"), intermediateMint.toBuffer()],
    STABLE_PROGRAM_ID,
  );
  const [outputPool] = PublicKey.findProgramAddressSync(
    [Buffer.from("pool"), outputMint.toBuffer()],
    STABLE_PROGRAM_ID,
  );
  const inputTokenProgram = intermediateTokenProgram(intermediateMint);
  const outputTokenProgram = intermediateTokenProgram(outputMint);

  const userInputAta = getAssociatedTokenAddressSync(
    intermediateMint,
    wallet,
    false,
    inputTokenProgram,
  );
  const userOutputAta = getAssociatedTokenAddressSync(
    outputMint,
    wallet,
    false,
    outputTokenProgram,
  );
  const poolInputAta = getAssociatedTokenAddressSync(
    intermediateMint,
    inputPool,
    true,
    inputTokenProgram,
  );
  const poolOutputAta = getAssociatedTokenAddressSync(
    outputMint,
    outputPool,
    true,
    outputTokenProgram,
  );

  const data = Buffer.concat([
    SINGLE_CHAIN_SWAP_DISCRIMINATOR,
    unsignedLe(inputRaw),
    unsignedLe(executionFeeLamports),
    maintainerSignature,
    unsignedLe(nonce),
    signedLe(deadline),
    Buffer.from([recoveryId]),
  ]);
  const instruction = new TransactionInstruction({
    programId: STABLE_PROGRAM_ID,
    keys: [
      { pubkey: wallet, isSigner: true, isWritable: true },
      { pubkey: wallet, isSigner: true, isWritable: true },
      { pubkey: nonceAccount, isSigner: false, isWritable: true },
      { pubkey: mainState, isSigner: false, isWritable: false },
      { pubkey: nativeFeeAccount, isSigner: false, isWritable: true },
      { pubkey: inputPool, isSigner: false, isWritable: true },
      { pubkey: intermediateMint, isSigner: false, isWritable: false },
      { pubkey: poolInputAta, isSigner: false, isWritable: true },
      { pubkey: userInputAta, isSigner: false, isWritable: true },
      { pubkey: outputPool, isSigner: false, isWritable: true },
      { pubkey: outputMint, isSigner: false, isWritable: false },
      { pubkey: poolOutputAta, isSigner: false, isWritable: true },
      { pubkey: wallet, isSigner: false, isWritable: false },
      { pubkey: userOutputAta, isSigner: false, isWritable: true },
      { pubkey: inputTokenProgram, isSigner: false, isWritable: false },
      { pubkey: outputTokenProgram, isSigner: false, isWritable: false },
      {
        pubkey: ASSOCIATED_TOKEN_PROGRAM_ID,
        isSigner: false,
        isWritable: false,
      },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data,
  });
  return {
    instruction,
    outputRaw: expectedOutputRaw,
    executionFeeLamports,
    nonce,
    deadline,
  };
}

async function getStableLeg(
  config: Config,
  wallet: PublicKey,
  quote: StableQuote,
): Promise<StableLeg> {
  const response = await fetchJson<StableOrderResponse | StableOrder>(
    `${config.stableApiBase}/swap/create/singleChain`,
    {
      method: "POST",
      headers: stableHeaders(),
      body: JSON.stringify({
        ...stableRequestPayload(config, wallet, quote.inputRaw, quote.outputRaw),
        device: randomUUID(),
      }),
    },
    config,
    "Stable.com create order",
  );
  const order =
    "maintainerSignature" in response
      ? (response as StableOrder)
      : (response as StableOrderResponse).data;
  if (!order?.maintainerSignature) {
    throw new Error("Stable.com create order omitted maintainerSignature");
  }
  const intermediateMint = config.swapOrder === "stable-first" ? config.loanMint : config.intermediateMint;
  const outputMint = config.swapOrder === "stable-first" ? config.intermediateMint : config.loanMint;
  const leg = buildStableSwapInstruction(
    wallet,
    quote.inputRaw,
    quote.outputRaw,
    order,
    config.stableMaxExecutionFeeLamports,
    intermediateMint,
    outputMint,
  );
  const minimumDeadline = BigInt(Math.floor(Date.now() / 1_000) + 5);
  if (leg.deadline <= minimumDeadline) {
    throw new Error("Stable.com signed order expires too soon to submit safely");
  }
  return leg;
}

export function buildJupiterQuoteParameters(
  inputMint: PublicKey,
  outputMint: PublicKey,
  amountRaw: bigint,
  onlyDirectRoutes: boolean,
  maxAccounts?: number,
): URLSearchParams {
  const params = new URLSearchParams({
    inputMint: inputMint.toBase58(),
    outputMint: outputMint.toBase58(),
    amount: amountRaw.toString(),
    swapMode: "ExactIn",
    slippageBps: "0",
    restrictIntermediateTokens: "true",
    onlyDirectRoutes: String(onlyDirectRoutes),
  });
  if (maxAccounts !== undefined) {
    params.set("maxAccounts", String(maxAccounts));
  }
  return params;
}

export function dexProviderName(
  provider: "metamatcha" | "dflow" | "jupiter",
): "MetaMatcha" | "DFlow" | "Jupiter" {
  if (provider === "metamatcha") return "MetaMatcha";
  if (provider === "dflow") return "DFlow";
  return "Jupiter";
}

function runJsonHelper<T>(
  executable: string,
  helperPath: string,
  payload: JsonRecord,
  timeoutMs: number,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [helperPath], {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`MetaMatcha helper timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(new Error(`Could not start MetaMatcha helper: ${error.message}`));
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        const errLines = stderr.trim().split("\n").map((l) => l.trim()).filter(Boolean);
        const lastErr = errLines.find((l) => l.includes("MetaMatcha quote failed:")) || errLines[errLines.length - 1] || `MetaMatcha helper exited with code ${code}`;
        reject(new Error(lastErr));
        return;
      }
      try {
        resolve(JSON.parse(stdout) as T);
      } catch (error) {
        reject(new Error(`MetaMatcha helper returned invalid JSON: ${errorMessage(error)}`));
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

const METAMATCHA_SERVE_PREFIX = "@@ARBBOT_METAMATCHA@@";

interface PendingHelperRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

/**
 * A MetaMatcha helper kept running between quotes (`--serve`), used only by
 * the warm engine worker. It answers one request at a time; a concurrent
 * quote uses a one-shot helper instead so quotes still run in parallel.
 */
class PersistentJsonHelper {
  private readonly child: ChildProcessWithoutNullStreams;
  private buffer = "";
  private nextId = 1;
  private pending: { id: number; request: PendingHelperRequest } | undefined;
  exited = false;

  constructor(executable: string, helperPath: string) {
    this.child = spawn(executable, [helperPath, "--serve"], {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    // Never keep a finished engine process alive for an idle helper.
    this.child.unref();
    for (const stream of [this.child.stdin, this.child.stdout, this.child.stderr]) {
      (stream as unknown as { unref?: () => void }).unref?.();
    }
    this.child.stderr.resume();
    this.child.stdout.setEncoding("utf8");
    this.child.stdout.on("data", (chunk: string) => this.onData(chunk));
    this.child.stdin.on("error", () => undefined);
    this.child.once("error", (error) => this.fail(new Error(`Could not start MetaMatcha helper: ${error.message}`)));
    this.child.once("close", (code) => this.fail(new Error(`MetaMatcha helper exited with code ${code}`)));
  }

  get busy(): boolean {
    return this.pending !== undefined;
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let newline = this.buffer.indexOf("\n");
    while (newline >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      newline = this.buffer.indexOf("\n");
      if (!line.startsWith(METAMATCHA_SERVE_PREFIX) || !this.pending) continue;
      let response: { id?: number; ok?: boolean; result?: unknown; error?: string };
      try {
        response = JSON.parse(line.slice(METAMATCHA_SERVE_PREFIX.length));
      } catch (error) {
        this.settle(undefined, new Error(`MetaMatcha helper returned invalid JSON: ${errorMessage(error)}`));
        continue;
      }
      if (response.id !== this.pending.id) continue;
      if (response.ok) this.settle(response.result);
      else this.settle(undefined, new Error(response.error || "MetaMatcha quote failed"));
    }
  }

  private settle(value: unknown, error?: Error): void {
    const pending = this.pending;
    if (!pending) return;
    this.pending = undefined;
    clearTimeout(pending.request.timer);
    if (error) pending.request.reject(error);
    else pending.request.resolve(value);
  }

  private fail(error: Error): void {
    this.exited = true;
    this.settle(undefined, error);
  }

  request<T>(payload: JsonRecord, timeoutMs: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const id = this.nextId++;
      const timer = setTimeout(() => {
        // Same as a one-shot helper: stop the one that overran.
        this.kill();
        this.fail(new Error(`MetaMatcha helper timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending = { id, request: { resolve: resolve as (value: unknown) => void, reject, timer } };
      // The helper inherits the environment a one-shot spawn would get now.
      this.child.stdin.write(`${JSON.stringify({ id, env: process.env, request: payload })}\n`);
    });
  }

  kill(): void {
    this.exited = true;
    this.child.kill();
  }
}

let persistentMatchaHelpersEnabled = false;
let persistentMatchaHelper: PersistentJsonHelper | undefined;

/** Called by the warm engine worker; one-shot CLI runs keep one-shot helpers. */
export function enablePersistentMatchaHelper(enabled = true): void {
  persistentMatchaHelpersEnabled = enabled;
  if (!enabled) {
    persistentMatchaHelper?.kill();
    persistentMatchaHelper = undefined;
  }
}

function runMatchaHelper<T>(
  executable: string,
  helperPath: string,
  payload: JsonRecord,
  timeoutMs: number,
): Promise<T> {
  if (!persistentMatchaHelpersEnabled) {
    return runJsonHelper<T>(executable, helperPath, payload, timeoutMs);
  }
  if (persistentMatchaHelper?.exited) persistentMatchaHelper = undefined;
  if (!persistentMatchaHelper) {
    try {
      persistentMatchaHelper = new PersistentJsonHelper(executable, helperPath);
    } catch {
      return runJsonHelper<T>(executable, helperPath, payload, timeoutMs);
    }
  }
  if (persistentMatchaHelper.busy) {
    return runJsonHelper<T>(executable, helperPath, payload, timeoutMs);
  }
  return persistentMatchaHelper.request<T>(payload, timeoutMs);
}

export async function getMetaMatchaQuote(
  config: Pick<
    Config,
    | "matchaApiBase"
    | "matchaAggregators"
    | "matchaPython"
    | "matchaHelperPath"
    | "httpTimeoutMs"
    | "httpAttempts"
  >,
  inputMint: PublicKey,
  outputMint: PublicKey,
  amountRaw: bigint,
  wallet: PublicKey,
): Promise<JupiterQuote> {
  if (!config.matchaAggregators.length) {
    throw new Error("SOL_FLASH_ARB_MATCHA_AGGREGATORS must not be empty");
  }
  // A denial costs a Python/browser round trip of up to ~20 s per check.
  // Honor it for the provider-access cooldown instead of asking again.
  const deniedForMs = metaMatchaDenialRemainingMs();
  if (deniedForMs > 0) {
    throw new Error(
      `MetaMatcha quote skipped: access was denied (HTTP 403) recently; next attempt in ${Math.ceil(deniedForMs / 1_000)}s`,
    );
  }
  let lastError: unknown;
  for (let attempt = 1; attempt <= config.httpAttempts; attempt += 1) {
    try {
      const quote = await runMatchaHelper<JupiterQuote>(
        config.matchaPython,
        config.matchaHelperPath,
        {
          baseUrl: config.matchaApiBase,
          aggregators: config.matchaAggregators,
          inputMint: inputMint.toBase58(),
          outputMint: outputMint.toBase58(),
          amount: amountRaw.toString(),
          taker: wallet.toBase58(),
          slippageBps: 0,
          timeoutMs: config.httpTimeoutMs,
        },
        Math.max(config.httpTimeoutMs + 5_000, 120_000),
      );
      if (quote.provider !== "MetaMatcha") {
        throw new Error("MetaMatcha helper returned an unexpected provider");
      }
      if (quote.inputMint !== inputMint.toBase58() || quote.outputMint !== outputMint.toBase58()) {
        throw new Error("MetaMatcha changed the requested token pair");
      }
      if (BigInt(quote.inAmount) !== amountRaw) {
        throw new Error(`MetaMatcha changed the requested input amount to ${quote.inAmount}`);
      }
      if (!quote.routePlan?.length || !quote.serializedTransaction) {
        throw new Error("MetaMatcha returned no executable swap route");
      }
      return quote;
    } catch (error) {
      lastError = error;
      // The sniper defers access denials and rate limits until its cooldown.
      // Repeating the whole competition here only adds rejected requests.
      if (METAMATCHA_ACCESS_DENIAL.test(errorMessage(error))) {
        // One competitor's denial (listed under "request failures") says
        // nothing about MetaMatcha itself, so only its own block is recorded.
        if (!/request failures:/i.test(errorMessage(error))) {
          recordMetaMatchaDenial(errorMessage(error));
        }
        break;
      }
      if (/\bHTTP\s+429\b/i.test(errorMessage(error))) break;
      if (attempt < config.httpAttempts) {
        await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
      }
    }
  }
  const message = errorMessage(lastError);
  throw new Error(
    message.startsWith("MetaMatcha quote failed:")
      ? message
      : `MetaMatcha quote failed: ${message}`,
  );
}

async function getJupiterQuote(
  config: Config,
  inputMint: PublicKey,
  outputMint: PublicKey,
  amountRaw: bigint,
  requestNumber: number,
  overrideMaxAccounts?: number,
): Promise<JupiterQuote> {
  const params = buildJupiterQuoteParameters(
    inputMint,
    outputMint,
    amountRaw,
    config.onlyDirectRoutes,
    overrideMaxAccounts ?? config.maxAccounts,
  );
  let quote: JupiterQuote;
  try {
    quote = await fetchJson<JupiterQuote>(
      `${config.jupiterApiBase}/quote?${params}`,
      { headers: jupiterHeaders(config, requestNumber) },
      config,
      "Jupiter quote",
    );
  } catch (error) {
    if (!shouldFallbackToJupiterLite(config.jupiterApiBase, error)) throw error;
    console.warn(
      "Jupiter API key was rejected; retrying through the public Lite API.",
    );
    config.jupiterApiBase = JUPITER_LITE_API_BASE;
    config.jupiterApiKeys = [];
    quote = await fetchJson<JupiterQuote>(
      `${config.jupiterApiBase}/quote?${params}`,
      { headers: jupiterHeaders(config, requestNumber) },
      config,
      "Jupiter Lite quote",
    );
  }
  if (!quote.routePlan?.length) throw new Error("Jupiter returned no swap route");
  if (BigInt(quote.inAmount) !== amountRaw) {
    throw new Error(`Jupiter changed the requested input amount to ${quote.inAmount}`);
  }
  return quote;
}

export async function getDFlowQuote(
  config: Config,
  inputMint: PublicKey,
  outputMint: PublicKey,
  amountRaw: bigint,
): Promise<JupiterQuote> {
  const url = `${config.dflowApiBase}/quote?inputMint=${inputMint.toBase58()}&outputMint=${outputMint.toBase58()}&amount=${amountRaw.toString()}&slippageBps=0`;
  const quote = await fetchJson<JupiterQuote>(
    url,
    { headers: { accept: "application/json" } },
    config,
    "DFlow quote",
  );
  if (!quote.routePlan?.length) throw new Error("DFlow returned no swap route");
  quote.provider = "DFlow";
  if (!quote.otherAmountThreshold && quote.outAmount) {
    quote.otherAmountThreshold = String(quote.outAmount);
  }
  return quote;
}

async function getDexQuote(
  config: Config,
  inputMint: PublicKey,
  outputMint: PublicKey,
  amountRaw: bigint,
  wallet: PublicKey,
  requestNumber: number,
  overrideMaxAccounts?: number,
): Promise<JupiterQuote> {
  // Quotes come only from the configured provider. Falling back to another
  // venue is opt-in, so a MetaMatcha failure surfaces (and pauses the route)
  // instead of silently trading on a different DEX.
  const fallbackToDflow = process.env.SOL_FLASH_ARB_FALLBACK_DFLOW === "true";
  const fallbackToJupiter = process.env.SOL_FLASH_ARB_FALLBACK_JUPITER === "true";
  const jupiterQuote = () =>
    getJupiterQuote(config, inputMint, outputMint, amountRaw, requestNumber, overrideMaxAccounts);
  if (config.dexProvider === "metamatcha") {
    try {
      const taker = config.subKeypair ? config.subKeypair.publicKey : wallet;
      return await getMetaMatchaQuote(config, inputMint, outputMint, amountRaw, taker);
    } catch (error) {
      if (fallbackToDflow) {
        console.warn(
          `MetaMatcha quote unavailable (${errorMessage(error)}); falling back to DFlow DEX...`,
        );
        try {
          return await getDFlowQuote(config, inputMint, outputMint, amountRaw);
        } catch (dflowError) {
          if (!fallbackToJupiter) throw dflowError;
          console.warn(
            `DFlow quote unavailable (${errorMessage(dflowError)}); falling back to Jupiter DEX...`,
          );
          return await jupiterQuote();
        }
      }
      if (!fallbackToJupiter) throw error;
      console.warn(
        `MetaMatcha quote unavailable (${errorMessage(error)}); falling back to Jupiter DEX...`,
      );
      return await jupiterQuote();
    }
  }
  if (config.dexProvider === "dflow") {
    try {
      return await getDFlowQuote(config, inputMint, outputMint, amountRaw);
    } catch (error) {
      if (!fallbackToJupiter) throw error;
      console.warn(
        `DFlow quote unavailable (${errorMessage(error)}); falling back to Jupiter DEX...`,
      );
      return await jupiterQuote();
    }
  }
  return jupiterQuote();
}

async function getCapacitySizedCycle(
  config: Config,
  wallet: PublicKey,
  maxBankBorrowRaw: bigint | undefined,
  quoteStable: StableQuoter,
): Promise<SizedCycle> {
  let loanAmountRaw = config.maximumLoanAmountRaw;
  if (maxBankBorrowRaw && maxBankBorrowRaw > 0n && loanAmountRaw > maxBankBorrowRaw) {
    console.log(
      `Flash-loan liquidity headroom capped the loan from ${formatRaw(loanAmountRaw)} to ${formatRaw(maxBankBorrowRaw)} ${config.loanSymbol}`,
    );
    loanAmountRaw = maxBankBorrowRaw;
  }
  let capacityAdjusted = false;
  const isTwoHop = config.dexProvider === "jupiter" && isTwoHopJupiterRoute(
    config.loanMint,
    config.intermediateMint,
  );

  for (
    let attempt = 0;
    attempt < config.stableCapacitySizingAttempts;
    attempt += 1
  ) {
    if (config.swapOrder === "stable-first") {
      const stableQuote = await quoteStable(loanAmountRaw);
      const { capacityRaw, minimumRaw, usableCapacityRaw } = stableCapacity(
        stableQuote.status,
        config.stableCapacityBufferRaw,
        config.loanSymbol,
      );
      if (stableQuote.inputRaw > usableCapacityRaw) {
        const adjustedLoanAmountRaw = capacityLimitedStableFirstLoanAmount(
          loanAmountRaw,
          stableQuote.inputRaw,
          usableCapacityRaw,
        );
        console.log(
          `Stable.com capacity reduced the loan from ${formatRaw(loanAmountRaw)} to ${formatRaw(adjustedLoanAmountRaw)} ${config.loanSymbol}; requesting fresh quotes...`,
        );
        loanAmountRaw = adjustedLoanAmountRaw;
        capacityAdjusted = true;
        continue;
      }
      if (stableQuote.inputRaw < minimumRaw) {
        throw new Error(
          `Sized Stable.com order ${formatRaw(stableQuote.inputRaw)} ${config.loanSymbol} is below its ${formatRaw(minimumRaw)} ${config.loanSymbol} minimum`,
        );
      }

      let jupiterQuote: JupiterQuote;
      let secondQuote: JupiterQuote | undefined;
      let otherAmountThresholdRaw: bigint;

      if (isTwoHop) {
        const twoHopMaxAccounts = Math.min(13, config.maxAccounts);
        jupiterQuote = await getDexQuote(
          config,
          config.intermediateMint,
          USDC_MINT,
          stableQuote.outputRaw,
          wallet,
          attempt * 2,
          twoHopMaxAccounts,
        );
        const usdcMinimumRaw = BigInt(jupiterQuote.otherAmountThreshold);
        secondQuote = await getDexQuote(
          config,
          USDC_MINT,
          config.loanMint,
          usdcMinimumRaw,
          wallet,
          attempt * 2 + 1,
          twoHopMaxAccounts,
        );
        otherAmountThresholdRaw = BigInt(secondQuote.otherAmountThreshold);
      } else {
        jupiterQuote = await getDexQuote(
          config,
          config.intermediateMint,
          config.loanMint,
          stableQuote.outputRaw,
          wallet,
          attempt,
        );
        otherAmountThresholdRaw = BigInt(jupiterQuote.otherAmountThreshold);
      }
      const grossProfitRaw = otherAmountThresholdRaw - loanAmountRaw;

      const cycle: ConservativeCycle = {
        firstLegMinimumRaw: stableQuote.outputRaw,
        secondLegMinimumRaw: otherAmountThresholdRaw,
        grossProfitRaw,
      };

      return {
        loanAmountRaw,
        firstQuote: jupiterQuote,
        secondJupiterQuote: secondQuote,
        stableQuote,
        cycle,
        capacityRaw,
        usableCapacityRaw,
        capacityAdjusted,
      };
    }

    let firstQuote: JupiterQuote;
    let secondJupiterQuote: JupiterQuote | undefined;
    let firstMinimumRaw: bigint;

    if (isTwoHop) {
      const twoHopMaxAccounts = Math.min(13, config.maxAccounts);
      firstQuote = await getDexQuote(
        config,
        config.loanMint,
        USDC_MINT,
        loanAmountRaw,
        wallet,
        attempt * 2,
        twoHopMaxAccounts,
      );
      const usdcMinimumRaw = BigInt(firstQuote.otherAmountThreshold);
      secondJupiterQuote = await getDexQuote(
        config,
        USDC_MINT,
        config.intermediateMint,
        usdcMinimumRaw,
        wallet,
        attempt * 2 + 1,
        twoHopMaxAccounts,
      );
      firstMinimumRaw = BigInt(secondJupiterQuote.otherAmountThreshold);
    } else {
      firstQuote = await getDexQuote(
        config,
        config.loanMint,
        config.intermediateMint,
        loanAmountRaw,
        wallet,
        attempt,
      );
      firstMinimumRaw = BigInt(firstQuote.otherAmountThreshold);
    }

    const stableQuote = await quoteStable(firstMinimumRaw);
    const { capacityRaw, minimumRaw, usableCapacityRaw } = stableCapacity(
      stableQuote.status,
      config.stableCapacityBufferRaw,
      config.intermediateSymbol,
    );

    if (stableQuote.inputRaw <= usableCapacityRaw) {
      if (stableQuote.inputRaw < minimumRaw) {
        throw new Error(
          `Sized Stable.com order ${formatRaw(stableQuote.inputRaw)} ${config.intermediateSymbol} is below its ${formatRaw(minimumRaw)} ${config.intermediateSymbol} minimum`,
        );
      }
      const cycle = conservativeCycle(
        secondJupiterQuote ?? firstQuote,
        stableQuote.outputRaw,
        loanAmountRaw,
      );
      const effectiveMinGrossRaw = effectiveScaledMinimumProfitRaw(
        config.minimumGrossProfitRaw,
        loanAmountRaw,
        config.maximumLoanAmountRaw,
      );
      if (cycle.grossProfitRaw >= effectiveMinGrossRaw || attempt >= config.stableCapacitySizingAttempts - 1) {
        return {
          loanAmountRaw,
          firstQuote,
          secondJupiterQuote,
          stableQuote,
          cycle,
          capacityRaw,
          usableCapacityRaw,
          capacityAdjusted,
        };
      }
      const stepDownRaw = 5_000_000_000n; // 5,000 tokens
      const downsizedLoan = loanAmountRaw > stepDownRaw + minimumRaw ? loanAmountRaw - stepDownRaw : (loanAmountRaw * 8n) / 10n;
      if (downsizedLoan < minimumRaw) {
        return {
          loanAmountRaw,
          firstQuote,
          secondJupiterQuote,
          stableQuote,
          cycle,
          capacityRaw,
          usableCapacityRaw,
          capacityAdjusted,
        };
      }
      console.log(
        `Gross profit at ${formatRaw(loanAmountRaw)} ${config.loanSymbol} was ${formatRaw(cycle.grossProfitRaw)}; stepping down to ${formatRaw(downsizedLoan)} ${config.loanSymbol}...`,
      );
      loanAmountRaw = downsizedLoan;
      capacityAdjusted = true;
      continue;
    }

    const adjustedLoanAmountRaw = capacityLimitedLoanAmount(
      loanAmountRaw,
      stableQuote.inputRaw,
      usableCapacityRaw,
    );
    console.log(
      `Stable.com capacity reduced the loan from ${formatRaw(loanAmountRaw)} to ${formatRaw(adjustedLoanAmountRaw)} ${config.loanSymbol}; requesting fresh quotes...`,
    );
    loanAmountRaw = adjustedLoanAmountRaw;
    capacityAdjusted = true;
  }

  throw new Error(
    "Stable.com capacity kept changing; could not size an executable route",
  );
}

async function getSwapLeg(
  config: Config,
  quote: JupiterQuote,
  wallet: PublicKey,
  requestNumber: number,
  connection: Connection,
): Promise<SwapLeg> {
  if (config.dexProvider === "metamatcha" && quote.provider === "MetaMatcha") {
    if (!quote.serializedTransaction) {
      throw new Error("MetaMatcha quote omitted its serialized transaction");
    }
    let transaction: VersionedTransaction;
    try {
      transaction = VersionedTransaction.deserialize(
        Buffer.from(quote.serializedTransaction, "base64"),
      );
    } catch (error) {
      throw new Error(`MetaMatcha returned an invalid Solana transaction: ${errorMessage(error)}`);
    }
    const lookupTableAddresses = transaction.message.addressTableLookups.map(
      (lookup) => lookup.accountKey,
    );
    const lookupTables = await fetchLookupTables(connection, lookupTableAddresses);
    const decompiled = TransactionMessage.decompile(transaction.message, {
      addressLookupTableAccounts: lookupTables,
    });
    const subKeypair = config.subKeypair;
    const expectedPayer = subKeypair ? subKeypair.publicKey : wallet;
    if (!decompiled.payerKey.equals(wallet) && !decompiled.payerKey.equals(expectedPayer)) {
      throw new Error(
        `MetaMatcha transaction payer ${decompiled.payerKey.toBase58()} does not match wallet ${wallet.toBase58()} or sub-account ${expectedPayer.toBase58()}`,
      );
    }
    const unexpectedSigner = decompiled.instructions
      .flatMap((instruction) => instruction.keys)
      .find((account) => account.isSigner && !account.pubkey.equals(wallet) && !account.pubkey.equals(expectedPayer));
    if (unexpectedSigner) {
      throw new Error(
        `MetaMatcha transaction requires unexpected signer ${unexpectedSigner.pubkey.toBase58()}`,
      );
    }
    const computeBudgetInstructions = decompiled.instructions.filter(
      (instruction) => instruction.programId.equals(ComputeBudgetProgram.programId),
    );
    const withoutComputeBudget = decompiled.instructions.filter(
      (instruction) => !instruction.programId.equals(ComputeBudgetProgram.programId),
    );
    const ataCreates = withoutComputeBudget.filter(
      (instruction) =>
        instruction.programId.equals(ASSOCIATED_TOKEN_PROGRAM_ID) &&
        instruction.data.length === 1 &&
        instruction.data[0] === 1 &&
        instruction.keys.length >= 2,
    );
    const ataInfos = ataCreates.length
      ? await connection.getMultipleAccountsInfo(
          ataCreates.map((instruction) => instruction.keys[1].pubkey),
        )
      : [];
    const existingAtaCreates = new Set(
      ataCreates.filter((_instruction, index) => ataInfos[index] !== null),
    );
    let instructions = withoutComputeBudget.filter(
      (instruction) => !existingAtaCreates.has(instruction),
    );
    if (!instructions.length) {
      throw new Error("MetaMatcha transaction contained no swap instructions");
    }

    if (subKeypair && !subKeypair.publicKey.equals(wallet)) {
      const inMint = new PublicKey(quote.inputMint);
      const outMint = new PublicKey(quote.outputMint);
      const inProgram = intermediateTokenProgram(inMint);
      const outProgram = intermediateTokenProgram(outMint);

      const masterInAta = getAssociatedTokenAddressSync(inMint, wallet, false, inProgram);
      const subInAta = getAssociatedTokenAddressSync(inMint, subKeypair.publicKey, false, inProgram);
      const masterOutAta = getAssociatedTokenAddressSync(outMint, wallet, false, outProgram);
      const subOutAta = getAssociatedTokenAddressSync(outMint, subKeypair.publicKey, false, outProgram);

      const inAmountRaw = BigInt(quote.inAmount);
      const outAmountRaw = BigInt(quote.otherAmountThreshold || quote.outAmount);

      // An unreadable account is treated as missing: the idempotent create is harmless.
      const [subInAtaExists, masterOutAtaExists] = await connection
        .getMultipleAccountsInfo([subInAta, masterOutAta])
        .then((infos) => infos.map((info) => info !== null))
        .catch(() => [false, false]);

      const preTransfer: TransactionInstruction[] = [];
      if (!subInAtaExists) {
        preTransfer.push(
          createAssociatedTokenAccountIdempotentInstruction(wallet, subInAta, subKeypair.publicKey, inMint, inProgram),
        );
      }
      preTransfer.push(
        createTransferCheckedInstruction(masterInAta, inMint, subInAta, wallet, inAmountRaw, 6, [], inProgram),
      );

      const postTransfer: TransactionInstruction[] = [];
      if (!masterOutAtaExists) {
        postTransfer.push(
          createAssociatedTokenAccountIdempotentInstruction(wallet, masterOutAta, wallet, outMint, outProgram),
        );
      }
      postTransfer.push(
        createTransferCheckedInstruction(subOutAta, outMint, masterOutAta, subKeypair.publicKey, outAmountRaw, 6, [], outProgram),
      );
      instructions = [...preTransfer, ...instructions, ...postTransfer];
    }

    console.log(
      `MetaMatcha selected ${quote.aggregator ?? "unknown aggregator"}: ` +
      `${Buffer.from(quote.serializedTransaction, "base64").length} quote bytes, ` +
      `${instructions.length} swap instructions, ${lookupTables.length} lookup tables` +
      `${existingAtaCreates.size ? `, stripped ${existingAtaCreates.size} existing ATA setup instructions` : ""}` +
      `${subKeypair && !subKeypair.publicKey.equals(wallet) ? " (linked to sub-account taker)" : ""}`,
    );
    return {
      instructions,
      lookupTables,
      ignoredComputeBudgetInstructionCount: computeBudgetInstructions.length,
    };
  }

  if (config.dexProvider === "dflow" || quote.provider === "DFlow") {
    const result = await fetchJson<JupiterSwapInstructions>(
      `${config.dflowApiBase}/swap-instructions`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
        },
        body: JSON.stringify({
          userPublicKey: wallet.toBase58(),
          payer: wallet.toBase58(),
          quoteResponse: quote,
        }),
      },
      config,
      "DFlow swap-instructions",
    );
    if (result.error) throw new Error(`DFlow swap-instructions error: ${result.error}`);
    if (!result.swapInstruction) throw new Error("DFlow omitted swapInstruction");

    const cleanup = result.cleanupInstruction
      ? [result.cleanupInstruction]
      : (Array.isArray(result.cleanupInstructions) ? result.cleanupInstructions : []);
    const instructionJson = [
      ...(result.setupInstructions ?? []),
      ...(result.otherInstructions ?? []),
      result.swapInstruction,
      ...cleanup,
    ];
    return {
      instructions: instructionJson.map(decodeJupiterInstruction),
      lookupTables: await fetchLookupTables(
        connection,
        (result.addressLookupTableAddresses ?? []).map(
          (address) => new PublicKey(address),
        ),
      ),
      ignoredComputeBudgetInstructionCount:
        result.computeBudgetInstructions?.length ?? 0,
    };
  }

  const result = await fetchJson<JupiterSwapInstructions>(
    `${config.jupiterApiBase}/swap-instructions`,
    {
      method: "POST",
      headers: {
        ...jupiterHeaders(config, requestNumber),
        "content-type": "application/json",
      },
      body: JSON.stringify({
        userPublicKey: wallet.toBase58(),
        payer: wallet.toBase58(),
        quoteResponse: quote,
        wrapAndUnwrapSol: false,
        // The wallet already has every route ATA. Reusing those accounts keeps
        // PYUSD/USDG flash-loan transactions smaller than Jupiter's shared
        // intermediate-account form, which can exceed Solana's wire limit.
        useSharedAccounts: false,
        dynamicComputeUnitLimit: false,
        skipUserAccountsRpcCalls: false,
      }),
    },
    config,
    "Jupiter swap-instructions",
  );
  if (result.error) throw new Error(`Jupiter swap-instructions error: ${result.error}`);
  if (!result.swapInstruction) throw new Error("Jupiter omitted swapInstruction");

  const instructionJson = [
    ...(result.setupInstructions ?? []),
    ...(result.otherInstructions ?? []),
    result.swapInstruction,
    ...(result.cleanupInstruction ? [result.cleanupInstruction] : []),
  ];
  return {
    instructions: instructionJson.map(decodeJupiterInstruction),
    lookupTables: await fetchLookupTables(
      connection,
      (result.addressLookupTableAddresses ?? []).map(
        (address) => new PublicKey(address),
      ),
    ),
    ignoredComputeBudgetInstructionCount:
      result.computeBudgetInstructions?.length ?? 0,
  };
}

async function fetchLookupTables(
  connection: Connection,
  addresses: PublicKey[],
): Promise<AddressLookupTableAccount[]> {
  const unique = [
    ...new Map(
      addresses.map((address) => [address.toBase58(), address]),
    ).values(),
  ];
  const responses = await Promise.all(
    unique.map(async (address) => ({
      address,
      result: await connection.getAddressLookupTable(address),
    })),
  );
  return responses.map(({ address, result }) => {
    if (!result.value) {
      throw new Error(
        `Address lookup table not found: ${address.toBase58()}`,
      );
    }
    return result.value;
  });
}

function mergeLookupTables(
  ...groups: AddressLookupTableAccount[][]
): AddressLookupTableAccount[] {
  return [
    ...new Map(
      groups.flat().map((table) => [table.key.toBase58(), table]),
    ).values(),
  ];
}

async function selectMarginfiAccount(
  client: Project0Client,
  authority: PublicKey,
  configured?: PublicKey,
): Promise<MarginfiAccountWrapper> {
  const addresses = await client.getAccountAddresses(authority);
  if (configured) {
    const address = addresses.find((candidate) => candidate.equals(configured));
    if (!address) {
      throw new Error(
        `SOL_FLASH_ARB_MARGINFI_ACCOUNT ${configured.toBase58()} is not owned by this wallet`,
      );
    }
    return client.fetchAccount(address, true);
  }
  if (addresses.length === 0) {
    throw new Error(
      "No Marginfi account exists for this wallet. Run the guarded --create-marginfi-account command once.",
    );
  }
  if (addresses.length > 1) {
    throw new Error(
      `Found ${addresses.length} Marginfi accounts; set SOL_FLASH_ARB_MARGINFI_ACCOUNT in .env`,
    );
  }
  return client.fetchAccount(addresses[0], true);
}

export function assertNoExistingLoanLiability(
  account: MarginfiAccountWrapper,
  bankAddress: PublicKey,
  loanSymbol: string,
): void {
  const balance = account.balances.find((item) =>
    item.bankPk.equals(bankAddress),
  );
  if (balance?.active && !balance.liabilityShares.isZero()) {
    throw new Error(
      `Selected Marginfi account already has a ${loanSymbol} liability. Use a dedicated empty account so repay-all cannot consume unrelated debt.`,
    );
  }
}

async function buildFlashTransaction(
  account: MarginfiAccountWrapper,
  keypair: Keypair,
  bankAddress: PublicKey,
  loanAmountRaw: bigint,
  swapInstructions: TransactionInstruction[],
  lookupTables: AddressLookupTableAccount[],
  blockhash: string,
  computeUnitLimit: number,
  computeUnitPriceMicroLamports: number,
  additionalSigners: Keypair[] = [],
): Promise<VersionedTransaction> {
  const uiAmount = formatRaw(loanAmountRaw, TOKEN_DECIMALS);
  const borrow = await account.makeBorrowIx(bankAddress, uiAmount, {
    createAtas: false,
    wrapAndUnwrapSol: false,
    overrideInferAccounts: {
      authority: keypair.publicKey,
      group: account.group,
    },
  });
  const repay = await account.makeRepayIx(bankAddress, uiAmount, true, {
    wrapAndUnwrapSol: false,
    overrideInferAccounts: {
      authority: keypair.publicKey,
      group: account.group,
    },
  });

  const client = account.getClient();
  const program = client.program;
  const bank = client.bankMap.get(bankAddress.toBase58());

  const innerIxs: TransactionInstruction[] = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: computeUnitLimit }),
    ComputeBudgetProgram.setComputeUnitPrice({
      microLamports: computeUnitPriceMicroLamports,
    }),
    ...borrow.instructions,
    ...swapInstructions,
    ...repay.instructions,
  ];

  const endIndex = innerIxs.length + 1;

  const beginFlashLoanIx = await (program.methods as any)
    .lendingAccountStartFlashloan(new BN(endIndex))
    .accounts({
      marginfiAccount: account.address,
    })
    .accountsPartial({
      authority: keypair.publicKey,
      group: account.group,
      ixsSysvar: new PublicKey("Sysvar1nstructions1111111111111111111111111"),
    })
    .instruction();

  const healthAccounts: { pubkey: PublicKey; isSigner: boolean; isWritable: boolean }[] = [];
  if (bank) {
    healthAccounts.push({
      pubkey: bank.address,
      isSigner: false,
      isWritable: false,
    });
    healthAccounts.push({
      pubkey: bank.oracleKey,
      isSigner: false,
      isWritable: false,
    });
  }

  const endFlashLoanIx = new TransactionInstruction({
    programId: program.programId,
    data: Buffer.from([105, 124, 201, 106, 153, 2, 8, 156]), // lending_account_end_flashloan
    keys: [
      {
        pubkey: account.address,
        isSigner: false,
        isWritable: true,
      },
      {
        pubkey: account.group,
        isSigner: false,
        isWritable: false,
      },
      {
        pubkey: keypair.publicKey,
        isSigner: true,
        isWritable: false,
      },
      ...healthAccounts,
    ],
  });

  try {
    const message = new TransactionMessage({
      payerKey: keypair.publicKey,
      recentBlockhash: blockhash,
      instructions: [beginFlashLoanIx, ...innerIxs, endFlashLoanIx],
    }).compileToV0Message(lookupTables);

    const transaction = new VersionedTransaction(message);
    const requiredSigners = message.staticAccountKeys.slice(0, message.header.numRequiredSignatures);
    const signers = [keypair, ...additionalSigners].filter((s) =>
      requiredSigners.some((req) => req.equals(s.publicKey)),
    );
    transaction.sign(signers);
    return transaction;
  } catch (err: unknown) {
    const msg = errorMessage(err);
    if (msg.includes("encoding overruns")) {
      throw new Error(
        "Atomic transaction exceeds Solana 1232-byte size limit (encoding overruns Uint8Array)",
      );
    }
    throw err;
  }
}

export async function buildKaminoFlashTransaction(
  keypair: Keypair,
  loanMint: PublicKey,
  loanAmountRaw: bigint,
  swapInstructions: TransactionInstruction[],
  lookupTables: AddressLookupTableAccount[],
  blockhash: string,
  computeUnitLimit: number,
  computeUnitPriceMicroLamports: number,
  additionalSigners: Keypair[] = [],
): Promise<VersionedTransaction> {
  const reserveInfo = getKaminoReserveInfo(loanMint);
  const userAta = getAssociatedTokenAddressSync(
    loanMint,
    keypair.publicKey,
    false,
    reserveInfo.tokenProgram,
  );

  const borrowData = Buffer.alloc(16);
  Buffer.from([135, 231, 52, 167, 7, 52, 212, 193]).copy(borrowData, 0);
  borrowData.writeBigUInt64LE(loanAmountRaw, 8);

  const borrowIx = new TransactionInstruction({
    programId: KAMINO_PROGRAM_ID,
    data: borrowData,
    keys: [
      { pubkey: keypair.publicKey, isSigner: true, isWritable: true },
      { pubkey: KAMINO_MAIN_MARKET_AUTHORITY, isSigner: false, isWritable: false },
      { pubkey: KAMINO_MAIN_MARKET, isSigner: false, isWritable: false },
      { pubkey: reserveInfo.reserve, isSigner: false, isWritable: true },
      { pubkey: loanMint, isSigner: false, isWritable: false },
      { pubkey: reserveInfo.supplyVault, isSigner: false, isWritable: true },
      { pubkey: userAta, isSigner: false, isWritable: true },
      { pubkey: reserveInfo.feeVault, isSigner: false, isWritable: true },
      { pubkey: KAMINO_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: KAMINO_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: SYSVAR_INSTRUCTIONS, isSigner: false, isWritable: false },
      { pubkey: reserveInfo.tokenProgram, isSigner: false, isWritable: false },
    ],
  });

  const computeBudgetIxs: TransactionInstruction[] = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: computeUnitLimit }),
    ComputeBudgetProgram.setComputeUnitPrice({
      microLamports: computeUnitPriceMicroLamports,
    }),
  ];

  const borrowIxIndex = computeBudgetIxs.length;
  const kaminoFee = kaminoFlashLoanFeeRaw(loanAmountRaw, loanMint);
  const repayAmountRaw = loanAmountRaw + kaminoFee;

  const repayData = Buffer.alloc(17);
  Buffer.from([185, 117, 0, 203, 96, 245, 180, 186]).copy(repayData, 0);
  repayData.writeBigUInt64LE(repayAmountRaw, 8);
  repayData.writeUInt8(borrowIxIndex, 16);

  const repayIx = new TransactionInstruction({
    programId: KAMINO_PROGRAM_ID,
    data: repayData,
    keys: [
      { pubkey: keypair.publicKey, isSigner: true, isWritable: true },
      { pubkey: KAMINO_MAIN_MARKET_AUTHORITY, isSigner: false, isWritable: false },
      { pubkey: KAMINO_MAIN_MARKET, isSigner: false, isWritable: false },
      { pubkey: reserveInfo.reserve, isSigner: false, isWritable: true },
      { pubkey: loanMint, isSigner: false, isWritable: false },
      { pubkey: reserveInfo.supplyVault, isSigner: false, isWritable: true },
      { pubkey: userAta, isSigner: false, isWritable: true },
      { pubkey: reserveInfo.feeVault, isSigner: false, isWritable: true },
      { pubkey: KAMINO_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: KAMINO_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: SYSVAR_INSTRUCTIONS, isSigner: false, isWritable: false },
      { pubkey: reserveInfo.tokenProgram, isSigner: false, isWritable: false },
    ],
  });

  const instructions: TransactionInstruction[] = [
    ...computeBudgetIxs,
    borrowIx,
    ...swapInstructions,
    repayIx,
  ];

  try {
    const message = new TransactionMessage({
      payerKey: keypair.publicKey,
      recentBlockhash: blockhash,
      instructions,
    }).compileToV0Message(lookupTables);

    const transaction = new VersionedTransaction(message);
    const requiredSigners = message.staticAccountKeys.slice(
      0,
      message.header.numRequiredSignatures,
    );
    const signers = [keypair, ...additionalSigners].filter((s) =>
      requiredSigners.some((req) => req.equals(s.publicKey)),
    );
    transaction.sign(signers);
    return transaction;
  } catch (err: unknown) {
    const msg = errorMessage(err);
    if (msg.includes("encoding overruns")) {
      throw new Error(
        "Atomic transaction exceeds Solana 1232-byte size limit (encoding overruns Uint8Array)",
      );
    }
    throw err;
  }
}

export async function buildFlashLoanTransaction(
  provider: "marginfi" | "kamino",
  account: MarginfiAccountWrapper | undefined,
  keypair: Keypair,
  loanMint: PublicKey,
  bankAddress: PublicKey | undefined,
  loanAmountRaw: bigint,
  swapInstructions: TransactionInstruction[],
  lookupTables: AddressLookupTableAccount[],
  blockhash: string,
  computeUnitLimit: number,
  computeUnitPriceMicroLamports: number,
  additionalSigners: Keypair[] = [],
): Promise<VersionedTransaction> {
  if (provider === "kamino") {
    return buildKaminoFlashTransaction(
      keypair,
      loanMint,
      loanAmountRaw,
      swapInstructions,
      lookupTables,
      blockhash,
      computeUnitLimit,
      computeUnitPriceMicroLamports,
      additionalSigners,
    );
  }
  if (!account || !bankAddress) {
    throw new Error(
      "Marginfi account and bank address are required for Marginfi flash loans",
    );
  }
  return buildFlashTransaction(
    account,
    keypair,
    bankAddress,
    loanAmountRaw,
    swapInstructions,
    lookupTables,
    blockhash,
    computeUnitLimit,
    computeUnitPriceMicroLamports,
    additionalSigners,
  );
}

export async function buildWalletFundedTransaction(
  keypair: Keypair,
  instructions: TransactionInstruction[],
  lookupTables: AddressLookupTableAccount[],
  blockhash: string,
  computeUnitLimit: number,
  computeUnitPriceMicroLamports: number,
  additionalSigners: Keypair[] = [],
): Promise<VersionedTransaction> {
  try {
    const message = new TransactionMessage({
      payerKey: keypair.publicKey,
      recentBlockhash: blockhash,
      instructions: [
        ComputeBudgetProgram.setComputeUnitLimit({ units: computeUnitLimit }),
        ComputeBudgetProgram.setComputeUnitPrice({
          microLamports: computeUnitPriceMicroLamports,
        }),
        ...instructions,
      ],
    }).compileToV0Message(lookupTables);

    const transaction = new VersionedTransaction(message);
    const requiredSigners = message.staticAccountKeys.slice(0, message.header.numRequiredSignatures);
    const signers = [keypair, ...additionalSigners].filter((s) =>
      requiredSigners.some((req) => req.equals(s.publicKey)),
    );
    transaction.sign(signers);
    return transaction;
  } catch (error) {
    if (errorMessage(error).includes("encoding overruns")) {
      throw new Error(
        "Atomic transaction exceeds Solana 1232-byte size limit (encoding overruns Uint8Array)",
      );
    }
    throw error;
  }
}

function simulationFailure(error: unknown, logs?: string[] | null): Error {
  const relevantLogs = (logs ?? []).slice(-18).join("\n");
  return new Error(
    `Atomic simulation reverted: ${JSON.stringify(error)}${relevantLogs ? `\n${relevantLogs}` : ""}`,
  );
}

async function simulate(
  connection: Connection,
  transaction: VersionedTransaction,
): Promise<number> {
  const result = await connection.simulateTransaction(transaction, {
    commitment: "processed",
    sigVerify: true,
  });
  if (result.value.err) throw simulationFailure(result.value.err, result.value.logs);
  if (!result.value.unitsConsumed) {
    throw new Error("Atomic simulation succeeded but did not report unitsConsumed");
  }
  return result.value.unitsConsumed;
}

export interface ExecutableCandidate {
  candidate: JupiterQuote;
  aggregatorName: string;
  grossProfitRaw: bigint;
  cycle: ConservativeCycle;
  swapInstructions: TransactionInstruction[];
  lookupTables: AddressLookupTableAccount[];
  ignoredComputeBudgetCount: number;
  stableLeg: StableLeg;
  probe: VersionedTransaction;
  probeUnits: number;
  activeFlashProvider?: "marginfi" | "kamino";
}

export interface CandidateEvaluationResult {
  candidate: JupiterQuote;
  aggregatorName: string;
  grossProfitRaw: bigint;
  executable?: ExecutableCandidate;
  error?: string;
  isPoisoned?: boolean;
}

export async function prerunCandidateQuotes(
  config: Config,
  connection: Connection,
  walletAddress: PublicKey,
  account: MarginfiAccountWrapper | undefined,
  loanBank: Bank | undefined,
  loanAmountRaw: bigint,
  isWalletFunded: boolean,
  firstQuote: JupiterQuote,
  stableQuote: StableQuote,
  effectiveMinGrossRaw: bigint,
  clientLookupTables: AddressLookupTableAccount[],
  customLookupTables: AddressLookupTableAccount[],
  blockhash: string,
  simulateFn: (
    connection: Connection,
    transaction: VersionedTransaction,
  ) => Promise<number> = simulate,
  existingStableLeg?: StableLeg,
  flashProvider: "marginfi" | "kamino" = config.provider === "kamino" ? "kamino" : "marginfi",
  estimateStableOutputRaw?: (inputRaw: bigint) => bigint,
): Promise<ExecutableCandidate> {
  const candidates: JupiterQuote[] = firstQuote.candidates?.length
    ? firstQuote.candidates
    : [firstQuote];

  let results: CandidateEvaluationResult[];

  if (config.swapOrder === "stable-first") {
    const stableLeg =
      existingStableLeg ??
      (await getStableLeg(config, walletAddress, stableQuote));
    results = await Promise.all(
      candidates.map(async (cand, index): Promise<CandidateEvaluationResult> => {
        const aggregatorName =
          cand.aggregator ?? cand.provider ?? `Aggregator #${index + 1}`;
        const candOutputRaw = BigInt(cand.otherAmountThreshold);
        const candGrossProfitRaw = candOutputRaw - loanAmountRaw;
        if (candGrossProfitRaw < effectiveMinGrossRaw) {
          return {
            candidate: cand,
            aggregatorName,
            grossProfitRaw: candGrossProfitRaw,
            error: `gross profit ${formatRaw(candGrossProfitRaw)} ${config.loanSymbol} is below scaled minimum ${formatRaw(effectiveMinGrossRaw)} ${config.loanSymbol}`,
            isPoisoned: false,
          };
        }
        try {
          const swapLeg = await getSwapLeg(
            config,
            cand,
            walletAddress,
            index + 1,
            connection,
          );
          const lookupTables = mergeLookupTables(
            customLookupTables,
            clientLookupTables,
            swapLeg.lookupTables,
          );
          const swapInstructions = [
            stableLeg.instruction,
            ...swapLeg.instructions,
          ];
          const additionalSigners = config.subKeypair ? [config.subKeypair] : [];
          let probe = isWalletFunded
            ? await buildWalletFundedTransaction(
                config.keypair,
                swapInstructions,
                lookupTables,
                blockhash,
                config.probeComputeUnitLimit,
                0,
                additionalSigners,
              )
            : await buildFlashLoanTransaction(
                flashProvider,
                account,
                config.keypair,
                config.loanMint,
                loanBank?.address,
                loanAmountRaw,
                swapInstructions,
                lookupTables,
                blockhash,
                config.probeComputeUnitLimit,
                0,
                additionalSigners,
              );
          let probeWireSize = probe.serialize().length;
          if (probeWireSize > MAX_WIRE_TRANSACTION_BYTES) {
            return {
              candidate: cand,
              aggregatorName,
              grossProfitRaw: candGrossProfitRaw,
              error: `transaction size ${probeWireSize} bytes exceeds Solana 1232-byte limit`,
              isPoisoned: false,
            };
          }
          let probeUnits: number;
          let candidateProvider = flashProvider;
          try {
            probeUnits = await simulateFn(connection, probe);
          } catch (simError) {
            if (
              !isWalletFunded &&
              flashProvider === "marginfi" &&
              config.provider === "auto" &&
              isMarginfiBorrowError(simError)
            ) {
              console.log(
                `  - ${aggregatorName}: Marginfi candidate simulation failed (${errorMessage(simError)}). Retrying candidate immediately with Kamino Lending fallback...`,
              );
              const kaminoProbe = await buildFlashLoanTransaction(
                "kamino",
                account,
                config.keypair,
                config.loanMint,
                loanBank?.address,
                loanAmountRaw,
                swapInstructions,
                lookupTables,
                blockhash,
                config.probeComputeUnitLimit,
                0,
                additionalSigners,
              );
              const kaminoWireSize = kaminoProbe.serialize().length;
              if (kaminoWireSize > MAX_WIRE_TRANSACTION_BYTES) {
                return {
                  candidate: cand,
                  aggregatorName,
                  grossProfitRaw: candGrossProfitRaw,
                  error: `transaction size ${kaminoWireSize} bytes exceeds Solana 1232-byte limit (Kamino fallback)`,
                  isPoisoned: false,
                };
              }
              probeUnits = await simulateFn(connection, kaminoProbe);
              probe = kaminoProbe;
              candidateProvider = "kamino";
            } else {
              throw simError;
            }
          }
          const cycle: ConservativeCycle = {
            firstLegMinimumRaw: stableQuote.outputRaw,
            secondLegMinimumRaw: candOutputRaw,
            grossProfitRaw: candGrossProfitRaw,
          };
          return {
            candidate: cand,
            aggregatorName,
            grossProfitRaw: candGrossProfitRaw,
            executable: {
              candidate: cand,
              aggregatorName,
              grossProfitRaw: candGrossProfitRaw,
              cycle,
              swapInstructions,
              lookupTables,
              ignoredComputeBudgetCount:
                swapLeg.ignoredComputeBudgetInstructionCount,
              stableLeg,
              probe,
              probeUnits,
              activeFlashProvider: candidateProvider,
            },
          };
        } catch (error) {
          return {
            candidate: cand,
            aggregatorName,
            grossProfitRaw: candGrossProfitRaw,
            error: errorMessage(error),
            isPoisoned: true,
          };
        }
      }),
    );
  } else {
    const stableLegCache = new Map<string, StableLeg>();
    if (existingStableLeg) {
      stableLegCache.set(`${stableQuote.inputRaw}_${stableQuote.outputRaw}`, existingStableLeg);
    }
    results = await Promise.all(
      candidates.map(async (cand, index): Promise<CandidateEvaluationResult> => {
        const aggregatorName =
          cand.aggregator ?? cand.provider ?? `Aggregator #${index + 1}`;
        const candFirstLegMinimumRaw = BigInt(cand.otherAmountThreshold);
        // A candidate that cannot clear the floor even on the on-chain Stable.com
        // estimate needs no Stable.com quote of its own.
        if (estimateStableOutputRaw && candFirstLegMinimumRaw !== stableQuote.inputRaw) {
          const estimatedGrossRaw = estimateStableOutputRaw(candFirstLegMinimumRaw) - loanAmountRaw;
          if (estimatedGrossRaw < effectiveMinGrossRaw) {
            return {
              candidate: cand,
              aggregatorName,
              grossProfitRaw: estimatedGrossRaw,
              error: `estimated gross profit ${formatRaw(estimatedGrossRaw)} ${config.loanSymbol} is below scaled minimum ${formatRaw(effectiveMinGrossRaw)} ${config.loanSymbol}`,
              isPoisoned: false,
            };
          }
        }
        try {
          const candStableQuote =
            candFirstLegMinimumRaw === stableQuote.inputRaw
              ? stableQuote
              : await getStableQuote(
                  config,
                  walletAddress,
                  candFirstLegMinimumRaw,
                );
          const candGrossProfitRaw = candStableQuote.outputRaw - loanAmountRaw;
          if (candGrossProfitRaw < effectiveMinGrossRaw) {
            return {
              candidate: cand,
              aggregatorName,
              grossProfitRaw: candGrossProfitRaw,
              error: `gross profit ${formatRaw(candGrossProfitRaw)} ${config.loanSymbol} is below scaled minimum ${formatRaw(effectiveMinGrossRaw)} ${config.loanSymbol}`,
              isPoisoned: false,
            };
          }
          const cacheKey = `${candStableQuote.inputRaw}_${candStableQuote.outputRaw}`;
          let candStableLeg = stableLegCache.get(cacheKey);
          if (!candStableLeg) {
            candStableLeg = await getStableLeg(
              config,
              walletAddress,
              candStableQuote,
            );
            stableLegCache.set(cacheKey, candStableLeg);
          }
          const swapLeg = await getSwapLeg(
            config,
            cand,
            walletAddress,
            index + 1,
            connection,
          );
          const lookupTables = mergeLookupTables(
            customLookupTables,
            clientLookupTables,
            swapLeg.lookupTables,
          );
          const swapInstructions = [
            ...swapLeg.instructions,
            candStableLeg.instruction,
          ];
          const additionalSigners = config.subKeypair ? [config.subKeypair] : [];
          let probe = isWalletFunded
            ? await buildWalletFundedTransaction(
                config.keypair,
                swapInstructions,
                lookupTables,
                blockhash,
                config.probeComputeUnitLimit,
                0,
                additionalSigners,
              )
            : await buildFlashLoanTransaction(
                flashProvider,
                account,
                config.keypair,
                config.loanMint,
                loanBank?.address,
                loanAmountRaw,
                swapInstructions,
                lookupTables,
                blockhash,
                config.probeComputeUnitLimit,
                0,
                additionalSigners,
              );
          let probeWireSize = probe.serialize().length;
          if (probeWireSize > MAX_WIRE_TRANSACTION_BYTES) {
            return {
              candidate: cand,
              aggregatorName,
              grossProfitRaw: candGrossProfitRaw,
              error: `transaction size ${probeWireSize} bytes exceeds Solana 1232-byte limit`,
              isPoisoned: false,
            };
          }
          let probeUnits: number;
          let candidateProvider = flashProvider;
          try {
            probeUnits = await simulateFn(connection, probe);
          } catch (simError) {
            if (
              !isWalletFunded &&
              flashProvider === "marginfi" &&
              config.provider === "auto" &&
              isMarginfiBorrowError(simError)
            ) {
              console.log(
                `  - ${aggregatorName}: Marginfi candidate simulation failed (${errorMessage(simError)}). Retrying candidate immediately with Kamino Lending fallback...`,
              );
              const kaminoProbe = await buildFlashLoanTransaction(
                "kamino",
                account,
                config.keypair,
                config.loanMint,
                loanBank?.address,
                loanAmountRaw,
                swapInstructions,
                lookupTables,
                blockhash,
                config.probeComputeUnitLimit,
                0,
                additionalSigners,
              );
              const kaminoWireSize = kaminoProbe.serialize().length;
              if (kaminoWireSize > MAX_WIRE_TRANSACTION_BYTES) {
                return {
                  candidate: cand,
                  aggregatorName,
                  grossProfitRaw: candGrossProfitRaw,
                  error: `transaction size ${kaminoWireSize} bytes exceeds Solana 1232-byte limit (Kamino fallback)`,
                  isPoisoned: false,
                };
              }
              probeUnits = await simulateFn(connection, kaminoProbe);
              probe = kaminoProbe;
              candidateProvider = "kamino";
            } else {
              throw simError;
            }
          }
          const cycle: ConservativeCycle = {
            firstLegMinimumRaw: candFirstLegMinimumRaw,
            secondLegMinimumRaw: candStableQuote.outputRaw,
            grossProfitRaw: candGrossProfitRaw,
          };
          return {
            candidate: cand,
            aggregatorName,
            grossProfitRaw: candGrossProfitRaw,
            executable: {
              candidate: cand,
              aggregatorName,
              grossProfitRaw: candGrossProfitRaw,
              cycle,
              swapInstructions,
              lookupTables,
              ignoredComputeBudgetCount:
                swapLeg.ignoredComputeBudgetInstructionCount,
              stableLeg: candStableLeg,
              probe,
              probeUnits,
              activeFlashProvider: candidateProvider,
            },
          };
        } catch (error) {
          return {
            candidate: cand,
            aggregatorName,
            grossProfitRaw: 0n,
            error: errorMessage(error),
            isPoisoned: true,
          };
        }
      }),
    );
  }

  console.log(`[Atomic Prerun] Evaluated ${results.length} candidate quote(s):`);
  for (const res of results) {
    if (res.executable) {
      console.log(
        `  - ${res.aggregatorName}: PASSED (${res.executable.probeUnits.toLocaleString()} compute units, guaranteed gross +${formatRaw(res.grossProfitRaw)} ${config.loanSymbol})`,
      );
    } else if (res.isPoisoned) {
      console.log(
        `  - ${res.aggregatorName}: POISONED (${res.error})`,
      );
    } else {
      console.log(
        `  - ${res.aggregatorName}: REJECTED (${res.error})`,
      );
    }
  }

  const validCandidates = results
    .map((res) => res.executable)
    .filter((exec): exec is ExecutableCandidate => exec !== undefined);

  if (!validCandidates.length) {
    if (
      !isWalletFunded &&
      flashProvider === "marginfi" &&
      config.provider === "auto" &&
      results.some((res) => isMarginfiBorrowError(res.error))
    ) {
      console.log(
        "[Atomic Prerun Fallback] Marginfi candidate simulation failed (utilization or borrow restricted on-chain). Retrying with Kamino Lending fallback...",
      );
      return prerunCandidateQuotes(
        config,
        connection,
        walletAddress,
        account,
        loanBank,
        loanAmountRaw,
        isWalletFunded,
        firstQuote,
        stableQuote,
        effectiveMinGrossRaw,
        clientLookupTables,
        customLookupTables,
        blockhash,
        simulateFn,
        existingStableLeg,
        "kamino",
        estimateStableOutputRaw,
      );
    }

    const details = results
      .map((res) => `  - ${res.aggregatorName}: ${res.error ?? "failed"}`)
      .join("\n");
    throw new Error(
      `All candidate quotes failed atomic prerun simulation or fell below the profit floor:\n${details}`,
    );
  }

  validCandidates.sort((a, b) =>
    b.grossProfitRaw > a.grossProfitRaw
      ? 1
      : b.grossProfitRaw < a.grossProfitRaw
        ? -1
        : 0,
  );
  const winning = validCandidates[0];
  if (results.length > 1) {
    console.log(
      `[Atomic Prerun] Selected best executable quote: ${winning.aggregatorName} (guaranteed gross +${formatRaw(winning.grossProfitRaw)} ${config.loanSymbol})`,
    );
  }
  return winning;
}

async function fetchSolUsd(config: Config): Promise<number> {
  const result = await fetchJson<JsonRecord>(
    config.solUsdUrl,
    { headers: { accept: "application/json" } },
    config,
    "SOL/USD price",
  );
  const price = Number(result.price);
  if (!Number.isFinite(price) || price <= 0) {
    throw new Error("SOL/USD endpoint returned an invalid price");
  }
  return price;
}

export interface FundingSnapshot {
  walletLoanBalanceRaw: bigint;
  kaminoHeadroomRaw?: bigint;
  /** From a previously recorded Marginfi vault; used only to skip Marginfi. */
  cachedMarginfiHeadroomRaw?: bigint;
  /** Raw balance of the Stable.com pool vault that pays this route's Stable leg. */
  stablePoolVaultRaw?: bigint;
}

/**
 * Verifies the wallet token accounts and reads every balance needed to pick a
 * funding source in one getMultipleAccounts call (previously four to six calls).
 */
async function readFundingSnapshot(
  connection: Connection,
  owner: PublicKey,
  intermediateMint: PublicKey,
  intermediateSymbol: string,
  loanMint: PublicKey,
  loanSymbol: string,
  stableVault?: PublicKey,
): Promise<FundingSnapshot> {
  const loanAta = getAssociatedTokenAddressSync(
    loanMint,
    owner,
    false,
    intermediateTokenProgram(loanMint),
  );
  const interAta = getAssociatedTokenAddressSync(
    intermediateMint,
    owner,
    false,
    intermediateTokenProgram(intermediateMint),
  );
  const required: Array<[string, PublicKey]> = [
    [loanSymbol, loanAta],
    [intermediateSymbol, interAta],
  ];
  if (!loanMint.equals(USDC_MINT) && !intermediateMint.equals(USDC_MINT)) {
    required.push([
      "USDC",
      getAssociatedTokenAddressSync(USDC_MINT, owner, false, TOKEN_PROGRAM_ID),
    ]);
  }
  const kaminoVault = KAMINO_RESERVES[loanMint.toBase58()]?.supplyVault;
  const marginfiVault = cachedMarginfiVault(loanMint);

  const infos = await connection.getMultipleAccountsInfo(
    [
      ...required.map(([, address]) => address),
      ...(kaminoVault ? [kaminoVault] : []),
      ...(marginfiVault ? [marginfiVault] : []),
      ...(stableVault ? [stableVault] : []),
    ],
    "confirmed",
  );
  const missing = required
    .filter((_entry, index) => !infos[index])
    .map(([symbol, address]) => `${symbol} ATA ${address.toBase58()}`);
  if (missing.length) {
    throw new Error(
      `Create the wallet token accounts before using the flash loan: ${missing.join(", ")}`,
    );
  }
  let next = required.length;
  const vaultAmount = (present: boolean): bigint | undefined => {
    if (!present) return undefined;
    const info = infos[next++];
    return info ? tokenAccountAmountRaw(info.data) : undefined;
  };
  const headroom = (amount: bigint | undefined): bigint | undefined =>
    amount === undefined ? undefined : flashHeadroomRaw(amount);
  return {
    walletLoanBalanceRaw: tokenAccountAmountRaw(infos[0]!.data),
    kaminoHeadroomRaw: headroom(vaultAmount(Boolean(kaminoVault))),
    cachedMarginfiHeadroomRaw: headroom(vaultAmount(Boolean(marginfiVault))),
    stablePoolVaultRaw: vaultAmount(Boolean(stableVault)),
  };
}

async function assertMainnet(connection: Connection): Promise<void> {
  const genesisHash = await connection.getGenesisHash();
  if (genesisHash !== MAINNET_GENESIS_HASH) {
    throw new Error(`RPC is not Solana mainnet-beta (genesis hash ${genesisHash})`);
  }
}

async function createMarginfiAccount(
  config: Config,
  cli: CliOptions,
  connection: Connection,
): Promise<void> {
  if (!cli.send || cli.confirmation !== "CREATE_MARGINFI_ACCOUNT") {
    throw new Error(
      "Creating an on-chain account requires --send --confirm-mainnet CREATE_MARGINFI_ACCOUNT",
    );
  }
  await assertMainnet(connection);
  const client = await Project0Client.initialize(connection, getConfig("production"));
  let accountIndex = -1;
  let accountAddress: PublicKey | undefined;
  for (let candidate = 0; candidate <= 65_535; candidate += 1) {
    const [address] = deriveMarginfiAccount(
      client.program.programId,
      client.group.address,
      config.keypair.publicKey,
      candidate,
    );
    if (!(await connection.getAccountInfo(address, config.commitment))) {
      accountIndex = candidate;
      accountAddress = address;
      break;
    }
  }
  if (accountIndex < 0 || !accountAddress) {
    throw new Error("Could not find a free Marginfi account index");
  }
  const transaction = await client.createMarginfiAccountTx(
    config.keypair.publicKey,
    accountIndex,
  );
  if (!(transaction instanceof Transaction)) {
    throw new Error(
      "Marginfi account creation unexpectedly returned a versioned transaction",
    );
  }
  const latest = await connection.getLatestBlockhash(config.commitment);
  transaction.feePayer = config.keypair.publicKey;
  transaction.recentBlockhash = latest.blockhash;
  transaction.sign(config.keypair);
  const simulation = await connection.simulateTransaction(transaction);
  if (simulation.value.err) {
    throw simulationFailure(simulation.value.err, simulation.value.logs);
  }
  const signature = await connection.sendRawTransaction(transaction.serialize(), {
    skipPreflight: false,
    preflightCommitment: config.commitment,
    maxRetries: 2,
  });
  const confirmation = await connection.confirmTransaction(
    {
      signature,
      blockhash: latest.blockhash,
      lastValidBlockHeight: latest.lastValidBlockHeight,
    },
    config.commitment,
  );
  if (confirmation.value.err) {
    throw new Error(
      `Marginfi account creation failed: ${JSON.stringify(confirmation.value.err)}`,
    );
  }
  console.log(`Created Marginfi account: ${accountAddress.toBase58()}`);
  console.log(`Transaction: https://solscan.io/tx/${signature}`);
  console.log(
    `Add SOL_FLASH_ARB_MARGINFI_ACCOUNT=${accountAddress.toBase58()} to .env`,
  );
}

export function parseSolanaRpcEndpoints(
  primaryRpc?: string,
  extraFallbacks?: string[],
): string[] {
  const envFallbacks = (process.env.SOLANA_RPC_FALLBACKS || "")
    .split(",")
    .map((u) => u.trim())
    .filter(Boolean);
  const envUrls = (process.env.SOLANA_RPC_URL || "")
    .split(",")
    .map((u) => u.trim())
    .filter(Boolean);

  const safePublicFallbacks = [
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
    "https://api.mainnet.solana.com",
    // publicnode.com (Allnodes) intentionally excluded: blocks getProgramAccounts without auth
  ];

  const candidates: string[] = [];
  if (primaryRpc) {
    for (const u of primaryRpc.split(",").map((s) => s.trim())) {
      if (u && !candidates.includes(u)) candidates.push(u);
    }
  }
  if (extraFallbacks) {
    for (const u of extraFallbacks) {
      const trimmed = u.trim();
      if (trimmed && !candidates.includes(trimmed)) candidates.push(trimmed);
    }
  }
  for (const u of envFallbacks) {
    if (!candidates.includes(u)) candidates.push(u);
  }
  for (const u of envUrls) {
    if (!candidates.includes(u)) candidates.push(u);
  }
  for (const u of safePublicFallbacks) {
    if (!candidates.includes(u)) candidates.push(u);
  }
  return candidates;
}

export function wrapConnectionWithResilientRpc(
  connection: Connection,
  configuredFallbacks: string[] = [],
): Connection {
  const fallbackUrls = parseSolanaRpcEndpoints(
    connection.rpcEndpoint,
    configuredFallbacks,
  );

  const originalRpcRequest = (connection as any)._rpcRequest?.bind(connection);
  (connection as any)._rpcRequest = async (methodName: string, args: any[]) => {
    let lastError: any;
    for (let attempt = 0; attempt < fallbackUrls.length * 2; attempt += 1) {
      const url = fallbackUrls[attempt % fallbackUrls.length];
      try {
        const body = {
          jsonrpc: "2.0",
          id: `${attempt + 1}`,
          method: methodName,
          params: args,
        };
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 8000);
        try {
          const res = await fetch(url, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
              ),
            },
            body: JSON.stringify(body),
            signal: controller.signal,
          });
          if (res.status === 403 || res.status === 429 || res.status >= 500) {
            throw new Error(`HTTP ${res.status} on ${url}`);
          }
          const data = await res.json();
          // Treat JSON-RPC-level errors as retryable — public nodes return HTTP 200
          // with {error:{message:"Request blocked"}} or "Indexed requests require a
          // personal token" rather than an HTTP error code.
          if (data && typeof data === "object" && data.error) {
            // Invalid params (e.g. "could not find account") is the same
            // answer on every node; hand it to web3.js instead of retrying.
            if (data.error.code === -32602) return data;
            const msg = data.error.message ?? JSON.stringify(data.error);
            throw new Error(`RPC error from ${url} [${methodName}]: ${msg}`);
          }
          return data;
        } finally {
          clearTimeout(timer);
        }
      } catch (err) {
        lastError = err;
      }
      await new Promise((r) => setTimeout(r, 100));
    }
    if (originalRpcRequest) {
      return await originalRpcRequest(methodName, args);
    }
    throw lastError || new Error(`RPC request ${methodName} failed across all endpoints`);
  };

  (connection as any)._rpcBatchRequest = async (requests: any[]) => {
    let lastError: any;
    for (let attempt = 0; attempt < fallbackUrls.length * 2; attempt += 1) {
      const url = fallbackUrls[attempt % fallbackUrls.length];
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 12000);
        try {
          const results = await Promise.all(
            requests.map(async (req) => {
              const body = {
                jsonrpc: "2.0",
                id: Math.floor(Math.random() * 1e9),
                method: req.methodName,
                params: req.args,
              };
              const res = await fetch(url, {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                  ),
                },
                body: JSON.stringify(body),
                signal: controller.signal,
              });
              if (res.status === 403 || res.status === 429 || res.status >= 500) {
                throw new Error(`HTTP ${res.status} on ${url}`);
              }
              const batchData = await res.json();
              if (batchData && typeof batchData === "object" && batchData.error) {
                const msg = batchData.error.message ?? JSON.stringify(batchData.error);
                throw new Error(`RPC batch error from ${url} [${req.methodName}]: ${msg}`);
              }
              return batchData;
            }),
          );
          if (Array.isArray(results) && results.length === requests.length) {
            return results;
          }
        } finally {
          clearTimeout(timer);
        }
      } catch (err) {
        lastError = err;
      }
      await new Promise((r) => setTimeout(r, 150));
    }
    throw lastError || new Error("Failed to fetch batch requests after retries");
  };

  return connection;
}

export const wrapConnectionWithResilientBatchRequest = wrapConnectionWithResilientRpc;

export const JITO_BLOCK_ENGINES = [
  "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
  "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
  "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
  "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
  "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
];

export const JITO_TIP_ACCOUNTS = [
  "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
  "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
  "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
  "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
  "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
  "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
  "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
  "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
];

export async function broadcastJitoOrFallback(
  connection: Connection,
  transaction: VersionedTransaction,
  config: Config,
): Promise<{ signature: string; method: string }> {
  const enableJito = (process.env.SOL_ENABLE_JITO || "true").trim().toLowerCase() !== "false";
  const jitoEngineUrl = process.env.SOL_JITO_ENGINE_URL || JITO_BLOCK_ENGINES[0];

  if (enableJito) {
    try {
      const serialized = bs58.encode(transaction.serialize());
      const payload = JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "sendBundle",
        params: [[serialized]],
      });

      const response = await fetch(jitoEngineUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
      });

      if (response.ok) {
        const body = (await response.json()) as any;
        if (body.result) {
          const sig = bs58.encode(transaction.signatures[0]);
          console.log(`[Jito] Bundle successfully submitted to Block Engine: ${body.result}`);
          return { signature: sig, method: "jito" };
        }
        if (body.error) {
          console.warn(`[Jito] Bundle rejected: ${JSON.stringify(body.error)}`);
        }
      } else {
        console.warn(`[Jito] HTTP ${response.status} from Block Engine`);
      }
    } catch (err: any) {
      console.warn(`[Jito] Bundle submission failed (${err?.message}); falling back to standard RPC...`);
    }
  }

  // Fallback to existing standard RPCs
  console.log(`[Privacy RPC] Falling back to standard Solana RPC (${config.rpcUrl})...`);
  const signature = await connection.sendRawTransaction(transaction.serialize(), {
    skipPreflight: true,
    preflightCommitment: config.commitment,
  });
  return { signature, method: "standard-rpc" };
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  const cli = parseCli(argv);
  const config = readConfig(cli);
  if (config.provider === "solend") {
    throw new Error(
      "Solend flash loans are not implemented by this engine; use --provider marginfi, kamino, or auto",
    );
  }
  const connection = wrapConnectionWithResilientRpc(
    new Connection(config.rpcUrl, config.commitment),
    config.rpcFallbacks,
  );
  const walletAddress = config.keypair.publicKey;

  if (cli.createMarginfiAccount) {
    await createMarginfiAccount(config, cli, connection);
    return;
  }
  if (cli.setupSubAtas) {
    if (!config.subKeypair) {
      throw new Error("Sub-account taker is not enabled (SOL_FLASH_ARB_USE_SUB_TAKER is false)");
    }
    await ensureSubAccountAtas(connection, config.keypair, config.subKeypair);
    return;
  }
  if (cli.send && cli.confirmation !== "EXECUTE_SOLANA_FLASH_ARB") {
    throw new Error(
      "Live execution requires --send --confirm-mainnet EXECUTE_SOLANA_FLASH_ARB",
    );
  }

  console.log(`Wallet: ${walletAddress.toBase58()}`);
  console.log(
    `Maximum flash-loan principal: ${formatRaw(config.maximumLoanAmountRaw)} ${config.loanSymbol}`,
  );

  let activeProvider: "marginfi" | "kamino" = "marginfi";
  let availableBankLiquidityRaw: bigint | undefined;
  let client: Project0Client | undefined;
  let account: MarginfiAccountWrapper | undefined;
  let loanBank: Bank | undefined;
  let isWalletFunded = false;

  // Stable.com is queried only once a route looks profitable: until then its
  // capacity comes from the pool's on-chain vault and its order limits and fee
  // from the last real /swap/status response (see cachedStableTerms).
  const stableCapacitySymbol = stableInputSymbol(
    config.swapOrder,
    config.loanSymbol,
    config.intermediateSymbol,
  );
  const stablePayoutSymbol = stableOutputSymbol(
    config.swapOrder,
    config.loanSymbol,
    config.intermediateSymbol,
  );
  const stableVault = stablePoolVault(
    config.swapOrder === "stable-first" ? config.intermediateMint : config.loanMint,
  );
  const stableTerms = cachedStableTerms(config);
  let stablePoolVaultRaw: bigint | undefined;

  if (!cli.quoteOnly) {
    if (cli.send && config.subKeypair) {
      await ensureSubAccountAtas(connection, config.keypair, config.subKeypair);
    }
    const funding = await readFundingSnapshot(
      connection,
      walletAddress,
      config.intermediateMint,
      config.intermediateSymbol,
      config.loanMint,
      config.loanSymbol,
      stableTerms ? stableVault : undefined,
    );
    stablePoolVaultRaw = funding.stablePoolVaultRaw;
    const kaminoHeadroom = async (): Promise<bigint> =>
      funding.kaminoHeadroomRaw ?? getKaminoAvailableLiquidity(connection, config.loanMint);
    const useKamino = (headroomRaw: bigint, reason: string): void => {
      activeProvider = "kamino";
      availableBankLiquidityRaw = headroomRaw;
      console.log(`Flash loan provider: Kamino Lending (${reason})`);
      console.log(
        `Kamino ${config.loanSymbol} reserve: ${getKaminoReserveInfo(config.loanMint).reserve.toBase58()}`,
      );
      console.log(
        `Kamino liquid pool headroom: ${formatRaw(headroomRaw)} ${config.loanSymbol}`,
      );
    };
    const cachedMarginfiHeadroom = funding.cachedMarginfiHeadroomRaw;

    if (funding.walletLoanBalanceRaw >= 1_000_000_000n) {
      isWalletFunded = true;
      availableBankLiquidityRaw = funding.walletLoanBalanceRaw;
      console.log(
        `Wallet capital available: ${formatRaw(funding.walletLoanBalanceRaw)} ${config.loanSymbol} (Executing wallet-funded 2-hop route, 0 flash loan overhead)`,
      );
    } else if (config.provider === "kamino") {
      useKamino(await kaminoHeadroom(), "explicitly configured");
    } else if (
      config.provider === "auto" &&
      cachedMarginfiHeadroom !== undefined &&
      cachedMarginfiHeadroom < config.maximumLoanAmountRaw &&
      funding.kaminoHeadroomRaw !== undefined &&
      funding.kaminoHeadroomRaw > cachedMarginfiHeadroom
    ) {
      // Marginfi could only fund a smaller loan than Kamino, so skip its
      // client initialization (~20 RPC calls) for this check.
      console.log(
        `[Flash Loan] Marginfi vault headroom ${formatRaw(cachedMarginfiHeadroom)} ${config.loanSymbol} is below the ${formatRaw(config.maximumLoanAmountRaw)} ${config.loanSymbol} maximum; skipping Marginfi.`,
      );
      useKamino(funding.kaminoHeadroomRaw, "larger liquidity");
    } else {
      // Marginfi must always be the first option!
      console.log(`[Flash Loan] Inspecting Marginfi as primary flash loan provider...`);
      let isMarginfiHealthy = false;
      let marginfiUnavailableReason = "";
      try {
        client = await Project0Client.initialize(
          connection,
          getConfig("production"),
        );
        account = await selectMarginfiAccount(
          client,
          walletAddress,
          config.marginfiAccount,
        );
        const loanBanks = client.getBanksByMint(config.loanMint, AssetTag.DEFAULT);
        if (loanBanks.length !== 1) {
          throw new Error(
            `Expected exactly one standard Marginfi ${config.loanSymbol} bank; found ${loanBanks.length}`,
          );
        }
        loanBank = loanBanks[0];
        rememberMarginfiBank(config.loanMint, loanBank);
        assertNoExistingLoanLiability(account, loanBank.address, config.loanSymbol);

        const vaultBalance = await connection.getTokenAccountBalance(
          loanBank.liquidityVault,
        );
        const headroom = flashHeadroomRaw(BigInt(vaultBalance.value.amount));

        // Check on-chain bank utilization to ensure borrow won't fail with AnchorError 6026: IllegalUtilizationRatio
        const assets = loanBank.getTotalAssetQuantity();
        const liabilities = loanBank.getTotalLiabilityQuantity();
        const utilization = loanBank.computeUtilizationRate();
        const isOverUtilized =
          liabilities.gte(assets) ||
          utilization.gte(0.98) ||
          loanBank.totalLiabilityShares.gte(loanBank.totalAssetShares);

        if (isOverUtilized) {
          const utilPct = utilization.toNumber() * 100;
          marginfiUnavailableReason = `Marginfi ${config.loanSymbol} bank utilization is ${utilPct.toFixed(2)}% (AnchorError 6026: IllegalUtilizationRatio; borrows disabled on-chain)`;
        } else if (headroom < 1_000_000n) {
          marginfiUnavailableReason = `Marginfi ${config.loanSymbol} liquid pool headroom is exhausted (${formatRaw(headroom)} ${config.loanSymbol})`;
        } else {
          isMarginfiHealthy = true;
          activeProvider = "marginfi";
          availableBankLiquidityRaw = headroom;
          console.log(`Marginfi account: ${account.address.toBase58()}`);
          console.log(`Marginfi ${config.loanSymbol} bank: ${loanBank.address.toBase58()}`);
          console.log(
            `Marginfi liquid pool headroom: ${formatRaw(availableBankLiquidityRaw)} ${config.loanSymbol}`,
          );
        }

        // Marginfi stays first only while it can fund the full principal.
        // Otherwise its small vault would cap the loan, and a later
        // Marginfi-to-Kamino fallback would keep that reduced size.
        if (
          isMarginfiHealthy &&
          config.provider === "auto" &&
          headroom < config.maximumLoanAmountRaw
        ) {
          try {
            const kaminoHeadroomRaw = await kaminoHeadroom();
            if (kaminoHeadroomRaw > headroom) {
              console.log(
                `[Flash Loan] Marginfi headroom ${formatRaw(headroom)} ${config.loanSymbol} is below the ${formatRaw(config.maximumLoanAmountRaw)} ${config.loanSymbol} maximum.`,
              );
              useKamino(kaminoHeadroomRaw, "larger liquidity");
            }
          } catch (kaminoErr) {
            console.log(
              `[Flash Loan] Kamino headroom check failed (${errorMessage(kaminoErr)}); keeping Marginfi.`,
            );
          }
        }
      } catch (err) {
        marginfiUnavailableReason = errorMessage(err);
      }

      if (!isMarginfiHealthy) {
        if (config.provider === "marginfi") {
          throw new Error(
            `Marginfi flash loan is unavailable: ${marginfiUnavailableReason}`,
          );
        }
        console.log(`[Flash Loan] Marginfi check: ${marginfiUnavailableReason}`);
        console.log(
          `[Flash Loan Fallback] Marginfi is unavailable; falling back to Kamino Lending as secondary provider.`,
        );
        useKamino(await kaminoHeadroom(), "fallback");
      }
    }
  }

  if (stableTerms && stablePoolVaultRaw === undefined) {
    const [vaultInfo] = await connection.getMultipleAccountsInfo([stableVault]);
    stablePoolVaultRaw = vaultInfo ? tokenAccountAmountRaw(vaultInfo.data) : undefined;
  }
  const liveStableQuote: StableQuoter = async (inputRaw) => {
    const quote = await getStableQuote(config, walletAddress, inputRaw);
    rememberStableTerms(config, quote.status);
    return quote;
  };
  let quoteStable = liveStableQuote;
  let estimateStableOutputRaw: ((inputRaw: bigint) => bigint) | undefined;
  if (stableTerms && stablePoolVaultRaw !== undefined) {
    const reportedBalanceRaw = stableReportedBalanceRaw(stablePoolVaultRaw);
    const { minimumRaw: stableMinimumRaw } = stableCapacityRaw(
      reportedBalanceRaw,
      stableTerms.minimumRaw,
      stableTerms.maximumRaw,
      config.stableCapacityBufferRaw,
      stableCapacitySymbol,
    );
    // The sniper reads this wording as "cannot be funded" and checks the
    // equivalent route that borrows the other token instead.
    if (
      !isWalletFunded &&
      availableBankLiquidityRaw !== undefined &&
      availableBankLiquidityRaw < stableMinimumRaw
    ) {
      throw new Error(
        `Flash-loan liquidity is below the Stable.com minimum order: ${activeProvider} can lend ${formatRaw(availableBankLiquidityRaw)} ${config.loanSymbol}; at least ${formatRaw(stableMinimumRaw)} ${stableCapacitySymbol} is required`,
      );
    }
    console.log(
      `Stable.com ${stablePayoutSymbol} pool (on-chain): ${formatRaw(reportedBalanceRaw)} available; Stable.com is queried only if the route is profitable`,
    );
    quoteStable = async (inputRaw) =>
      estimatedStableQuote(inputRaw, reportedBalanceRaw, stableTerms, stablePayoutSymbol);
    estimateStableOutputRaw = (inputRaw) =>
      estimatedStableQuote(inputRaw, reportedBalanceRaw, stableTerms, stablePayoutSymbol).outputRaw;
  }

  /** Replace the on-chain estimate with Stable.com's own quote before any order exists. */
  async function confirmEstimatedStableLeg(estimated: SizedCycle): Promise<SizedCycle> {
    console.log("Estimated route clears the floor; confirming the Stable.com leg with Stable.com...");
    try {
      const live = await liveStableQuote(estimated.stableQuote.inputRaw);
      const capacity = stableCapacity(
        live.status,
        config.stableCapacityBufferRaw,
        stableCapacitySymbol,
      );
      if (
        live.outputRaw === estimated.stableQuote.outputRaw &&
        live.inputRaw <= capacity.usableCapacityRaw
      ) {
        console.log("Stable.com confirmed the on-chain estimate.");
        return {
          ...estimated,
          stableQuote: live,
          capacityRaw: capacity.capacityRaw,
          usableCapacityRaw: capacity.usableCapacityRaw,
        };
      }
      console.log(
        `Stable.com quoted ${formatRaw(live.outputRaw)} ${stablePayoutSymbol} against the ${formatRaw(estimated.stableQuote.outputRaw)} estimate; re-sizing with live Stable.com quotes...`,
      );
    } catch (error) {
      console.log(
        `Stable.com did not confirm the estimate (${errorMessage(error)}); re-sizing with live Stable.com quotes...`,
      );
    }
    return getCapacitySizedCycle(config, walletAddress, availableBankLiquidityRaw, liveStableQuote);
  }

  let sized = await getCapacitySizedCycle(
    config,
    walletAddress,
    availableBankLiquidityRaw,
    quoteStable,
  );
  if (sized.stableQuote.status.estimated) {
    const estimatedMinGrossRaw = effectiveScaledMinimumProfitRaw(
      config.minimumGrossProfitRaw,
      sized.loanAmountRaw,
      config.maximumLoanAmountRaw,
    );
    if (sized.cycle.grossProfitRaw >= estimatedMinGrossRaw) {
      sized = await confirmEstimatedStableLeg(sized);
    } else {
      console.log("Stable.com leg estimated from its on-chain pool; below the floor, so Stable.com was not queried.");
    }
  }

  let { loanAmountRaw, firstQuote, secondJupiterQuote, stableQuote, cycle } = sized;
  const isTwoHop = config.dexProvider === "jupiter" &&
    isTwoHopJupiterRoute(config.loanMint, config.intermediateMint);
  const dexName = dexProviderName(config.dexProvider);
  const routeFlow = config.swapOrder === "stable-first"
    ? `${config.loanSymbol} -> ${config.intermediateSymbol} (Stable.com) -> ${config.loanSymbol} (${dexName})`
    : `${config.loanSymbol} -> ${config.intermediateSymbol} (${dexName}) -> ${config.loanSymbol} (Stable.com)`;
  console.log(`Route: ${routeFlow}`);
  console.log(`Trading capital: ${formatRaw(loanAmountRaw)} ${config.loanSymbol}`);
  console.log(
    `Stable.com capacity: ${formatRaw(sized.capacityRaw)} ${stableInputSymbol(config.swapOrder, config.loanSymbol, config.intermediateSymbol)} (${formatRaw(sized.usableCapacityRaw)} usable)`,
  );
  if (config.swapOrder === "stable-first") {
    if (isTwoHop && secondJupiterQuote) {
      console.log(
        `Leg 1 (Stable.com): ${config.loanSymbol} -> ${config.intermediateSymbol} | executable output: ${formatRaw(cycle.firstLegMinimumRaw)} ${config.intermediateSymbol}`,
      );
      console.log(
        `Leg 2 (${dexName}): ${config.intermediateSymbol} -> USDC -> ${config.loanSymbol} | guaranteed minimum: ${formatRaw(cycle.secondLegMinimumRaw)} ${config.loanSymbol}`,
      );
    } else {
      console.log(
        `Leg 1 (Stable.com): ${config.loanSymbol} -> ${config.intermediateSymbol} | executable output: ${formatRaw(cycle.firstLegMinimumRaw)} ${config.intermediateSymbol}`,
      );
      console.log(
        `Leg 2 (${dexName}): ${config.intermediateSymbol} -> ${config.loanSymbol} | guaranteed minimum: ${formatRaw(cycle.secondLegMinimumRaw)} ${config.loanSymbol}`,
      );
    }
  } else if (isTwoHop && secondJupiterQuote) {
    console.log(
      `Leg 1 (${dexName}): ${config.loanSymbol} -> USDC -> ${config.intermediateSymbol} | guaranteed minimum: ${formatRaw(cycle.firstLegMinimumRaw)} ${config.intermediateSymbol}`,
    );
    console.log(
      `Leg 2 (Stable.com): ${config.intermediateSymbol} -> ${config.loanSymbol} | executable output: ${formatRaw(cycle.secondLegMinimumRaw)} ${config.loanSymbol}`,
    );
  } else {
    console.log(
      `Leg 1 (${dexName}): ${config.loanSymbol} -> ${config.intermediateSymbol} | guaranteed minimum: ${formatRaw(cycle.firstLegMinimumRaw)} ${config.intermediateSymbol}`,
    );
    console.log(
      `Leg 2 (Stable.com): ${config.intermediateSymbol} -> ${config.loanSymbol} | executable output: ${formatRaw(cycle.secondLegMinimumRaw)} ${config.loanSymbol}`,
    );
  }
  console.log(
    `Stable.com token fee: ${formatRaw(stableQuote.tokenFeeRaw)} ${stableOutputSymbol(config.swapOrder, config.loanSymbol, config.intermediateSymbol)}`,
  );
  const effectiveMinGrossRaw = effectiveScaledMinimumProfitRaw(
    config.minimumGrossProfitRaw,
    loanAmountRaw,
    config.maximumLoanAmountRaw,
  );
  const effectiveMinNetRaw = effectiveScaledMinimumProfitRaw(
    config.minimumNetProfitRaw,
    loanAmountRaw,
    config.maximumLoanAmountRaw,
  );

  console.log(`Guaranteed gross result: ${formatRaw(cycle.grossProfitRaw)} ${config.loanSymbol}`);
  if (loanAmountRaw < config.maximumLoanAmountRaw) {
    console.log(
      `Scaled minimum profit required: ${formatRaw(effectiveMinGrossRaw)} ${config.loanSymbol} (scaled proportionally from ${formatRaw(config.minimumGrossProfitRaw)} ${config.loanSymbol} base for ${formatRaw(loanAmountRaw)}/${formatRaw(config.maximumLoanAmountRaw)} principal)`,
    );
  }

  if (cycle.grossProfitRaw < effectiveMinGrossRaw) {
    throw new Error(
      `No executable opportunity: guaranteed gross ${formatRaw(cycle.grossProfitRaw)} ${config.loanSymbol} is below ${formatRaw(effectiveMinGrossRaw)} ${config.loanSymbol} (scaled minimum)`,
    );
  }
  if (cli.quoteOnly) {
    console.log("Quote-only mode: no transaction was built, signed, or sent.");
    return;
  }
  if (!isWalletFunded && activeProvider === "marginfi" && (!client || !account || !loanBank)) {
    throw new Error("Marginfi client or account not initialized");
  }

  let swapInstructions: TransactionInstruction[];
  let lookupTables: AddressLookupTableAccount[];
  let ignoredComputeBudgetCount = 0;
  let stableLeg: StableLeg;
  let probe: VersionedTransaction;
  let probeUnits: number;

  const customLookupTables = config.customLookupTableAddresses.length
    ? await fetchLookupTables(connection, config.customLookupTableAddresses)
    : [];
  const latest = await connection.getLatestBlockhash(config.commitment);

  if (isTwoHop && secondJupiterQuote) {
    const [firstSwap1, firstSwap2, stableLegResult] = await Promise.all([
      getSwapLeg(config, firstQuote, walletAddress, 1, connection),
      getSwapLeg(config, secondJupiterQuote, walletAddress, 2, connection),
      getStableLeg(config, walletAddress, stableQuote),
    ]);
    stableLeg = stableLegResult;
    lookupTables = mergeLookupTables(
      customLookupTables,
      client?.addressLookupTables ?? [],
      firstSwap1.lookupTables,
      firstSwap2.lookupTables,
    );
    swapInstructions = config.swapOrder === "stable-first"
      ? [stableLeg.instruction, ...firstSwap1.instructions, ...firstSwap2.instructions]
      : [...firstSwap1.instructions, ...firstSwap2.instructions, stableLeg.instruction];
    ignoredComputeBudgetCount =
      firstSwap1.ignoredComputeBudgetInstructionCount +
      firstSwap2.ignoredComputeBudgetInstructionCount;

    const additionalSigners = config.subKeypair ? [config.subKeypair] : [];
    probe = isWalletFunded
      ? await buildWalletFundedTransaction(
          config.keypair,
          swapInstructions,
          lookupTables,
          latest.blockhash,
          config.probeComputeUnitLimit,
          0,
          additionalSigners,
        )
      : await buildFlashLoanTransaction(
          activeProvider,
          account,
          config.keypair,
          config.loanMint,
          loanBank?.address,
          loanAmountRaw,
          swapInstructions,
          lookupTables,
          latest.blockhash,
          config.probeComputeUnitLimit,
          0,
          additionalSigners,
        );

    const probeWireSize = probe.serialize().length;
    if (probeWireSize > MAX_WIRE_TRANSACTION_BYTES) {
      const hint = config.dexProvider === "jupiter"
        ? " Lower SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS."
        : " Try a different SOL_FLASH_ARB_MATCHA_AGGREGATORS selection.";
      throw new Error(
        `Atomic transaction is ${probeWireSize} bytes before simulation; Solana maximum is ${MAX_WIRE_TRANSACTION_BYTES}.${hint}`,
      );
    }
    try {
      probeUnits = await simulate(connection, probe);
    } catch (err) {
      if (
        !isWalletFunded &&
        activeProvider === "marginfi" &&
        config.provider === "auto" &&
        isMarginfiBorrowError(err)
      ) {
        console.log(
          `[2-Hop Simulation Fallback] Marginfi simulation failed (${errorMessage(err)}). Retrying with Kamino Lending fallback...`,
        );
        activeProvider = "kamino";
        probe = await buildFlashLoanTransaction(
          activeProvider,
          account,
          config.keypair,
          config.loanMint,
          loanBank?.address,
          loanAmountRaw,
          swapInstructions,
          lookupTables,
          latest.blockhash,
          config.probeComputeUnitLimit,
          0,
          additionalSigners,
        );
        probeUnits = await simulate(connection, probe);
      } else {
        throw err;
      }
    }
  } else {
    const winning = await prerunCandidateQuotes(
      config,
      connection,
      walletAddress,
      account,
      loanBank,
      loanAmountRaw,
      isWalletFunded,
      firstQuote,
      stableQuote,
      effectiveMinGrossRaw,
      client?.addressLookupTables ?? [],
      customLookupTables,
      latest.blockhash,
      simulate,
      undefined,
      activeProvider,
      estimateStableOutputRaw,
    );
    if (winning.activeFlashProvider) {
      activeProvider = winning.activeFlashProvider;
    }
    firstQuote = winning.candidate;
    cycle = winning.cycle;
    swapInstructions = winning.swapInstructions;
    lookupTables = winning.lookupTables;
    ignoredComputeBudgetCount = winning.ignoredComputeBudgetCount;
    stableLeg = winning.stableLeg;
    probe = winning.probe;
    probeUnits = winning.probeUnits;
  }
  const computeUnitLimit = finalComputeUnitLimit(
    probeUnits,
    config.computeUnitSafetyBps,
    config.probeComputeUnitLimit,
  );
  const additionalSigners = config.subKeypair ? [config.subKeypair] : [];
  let transaction = isWalletFunded
    ? await buildWalletFundedTransaction(
        config.keypair,
        swapInstructions,
        lookupTables,
        latest.blockhash,
        computeUnitLimit,
        config.computeUnitPriceMicroLamports,
        additionalSigners,
      )
    : await buildFlashLoanTransaction(
        activeProvider,
        account,
        config.keypair,
        config.loanMint,
        loanBank?.address,
        loanAmountRaw,
        swapInstructions,
        lookupTables,
        latest.blockhash,
        computeUnitLimit,
        config.computeUnitPriceMicroLamports,
        additionalSigners,
      );
  const wireSize = transaction.serialize().length;
  if (wireSize > MAX_WIRE_TRANSACTION_BYTES) {
    const hint = config.dexProvider === "jupiter"
      ? " Reduce SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS or keep direct routes enabled."
      : " Try a different SOL_FLASH_ARB_MATCHA_AGGREGATORS selection.";
    throw new Error(
      `Atomic transaction is ${wireSize} bytes; Solana maximum is ${MAX_WIRE_TRANSACTION_BYTES}.${hint}`,
    );
  }
  let finalUnits: number;
  try {
    finalUnits = await simulate(connection, transaction);
  } catch (err) {
    if (
      !isWalletFunded &&
      activeProvider === "marginfi" &&
      config.provider === "auto" &&
      isMarginfiBorrowError(err)
    ) {
      console.log(
        `[Final Simulation Fallback] Marginfi transaction simulation failed (${errorMessage(err)}). Retrying with Kamino Lending fallback...`,
      );
      activeProvider = "kamino";
      transaction = await buildFlashLoanTransaction(
        activeProvider,
        account,
        config.keypair,
        config.loanMint,
        loanBank?.address,
        loanAmountRaw,
        swapInstructions,
        lookupTables,
        latest.blockhash,
        computeUnitLimit,
        config.computeUnitPriceMicroLamports,
        additionalSigners,
      );
      finalUnits = await simulate(connection, transaction);
    } else {
      throw err;
    }
  }
  const feeResponse = await connection.getFeeForMessage(
    transaction.message,
    config.commitment,
  );
  if (feeResponse.value === null) throw new Error("RPC could not calculate transaction fee");
  const solUsd = await fetchSolUsd(config);
  const totalFeeLamports =
    BigInt(feeResponse.value) + stableLeg.executionFeeLamports;
  const executionCostRaw = bufferedFeeRaw(
    totalFeeLamports,
    solUsd,
    config.solUsdBufferBps,
  );
  const kaminoFeeRaw = (!isWalletFunded && activeProvider === "kamino")
    ? kaminoFlashLoanFeeRaw(loanAmountRaw, config.loanMint)
    : 0n;
  const netProfitRaw = cycle.grossProfitRaw - executionCostRaw - kaminoFeeRaw;

  console.log(`Simulation: passed (${finalUnits.toLocaleString()} compute units)`);
  console.log(`Transaction size: ${wireSize}/${MAX_WIRE_TRANSACTION_BYTES} bytes`);
  console.log(`Network fee: ${feeResponse.value.toLocaleString()} lamports`);
  console.log(
    `Stable.com execution fee: ${stableLeg.executionFeeLamports.toString()} lamports`,
  );
  console.log(`SOL/USD: $${solUsd.toFixed(4)}`);
  console.log(`Buffered execution cost: ${formatRaw(executionCostRaw)} ${config.loanSymbol}`);
  if (kaminoFeeRaw > 0n) {
    console.log(`Kamino flash loan fee: ${formatRaw(kaminoFeeRaw)} ${config.loanSymbol}`);
  }
  console.log(`Guaranteed net result: ${formatRaw(netProfitRaw)} ${config.loanSymbol}`);

  const stableFromSymbol = stableInputSymbol(
    config.swapOrder,
    config.loanSymbol,
    config.intermediateSymbol,
  );
  const stableToSymbol = stableOutputSymbol(
    config.swapOrder,
    config.loanSymbol,
    config.intermediateSymbol,
  );
  const plan: JsonRecord = {
    createdAt: new Date().toISOString(),
    wallet: walletAddress.toBase58(),
    flashLoanProvider: isWalletFunded ? "wallet" : activeProvider,
    marginfiAccount: (!isWalletFunded && activeProvider === "marginfi") ? account?.address.toBase58() ?? null : null,
    marginfiBank: (!isWalletFunded && activeProvider === "marginfi") ? loanBank?.address.toBase58() ?? null : null,
    kaminoReserve: (!isWalletFunded && activeProvider === "kamino") ? getKaminoReserveInfo(config.loanMint).reserve.toBase58() : null,
    kaminoFlashLoanFeeRaw: kaminoFeeRaw.toString(),
    pair: `${config.loanSymbol}/${config.intermediateSymbol}`,
    stablePair: `${stableFromSymbol}/${stableToSymbol}`,
    dexPair: config.swapOrder === "dex-first"
      ? `${config.loanSymbol}/${config.intermediateSymbol}`
      : `${config.intermediateSymbol}/${config.loanSymbol}`,
    route: routeFlow,
    dexProvider: dexName,
    swapOrder: config.swapOrder,
    loanSymbol: config.loanSymbol,
    intermediateSymbol: config.intermediateSymbol,
    configuredMaximumPrincipalRaw: config.maximumLoanAmountRaw.toString(),
    principalRaw: loanAmountRaw.toString(),
    capacityAdjusted: sized.capacityAdjusted,
    stableCapacityRaw: sized.capacityRaw.toString(),
    stableUsableCapacityRaw: sized.usableCapacityRaw.toString(),
    firstLegMinimumRaw: cycle.firstLegMinimumRaw.toString(),
    secondLegMinimumRaw: cycle.secondLegMinimumRaw.toString(),
    grossProfitRaw: cycle.grossProfitRaw.toString(),
    executionCostRaw: executionCostRaw.toString(),
    netProfitRaw: netProfitRaw.toString(),
    effectiveMinimumGrossProfitRaw: effectiveMinGrossRaw.toString(),
    effectiveMinimumNetProfitRaw: effectiveMinNetRaw.toString(),
    configuredBaseMinimumGrossProfitRaw: config.minimumGrossProfitRaw.toString(),
    configuredBaseMinimumNetProfitRaw: config.minimumNetProfitRaw.toString(),
    solUsd,
    networkFeeLamports: feeResponse.value,
    stableExecutionFeeLamports: stableLeg.executionFeeLamports.toString(),
    stableNonce: stableLeg.nonce.toString(),
    stableDeadline: stableLeg.deadline.toString(),
    computeUnitLimit,
    unitsConsumed: finalUnits,
    transactionBytes: wireSize,
    recentBlockhash: latest.blockhash,
    lastValidBlockHeight: latest.lastValidBlockHeight,
    ignoredDexComputeBudgetInstructions: ignoredComputeBudgetCount,
    firstQuote,
    secondJupiterQuote,
    stableStatus: stableQuote.status,
  };
  writePlan(config.outputPath, plan);
  console.log(`Plan: ${config.outputPath}`);

  if (netProfitRaw < effectiveMinNetRaw) {
    throw new Error(
      `No executable opportunity: guaranteed net ${formatRaw(netProfitRaw)} ${config.loanSymbol} is below ${formatRaw(effectiveMinNetRaw)} ${config.loanSymbol} (scaled minimum)`,
    );
  }
  if (!cli.send) {
    console.log("Dry run complete: the atomic transaction was not sent.");
    return;
  }

  await assertMainnet(connection);
  const broadcastResult = await broadcastJitoOrFallback(
    connection,
    transaction,
    config,
  );
  const signature = broadcastResult.signature;
  plan.transactionSignature = signature;
  plan.transactionStatus = "submitted";
  plan.broadcastMethod = broadcastResult.method;
  writePlan(config.outputPath, plan);
  console.log(`Submitted: https://solscan.io/tx/${signature}`);
  try {
    const confirmation = await connection.confirmTransaction(
      {
        signature,
        blockhash: latest.blockhash,
        lastValidBlockHeight: latest.lastValidBlockHeight,
      },
      config.commitment,
    );
    if (confirmation.value.err) {
      plan.transactionStatus = "reverted";
      plan.transactionError = confirmation.value.err;
      writePlan(config.outputPath, plan);
      throw new Error(`Transaction failed: ${JSON.stringify(confirmation.value.err)}`);
    }
  } catch (error) {
    if (!isBlockheightExpiry(error)) throw error;

    for (let statusCheck = 0; statusCheck < 2; statusCheck += 1) {
      const response = await connection.getSignatureStatuses([signature], {
        searchTransactionHistory: true,
      });
      const signatureState = classifySignatureStatus(response.value[0]);
      if (signatureState === "confirmed") {
        plan.transactionStatus = "confirmed";
        delete plan.transactionError;
        writePlan(config.outputPath, plan);
        console.log(`Confirmed: ${signature}`);
        return;
      }
      if (signatureState === "reverted") {
        plan.transactionStatus = "reverted";
        plan.transactionError = response.value[0]?.err;
        writePlan(config.outputPath, plan);
        throw new Error(
          `Transaction failed: ${JSON.stringify(response.value[0]?.err)}`,
        );
      }
      if (signatureState === "ambiguous") {
        plan.transactionError =
          "blockhash expired, but the signature is still visible below confirmed commitment";
        writePlan(config.outputPath, plan);
        throw new Error(String(plan.transactionError));
      }
      if (statusCheck === 0) {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    }

    plan.transactionStatus = "expired";
    plan.transactionError =
      "blockhash expired and the signature was not recorded on chain";
    writePlan(config.outputPath, plan);
    console.log(`Expired: ${signature}`);
    return;
  }
  plan.transactionStatus = "confirmed";
  delete plan.transactionError;
  writePlan(config.outputPath, plan);
  console.log(`Confirmed: ${signature}`);
}

const invokedPath = process.argv[1] ? pathToFileURL(process.argv[1]).href : "";
if (import.meta.url === invokedPath) {
  main().catch((error: unknown) => {
    console.error(`ERROR: ${errorMessage(error)}`);
    process.exitCode = 1;
  });
}
