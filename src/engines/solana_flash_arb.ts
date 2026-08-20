import "dotenv/config";

import { createHash, randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import {
  AssetTag,
  MarginfiAccountWrapper,
  Project0Client,
  deriveMarginfiAccount,
  getConfig,
} from "@0dotxyz/p0-ts-sdk";
import {
  AddressLookupTableAccount,
  Commitment,
  ComputeBudgetProgram,
  Connection,
  Keypair,
  PublicKey,
  SystemProgram,
  Transaction,
  TransactionInstruction,
  VersionedTransaction,
} from "@solana/web3.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
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
const TOKEN_DECIMALS = 6;
const LAMPORTS_PER_SOL = 1_000_000_000;
const MAX_WIRE_TRANSACTION_BYTES = 1232;
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
  keypair: Keypair;
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
  slippageBps: number;
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
}

interface CliOptions {
  quoteOnly: boolean;
  send: boolean;
  createMarginfiAccount: boolean;
  confirmation?: string;
}

interface SwapLeg {
  instructions: TransactionInstruction[];
  lookupTableAddresses: PublicKey[];
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

export function resolveIntermediateMint(symbol: string): PublicKey {
  if (symbol === "PYUSD") return PYUSD_MINT;
  if (symbol === "USDG") return USDG_MINT;
  if (symbol === "USDT") return USDT_MINT;
  throw new Error(
    `SOL_FLASH_ARB_INTERMEDIATE_TOKEN must be PYUSD, USDG, or USDT; received ${symbol}`,
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
  loanMint: PublicKey,
  intermediateMint: PublicKey,
): boolean {
  return (
    (loanMint.equals(PYUSD_MINT) && intermediateMint.equals(USDG_MINT)) ||
    (loanMint.equals(USDG_MINT) && intermediateMint.equals(PYUSD_MINT))
  );
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

function readConfig(): Config {
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
  const capacityBuffer =
    process.env[`SOL_FLASH_ARB_STABLE_CAPACITY_BUFFER_${intermediateSymbol}`] ||
    process.env.SOL_FLASH_ARB_STABLE_CAPACITY_BUFFER_USDT ||
    "1";
  return {
    rpcUrl: requiredEnv("SOLANA_RPC_URL"),
    keypair: loadKeypair(requiredEnv("SOLANA_PRIVATE_KEY")),
    jupiterApiBase: (
      process.env.SOL_FLASH_ARB_JUPITER_API_BASE ||
      "https://api.jup.ag/swap/v1"
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
    slippageBps: envInt("SOL_FLASH_ARB_SLIPPAGE_BPS", 0, 0),
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
    maxAccounts: envInt("SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS", 20, 1),
    onlyDirectRoutes: envBool("SOL_FLASH_ARB_ONLY_DIRECT_ROUTES", true),
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
  };
}

function parseCli(argv: string[]): CliOptions {
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
  npm run solana:flash -- --send --confirm-mainnet EXECUTE_SOLANA_FLASH_ARB
  npm run solana:flash -- --create-marginfi-account --send --confirm-mainnet CREATE_MARGINFI_ACCOUNT

All amounts, RPC settings, API keys, fee settings, and account addresses come from .env.`);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function fetchJson<T>(
  url: string,
  init: RequestInit,
  config: Pick<Config, "httpTimeoutMs" | "httpAttempts">,
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
        const failure = new Error(
          `${description} returned HTTP ${response.status}${excerpt ? `: ${excerpt}` : ""}`,
        );
        if (!retryable || attempt === config.httpAttempts) throw failure;
        lastError = failure;
      } else {
        return JSON.parse(body) as T;
      }
    } catch (error) {
      lastError = error;
      if (attempt === config.httpAttempts) break;
    } finally {
      clearTimeout(timeout);
    }
    await new Promise((resolve) => setTimeout(resolve, attempt * 400));
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

function stableHeaders(): HeadersInit {
  return {
    accept: "application/json",
    "content-type": "application/json",
    origin: "https://stable.com",
    referer: "https://stable.com/",
  };
}

function stableRequestPayload(
  config: Config,
  wallet: PublicKey,
  inputRaw: bigint,
  outputRaw?: bigint,
): JsonRecord {
  const payload: JsonRecord = {
    chainFrom: config.stableChainId,
    assetFrom: config.intermediateSymbol,
    chainTo: config.stableChainId,
    assetTo: config.loanSymbol,
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

async function getStableQuote(
  config: Config,
  wallet: PublicKey,
  inputRaw: bigint,
): Promise<StableQuote> {
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
  const status = unwrapStableStatus(response);
  const quotedInputRaw = parseDecimalToRawFloor(status.amountFrom);
  if (quotedInputRaw !== inputRaw) {
    throw new Error(
      `Stable.com changed the requested input to ${status.amountFrom} ${config.intermediateSymbol}`,
    );
  }
  const outputRaw = parseDecimalToRawFloor(status.amountTo);
  if (outputRaw <= 0n) {
    throw new Error(`Stable.com returned no ${config.loanSymbol} output`);
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
  const leg = buildStableSwapInstruction(
    wallet,
    quote.inputRaw,
    quote.outputRaw,
    order,
    config.stableMaxExecutionFeeLamports,
    config.intermediateMint,
    config.loanMint,
  );
  const minimumDeadline = BigInt(Math.floor(Date.now() / 1_000) + 5);
  if (leg.deadline <= minimumDeadline) {
    throw new Error("Stable.com signed order expires too soon to submit safely");
  }
  return leg;
}

async function getJupiterQuote(
  config: Config,
  inputMint: PublicKey,
  outputMint: PublicKey,
  amountRaw: bigint,
  requestNumber: number,
): Promise<JupiterQuote> {
  const params = new URLSearchParams({
    inputMint: inputMint.toBase58(),
    outputMint: outputMint.toBase58(),
    amount: amountRaw.toString(),
    swapMode: "ExactIn",
    slippageBps: String(config.slippageBps),
    restrictIntermediateTokens: "true",
    onlyDirectRoutes: String(config.onlyDirectRoutes),
    maxAccounts: String(config.maxAccounts),
  });
  const quote = await fetchJson<JupiterQuote>(
    `${config.jupiterApiBase}/quote?${params}`,
    { headers: jupiterHeaders(config, requestNumber) },
    config,
    "Jupiter quote",
  );
  if (!quote.routePlan?.length) throw new Error("Jupiter returned no swap route");
  if (BigInt(quote.inAmount) !== amountRaw) {
    throw new Error(`Jupiter changed the requested input amount to ${quote.inAmount}`);
  }
  return quote;
}

async function getCapacitySizedCycle(
  config: Config,
  wallet: PublicKey,
): Promise<SizedCycle> {
  let loanAmountRaw = config.maximumLoanAmountRaw;
  let capacityAdjusted = false;
  const isTwoHop = isTwoHopJupiterRoute(
    config.loanMint,
    config.intermediateMint,
  );

  for (
    let attempt = 0;
    attempt < config.stableCapacitySizingAttempts;
    attempt += 1
  ) {
    let firstQuote: JupiterQuote;
    let secondJupiterQuote: JupiterQuote | undefined;
    let firstMinimumRaw: bigint;

    if (isTwoHop) {
      firstQuote = await getJupiterQuote(
        config,
        config.loanMint,
        USDC_MINT,
        loanAmountRaw,
        attempt * 2,
      );
      const usdcMinimumRaw = BigInt(firstQuote.otherAmountThreshold);
      secondJupiterQuote = await getJupiterQuote(
        config,
        USDC_MINT,
        config.intermediateMint,
        usdcMinimumRaw,
        attempt * 2 + 1,
      );
      firstMinimumRaw = BigInt(secondJupiterQuote.otherAmountThreshold);
    } else {
      firstQuote = await getJupiterQuote(
        config,
        config.loanMint,
        config.intermediateMint,
        loanAmountRaw,
        attempt,
      );
      firstMinimumRaw = BigInt(firstQuote.otherAmountThreshold);
    }

    const stableQuote = await getStableQuote(config, wallet, firstMinimumRaw);
    const capacityRaw = parseDecimalToRawFloor(String(stableQuote.status.balance));
    const minimumRaw = parseDecimalToRawCeil(String(stableQuote.status.min));
    const maximumRaw = parseDecimalToRawFloor(String(stableQuote.status.max));
    if (capacityRaw <= config.stableCapacityBufferRaw) {
      throw new Error(
        `Stable.com capacity ${formatRaw(capacityRaw)} ${config.intermediateSymbol} is not above the configured ${formatRaw(config.stableCapacityBufferRaw)} ${config.intermediateSymbol} safety buffer`,
      );
    }
    const capacityAfterBufferRaw =
      capacityRaw - config.stableCapacityBufferRaw;
    const usableCapacityRaw =
      maximumRaw > 0n && maximumRaw < capacityAfterBufferRaw
        ? maximumRaw
        : capacityAfterBufferRaw;
    if (usableCapacityRaw < minimumRaw) {
      throw new Error(
        `Stable.com usable capacity ${formatRaw(usableCapacityRaw)} ${config.intermediateSymbol} is below its ${formatRaw(minimumRaw)} ${config.intermediateSymbol} minimum order`,
      );
    }

    if (stableQuote.inputRaw <= usableCapacityRaw) {
      if (stableQuote.inputRaw < minimumRaw) {
        throw new Error(
          `Sized Stable.com order ${formatRaw(stableQuote.inputRaw)} ${config.intermediateSymbol} is below its ${formatRaw(minimumRaw)} ${config.intermediateSymbol} minimum`,
        );
      }
      return {
        loanAmountRaw,
        firstQuote,
        secondJupiterQuote,
        stableQuote,
        cycle: conservativeCycle(
          secondJupiterQuote ?? firstQuote,
          stableQuote.outputRaw,
          loanAmountRaw,
        ),
        capacityRaw,
        usableCapacityRaw,
        capacityAdjusted,
      };
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
): Promise<SwapLeg> {
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
        useSharedAccounts: true,
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
    lookupTableAddresses: (result.addressLookupTableAddresses ?? []).map(
      (address) => new PublicKey(address),
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

function assertNoExistingLoanLiability(
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
): Promise<VersionedTransaction> {
  const uiAmount = formatRaw(loanAmountRaw, TOKEN_DECIMALS);
  const borrow = await account.makeBorrowIx(bankAddress, uiAmount, {
    createAtas: false,
    wrapAndUnwrapSol: false,
    // p0-ts-sdk 2.5.3 does not forward the inferred authority/group through
    // its async Anchor path, so provide both explicitly.
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
  const transaction = await account.makeFlashLoanTx({
    bankMap: account.getClient().bankMap,
    ixs: [
      ComputeBudgetProgram.setComputeUnitLimit({ units: computeUnitLimit }),
      ComputeBudgetProgram.setComputeUnitPrice({
        microLamports: computeUnitPriceMicroLamports,
      }),
      ...borrow.instructions,
      ...swapInstructions,
      ...repay.instructions,
    ],
    signers: [],
    blockhash,
    addressLookupTableAccounts: lookupTables,
  });
  transaction.sign([keypair]);
  return transaction;
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

async function assertTokenAccountsExist(
  connection: Connection,
  owner: PublicKey,
  intermediateMint: PublicKey = PYUSD_MINT,
  intermediateSymbol = "PYUSD",
  loanMint: PublicKey = USDC_MINT,
  loanSymbol = "USDC",
): Promise<void> {
  const inputTokenProgram = intermediateTokenProgram(intermediateMint);
  const outputTokenProgram = intermediateTokenProgram(loanMint);
  const loanAta = getAssociatedTokenAddressSync(
    loanMint,
    owner,
    false,
    outputTokenProgram,
  );
  const interAta = getAssociatedTokenAddressSync(
    intermediateMint,
    owner,
    false,
    inputTokenProgram,
  );
  const checkAccounts: PublicKey[] = [loanAta, interAta];
  const requiresUsdc =
    !loanMint.equals(USDC_MINT) && !intermediateMint.equals(USDC_MINT);
  let usdcAta: PublicKey | undefined;
  if (requiresUsdc) {
    usdcAta = getAssociatedTokenAddressSync(
      USDC_MINT,
      owner,
      false,
      TOKEN_PROGRAM_ID,
    );
    checkAccounts.push(usdcAta);
  }
  const infos = await connection.getMultipleAccountsInfo(
    checkAccounts,
    "confirmed",
  );
  const missing: string[] = [];
  if (!infos[0]) missing.push(`${loanSymbol} ATA ${loanAta.toBase58()}`);
  if (!infos[1]) missing.push(`${intermediateSymbol} ATA ${interAta.toBase58()}`);
  if (requiresUsdc && !infos[2]) missing.push(`USDC ATA ${usdcAta!.toBase58()}`);
  if (missing.length) {
    throw new Error(
      `Create the wallet token accounts before using the flash loan: ${missing.join(", ")}`,
    );
  }
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

function wrapConnectionWithResilientBatchRequest(connection: Connection): Connection {
  const fallbackUrls = [
    connection.rpcEndpoint,
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
  ].filter((u, i, arr) => arr.indexOf(u) === i);

  (connection as any)._rpcBatchRequest = async (requests: any[]) => {
    let lastError: any;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const url = fallbackUrls[attempt % fallbackUrls.length];
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
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            if (res.status === 429) {
              throw new Error(`HTTP 429 rate limit on ${url}`);
            }
            return await res.json();
          }),
        );
        if (Array.isArray(results) && results.length === requests.length) {
          return results;
        }
      } catch (err) {
        lastError = err;
      }
      await new Promise((r) => setTimeout(r, (attempt + 1) * 300));
    }
    throw lastError || new Error("Failed to fetch account infos after retries");
  };
  return connection;
}

async function main(): Promise<void> {
  const cli = parseCli(process.argv.slice(2));
  const config = readConfig();
  const connection = wrapConnectionWithResilientBatchRequest(
    new Connection(config.rpcUrl, config.commitment),
  );
  const walletAddress = config.keypair.publicKey;

  if (cli.createMarginfiAccount) {
    await createMarginfiAccount(config, cli, connection);
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
  const sized = await getCapacitySizedCycle(config, walletAddress);
  const { loanAmountRaw, firstQuote, secondJupiterQuote, stableQuote, cycle } = sized;
  const isTwoHop = isTwoHopJupiterRoute(config.loanMint, config.intermediateMint);
  console.log(`Flash-loan principal: ${formatRaw(loanAmountRaw)} ${config.loanSymbol}`);
  console.log(
    `Stable.com capacity: ${formatRaw(sized.capacityRaw)} ${config.intermediateSymbol} (${formatRaw(sized.usableCapacityRaw)} usable)`,
  );
  if (isTwoHop && secondJupiterQuote) {
    console.log(
      `Leg 1 (Jupiter): ${config.loanSymbol} -> USDC -> ${config.intermediateSymbol} | guaranteed minimum: ${formatRaw(cycle.firstLegMinimumRaw)} ${config.intermediateSymbol}`,
    );
  } else {
    console.log(
      `Leg 1 (Jupiter): ${config.loanSymbol} -> ${config.intermediateSymbol} | guaranteed minimum: ${formatRaw(cycle.firstLegMinimumRaw)} ${config.intermediateSymbol}`,
    );
  }
  console.log(
    `Leg 2 (Stable.com): ${config.intermediateSymbol} -> ${config.loanSymbol} | executable output: ${formatRaw(cycle.secondLegMinimumRaw)} ${config.loanSymbol}`,
  );
  console.log(`Stable.com token fee: ${formatRaw(stableQuote.tokenFeeRaw)} ${config.loanSymbol}`);
  console.log(`Guaranteed gross result: ${formatRaw(cycle.grossProfitRaw)} ${config.loanSymbol}`);

  if (cycle.grossProfitRaw < config.minimumGrossProfitRaw) {
    throw new Error(
      `No executable opportunity: guaranteed gross ${formatRaw(cycle.grossProfitRaw)} ${config.loanSymbol} is below ${formatRaw(config.minimumGrossProfitRaw)} ${config.loanSymbol}`,
    );
  }
  if (cli.quoteOnly) {
    console.log("Quote-only mode: no transaction was built, signed, or sent.");
    return;
  }

  await assertTokenAccountsExist(
    connection,
    walletAddress,
    config.intermediateMint,
    config.intermediateSymbol,
    config.loanMint,
    config.loanSymbol,
  );
  const client = await Project0Client.initialize(
    connection,
    getConfig("production"),
  );
  const account = await selectMarginfiAccount(
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
  const loanBank = loanBanks[0];
  assertNoExistingLoanLiability(account, loanBank.address, config.loanSymbol);
  console.log(`Marginfi account: ${account.address.toBase58()}`);
  console.log(`Marginfi ${config.loanSymbol} bank: ${loanBank.address.toBase58()}`);

  let swapInstructions: TransactionInstruction[];
  let lookupTables: AddressLookupTableAccount[];
  let ignoredComputeBudgetCount = 0;
  let stableLeg: StableLeg;

  if (isTwoHop && secondJupiterQuote) {
    const [firstSwap1, firstSwap2, stableLegResult] = await Promise.all([
      getSwapLeg(config, firstQuote, walletAddress, 1),
      getSwapLeg(config, secondJupiterQuote, walletAddress, 2),
      getStableLeg(config, walletAddress, stableQuote),
    ]);
    stableLeg = stableLegResult;
    const [jupiterLookupTables1, jupiterLookupTables2] = await Promise.all([
      fetchLookupTables(connection, firstSwap1.lookupTableAddresses),
      fetchLookupTables(connection, firstSwap2.lookupTableAddresses),
    ]);
    lookupTables = mergeLookupTables(
      client.addressLookupTables ?? [],
      jupiterLookupTables1,
      jupiterLookupTables2,
    );
    swapInstructions = [
      ...firstSwap1.instructions,
      ...firstSwap2.instructions,
      stableLeg.instruction,
    ];
    ignoredComputeBudgetCount =
      firstSwap1.ignoredComputeBudgetInstructionCount +
      firstSwap2.ignoredComputeBudgetInstructionCount;
  } else {
    const [firstSwap, stableLegResult] = await Promise.all([
      getSwapLeg(config, firstQuote, walletAddress, 1),
      getStableLeg(config, walletAddress, stableQuote),
    ]);
    stableLeg = stableLegResult;
    const jupiterLookupTables = await fetchLookupTables(
      connection,
      firstSwap.lookupTableAddresses,
    );
    lookupTables = mergeLookupTables(
      client.addressLookupTables ?? [],
      jupiterLookupTables,
    );
    swapInstructions = [...firstSwap.instructions, stableLeg.instruction];
    ignoredComputeBudgetCount = firstSwap.ignoredComputeBudgetInstructionCount;
  }

  const latest = await connection.getLatestBlockhash(config.commitment);
  const probe = await buildFlashTransaction(
    account,
    config.keypair,
    loanBank.address,
    loanAmountRaw,
    swapInstructions,
    lookupTables,
    latest.blockhash,
    config.probeComputeUnitLimit,
    0,
  );
  const probeWireSize = probe.serialize().length;
  if (probeWireSize > MAX_WIRE_TRANSACTION_BYTES) {
    throw new Error(
      `Atomic transaction is ${probeWireSize} bytes before simulation; Solana maximum is ${MAX_WIRE_TRANSACTION_BYTES}. Lower SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS.`,
    );
  }
  const probeUnits = await simulate(connection, probe);
  const computeUnitLimit = finalComputeUnitLimit(
    probeUnits,
    config.computeUnitSafetyBps,
    config.probeComputeUnitLimit,
  );
  const transaction = await buildFlashTransaction(
    account,
    config.keypair,
    loanBank.address,
    loanAmountRaw,
    swapInstructions,
    lookupTables,
    latest.blockhash,
    computeUnitLimit,
    config.computeUnitPriceMicroLamports,
  );
  const wireSize = transaction.serialize().length;
  if (wireSize > MAX_WIRE_TRANSACTION_BYTES) {
    throw new Error(
      `Atomic transaction is ${wireSize} bytes; Solana maximum is ${MAX_WIRE_TRANSACTION_BYTES}. Reduce SOL_FLASH_ARB_JUPITER_MAX_ACCOUNTS or keep direct routes enabled.`,
    );
  }
  const finalUnits = await simulate(connection, transaction);
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
  const netProfitRaw = cycle.grossProfitRaw - executionCostRaw;

  console.log(`Simulation: passed (${finalUnits.toLocaleString()} compute units)`);
  console.log(`Transaction size: ${wireSize}/${MAX_WIRE_TRANSACTION_BYTES} bytes`);
  console.log(`Network fee: ${feeResponse.value.toLocaleString()} lamports`);
  console.log(
    `Stable.com execution fee: ${stableLeg.executionFeeLamports.toString()} lamports`,
  );
  console.log(`SOL/USD: $${solUsd.toFixed(4)}`);
  console.log(`Buffered execution cost: ${formatRaw(executionCostRaw)} ${config.loanSymbol}`);
  console.log(`Guaranteed net result: ${formatRaw(netProfitRaw)} ${config.loanSymbol}`);

  const plan = {
    createdAt: new Date().toISOString(),
    wallet: walletAddress.toBase58(),
    marginfiAccount: account.address.toBase58(),
    pair: `${config.intermediateSymbol}/${config.loanSymbol}`,
    loanSymbol: config.loanSymbol,
    intermediateSymbol: config.intermediateSymbol,
    marginfiBank: loanBank.address.toBase58(),
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
    solUsd,
    networkFeeLamports: feeResponse.value,
    stableExecutionFeeLamports: stableLeg.executionFeeLamports.toString(),
    stableNonce: stableLeg.nonce.toString(),
    stableDeadline: stableLeg.deadline.toString(),
    computeUnitLimit,
    unitsConsumed: finalUnits,
    transactionBytes: wireSize,
    ignoredJupiterComputeBudgetInstructions: ignoredComputeBudgetCount,
    firstQuote,
    secondJupiterQuote,
    stableStatus: stableQuote.status,
  };
  fs.mkdirSync(path.dirname(config.outputPath), { recursive: true });
  fs.writeFileSync(config.outputPath, `${JSON.stringify(plan, null, 2)}\n`, {
    mode: 0o600,
  });
  console.log(`Plan: ${config.outputPath}`);

  if (netProfitRaw < config.minimumNetProfitRaw) {
    throw new Error(
      `No executable opportunity: guaranteed net ${formatRaw(netProfitRaw)} ${config.loanSymbol} is below ${formatRaw(config.minimumNetProfitRaw)} ${config.loanSymbol}`,
    );
  }
  if (!cli.send) {
    console.log("Dry run complete: the atomic transaction was not sent.");
    return;
  }

  await assertMainnet(connection);
  const signature = await connection.sendRawTransaction(transaction.serialize(), {
    skipPreflight: true,
    preflightCommitment: "processed",
    maxRetries: 3,
  });
  console.log(`Submitted: https://solscan.io/tx/${signature}`);
  const confirmation = await connection.confirmTransaction(
    {
      signature,
      blockhash: latest.blockhash,
      lastValidBlockHeight: latest.lastValidBlockHeight,
    },
    config.commitment,
  );
  if (confirmation.value.err) {
    throw new Error(`Transaction failed: ${JSON.stringify(confirmation.value.err)}`);
  }
  console.log(`Confirmed: ${signature}`);
}

const invokedPath = process.argv[1] ? pathToFileURL(process.argv[1]).href : "";
if (import.meta.url === invokedPath) {
  main().catch((error: unknown) => {
    console.error(`ERROR: ${errorMessage(error)}`);
    process.exitCode = 1;
  });
}
