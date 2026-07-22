import { createHash } from "node:crypto";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
} from "@solana/spl-token";
import { PublicKey, SystemProgram, SYSVAR_INSTRUCTIONS_PUBKEY } from "@solana/web3.js";

export const USDC_DECIMALS = 6;
export const RAW_PER_USDC = 1_000_000n;

export const USDC_MINT = new PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");
export const USDG_MINT = new PublicKey("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH");
export const PYUSD_MINT = new PublicKey("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo");
export const USDT_MINT = new PublicKey("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB");

export const STABLE_PROGRAM_ID = new PublicKey("2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG");
export const STABLE_CHAIN_ID = 102;
export const STABLE_API = "https://api-defi.stable.com";
export const JUPITER_BUILD_API = "https://api.jup.ag/swap/v2/build";

export const MAIN_STATE_SEED = Buffer.from("main_state");
export const POOL_SEED = Buffer.from("pool");
export const NONCE_SEED = Buffer.from("nonce");
export const NATIVE_FEE_SEED = Buffer.from("native_fee");
export const SINGLE_CHAIN_SWAP_DISCRIMINATOR = createHash("sha256")
  .update("global:single_chain_swap")
  .digest()
  .subarray(0, 8);

export const PROGRAM_IDS = {
  token: TOKEN_PROGRAM_ID,
  token2022: TOKEN_2022_PROGRAM_ID,
  associatedToken: ASSOCIATED_TOKEN_PROGRAM_ID,
  system: SystemProgram.programId,
  instructionsSysvar: SYSVAR_INSTRUCTIONS_PUBKEY,
} as const;

export const TOKEN_CONFIG = {
  USDC: { symbol: "USDC", mint: USDC_MINT, tokenProgram: TOKEN_PROGRAM_ID },
  USDG: { symbol: "USDG", mint: USDG_MINT, tokenProgram: TOKEN_2022_PROGRAM_ID },
  PYUSD: { symbol: "PYUSD", mint: PYUSD_MINT, tokenProgram: TOKEN_2022_PROGRAM_ID },
  USDT: { symbol: "USDT", mint: USDT_MINT, tokenProgram: TOKEN_PROGRAM_ID },
} as const;

export const DIRECT_ROUTES = [
  { token: "USDG", venueOrder: "stable_first" },
  { token: "PYUSD", venueOrder: "stable_first" },
  { token: "USDG", venueOrder: "jupiter_first" },
  { token: "PYUSD", venueOrder: "jupiter_first" },
  { token: "USDT", venueOrder: "jupiter_first" },
] as const;

export const SOLANA_PACKET_DATA_SIZE = 1_232;
export const MAX_COMPUTE_UNITS = 1_400_000;

export { ASSOCIATED_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID };

export type TokenSymbol = keyof typeof TOKEN_CONFIG;
export type IntermediateToken = Exclude<TokenSymbol, "USDC">;
export type VenueOrder = "stable_first" | "jupiter_first";
