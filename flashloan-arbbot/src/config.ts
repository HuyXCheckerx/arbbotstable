import "dotenv/config";
import path from "node:path";
import { usdcToRaw } from "./amounts.js";
import { MAX_COMPUTE_UNITS } from "./constants.js";

function envString(name: string, fallback = ""): string {
  return (process.env[name] ?? fallback).trim();
}

function envBoolean(name: string, fallback: boolean): boolean {
  const raw = envString(name, fallback ? "true" : "false").toLowerCase();
  if (["1", "true", "yes", "on"].includes(raw)) return true;
  if (["0", "false", "no", "off"].includes(raw)) return false;
  throw new Error(`${name} must be true or false`);
}

function envInteger(name: string, fallback: number): number {
  const raw = envString(name, String(fallback));
  if (!/^-?\d+$/.test(raw)) throw new Error(`${name} must be an integer`);
  return Number(raw);
}

function envNumber(name: string, fallback: number): number {
  const value = Number(envString(name, String(fallback)));
  if (!Number.isFinite(value)) throw new Error(`${name} must be numeric`);
  return value;
}

const minTradeRaw = usdcToRaw(envString("FLASH_LOAN_MIN_USDC", "10000"));
const maxTradeRaw = usdcToRaw(envString("FLASH_LOAN_MAX_USDC", "100000"));
const hardMinimumRaw = usdcToRaw("10000");
const hardMaximumRaw = usdcToRaw("100000");
if (minTradeRaw < hardMinimumRaw || maxTradeRaw > hardMaximumRaw || minTradeRaw > maxTradeRaw) {
  throw new Error("Flash-loan sizing must stay within 10,000–100,000 USDC");
}

const computeUnitLimit = envInteger("COMPUTE_UNIT_LIMIT", MAX_COMPUTE_UNITS);
if (computeUnitLimit < 200_000 || computeUnitLimit > MAX_COMPUTE_UNITS) {
  throw new Error(`COMPUTE_UNIT_LIMIT must be between 200000 and ${MAX_COMPUTE_UNITS}`);
}

const executionEnabled = envBoolean("EXECUTION_ENABLED", false);
const dryRun = envBoolean("DRY_RUN", true);
if (executionEnabled && dryRun) {
  throw new Error("Set DRY_RUN=false before enabling execution");
}

const apiKeys = envString("JUP_API_KEYS", envString("JUP_API_KEY"))
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);

export const config = {
  rpcUrl: envString("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
  privateKey: envString("SOLANA_PRIVATE_KEY"),
  walletPublicKey: envString("WALLET_PUBLIC_KEY"),
  jupiterApiKeys: apiKeys,
  executionEnabled,
  dryRun,
  simulateDryRun: envBoolean("SIMULATE_DRY_RUN", true),
  flashLoanMarket: envString("FLASH_LOAN_MARKET", "main") as "main" | "ethena",
  minTradeRaw,
  maxTradeRaw,
  minNetProfitRaw: usdcToRaw(envString("MIN_NET_PROFIT_USDC", "0.10")),
  estimatedTxCostRaw: usdcToRaw(envString("ESTIMATED_TX_COST_USDC", "0.02")),
  slippageBps: envInteger("JUPITER_SLIPPAGE_BPS", 1),
  maxAccounts: envInteger("JUPITER_MAX_ACCOUNTS", 52),
  fastQuotes: envBoolean("JUPITER_FAST_QUOTES", true),
  quoteDelayMs: envInteger("QUOTE_DELAY_MS", 100),
  scanIntervalMs: Math.max(1_000, envInteger("SCAN_INTERVAL_MS", 5_000)),
  computeUnitLimit,
  computeUnitPriceMicroLamports: envInteger("COMPUTE_UNIT_PRICE_MICROLAMPORTS", 10_000),
  computeUnitBuffer: envNumber("COMPUTE_UNIT_BUFFER", 1.15),
  stableTokenReserveRaw: usdcToRaw(envString("STABLE_TOKEN_RESERVE_USDC", "2")),
  stableUsdcReserveRaw: usdcToRaw(envString("STABLE_USDC_RESERVE_USDC", "2")),
  usdtUsdcReserveRaw: usdcToRaw(envString("USDT_MIN_USDC_RESERVE_USDC", "50000")),
  maxStableNativeFeeLamports: BigInt(envString("MAX_STABLE_NATIVE_FEE_LAMPORTS", "0")),
  httpTimeoutMs: envInteger("HTTP_TIMEOUT_MS", 10_000),
  stateFile: path.resolve(envString("BOT_STATE_FILE", "state/bot-state.json")),
  customAltFile: path.resolve(envString("CUSTOM_ALT_FILE", "state/custom-alt.json")),
  customAltAddresses: envString("CUSTOM_ALT_ADDRESSES")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
} as const;

if (config.flashLoanMarket !== "main" && config.flashLoanMarket !== "ethena") {
  throw new Error("FLASH_LOAN_MARKET must be main or ethena");
}
if (config.slippageBps < 0 || config.slippageBps > 100) {
  throw new Error("JUPITER_SLIPPAGE_BPS must be between 0 and 100");
}
if (config.maxAccounts < 1 || config.maxAccounts > 64) {
  throw new Error("JUPITER_MAX_ACCOUNTS must be between 1 and 64");
}
if (config.computeUnitBuffer < 1 || config.computeUnitBuffer > 2) {
  throw new Error("COMPUTE_UNIT_BUFFER must be between 1 and 2");
}
