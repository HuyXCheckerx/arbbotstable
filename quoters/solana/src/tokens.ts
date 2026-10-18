import { Connection, PublicKey } from "@solana/web3.js";
import { ExtensionType, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, getExtensionData, getExtensionTypes,
  getTransferFeeConfig, getTransferHook, unpackMint, type Mint } from "@solana/spl-token";

export interface TokenInfo { symbol: string; mint: PublicKey; decimals: number; program: PublicKey }

/** Supported stablecoins. Mint owner program and decimals are re-verified on chain before use. */
export const KNOWN_TOKENS = {
  USDC: { mint: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", decimals: 6, program: TOKEN_PROGRAM_ID },
  USDT: { mint: "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", decimals: 6, program: TOKEN_PROGRAM_ID },
  // PayPal USD (Paxos). Token-2022 with permanent delegate, zero transfer fee and an unset hook.
  PYUSD: { mint: "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", decimals: 6, program: TOKEN_2022_PROGRAM_ID },
  // Global Dollar (Paxos). Same Token-2022 extension set as PYUSD.
  USDG: { mint: "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH", decimals: 6, program: TOKEN_2022_PROGRAM_ID },
} as const;
export type KnownSymbol = keyof typeof KNOWN_TOKENS;

export function knownToken(symbol: string): TokenInfo {
  const upper = symbol.toUpperCase();
  const entry = (KNOWN_TOKENS as Record<string, (typeof KNOWN_TOKENS)[KnownSymbol]>)[upper];
  if (!entry) throw new Error(`Unknown token ${symbol}; supported: ${Object.keys(KNOWN_TOKENS).join(", ")}`);
  return { symbol: upper, mint: new PublicKey(entry.mint), decimals: entry.decimals, program: entry.program };
}

// Extension numbers added after the installed SDK's enum are looked up by name when available.
const PAUSABLE = (ExtensionType as unknown as Record<string, number>).PausableConfig;

/**
 * Reject Token-2022 mints whose extensions would make an atomic round trip unsafe or impossible:
 * non-transferable, transfer fees (unless allowed), an active transfer hook, frozen-by-default
 * accounts, or a paused mint. Unknown extensions are tolerated; the on-chain balance guard
 * still enforces the final profit.
 */
export function assertTradableMint(mint: Pick<Mint, "tlvData">, symbol: string, allowTransferFee = false): void {
  const types = getExtensionTypes(mint.tlvData);
  if (types.includes(ExtensionType.NonTransferable)) throw new Error(`${symbol} is non-transferable`);
  const fee = getTransferFeeConfig(mint as Mint);
  if (fee && !allowTransferFee && (fee.olderTransferFee.transferFeeBasisPoints > 0
      || fee.newerTransferFee.transferFeeBasisPoints > 0)) {
    throw new Error(`${symbol} has a nonzero Token-2022 transfer fee`);
  }
  const hook = getTransferHook(mint as Mint);
  if (hook && !hook.programId.equals(PublicKey.default)) {
    throw new Error(`${symbol} has an active transfer hook (${hook.programId.toBase58()})`);
  }
  if (types.includes(ExtensionType.DefaultAccountState)) {
    const state = getExtensionData(ExtensionType.DefaultAccountState, mint.tlvData);
    if (state?.[0] === 2) throw new Error(`${symbol} creates frozen token accounts by default`);
  }
  if (PAUSABLE !== undefined && types.includes(PAUSABLE as ExtensionType)) {
    const config = getExtensionData(PAUSABLE as ExtensionType, mint.tlvData);
    if (config && config[32] === 1) throw new Error(`${symbol} is paused`);
  }
}

/** Resolve a known symbol and verify its mint account (owner program, decimals, extensions) on chain. */
export async function loadToken(connection: Connection, symbol: string, allowTransferFee = false): Promise<TokenInfo> {
  const token = knownToken(symbol);
  const account = await connection.getAccountInfo(token.mint);
  if (!account) throw new Error(`${token.symbol} mint ${token.mint.toBase58()} not found`);
  if (!account.owner.equals(token.program)) {
    throw new Error(`${token.symbol} mint is owned by ${account.owner.toBase58()}, expected ${token.program.toBase58()}`);
  }
  const mint = unpackMint(token.mint, account, account.owner);
  if (mint.decimals !== token.decimals) throw new Error(`${token.symbol} has ${mint.decimals} decimals, expected ${token.decimals}`);
  if (account.owner.equals(TOKEN_2022_PROGRAM_ID)) assertTradableMint(mint, token.symbol, allowTransferFee);
  return token;
}

export interface Pair { base: string; quote: string }

/** Explicit intermediate mint allowlist; metadata and transfer restrictions are checked on chain. */
export async function loadIntermediateToken(connection: Connection, mint: string): Promise<TokenInfo> {
  const key = new PublicKey(mint), account = await connection.getAccountInfo(key);
  if (!account || ![TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID].some(p => p.equals(account.owner))) throw new Error("Intermediate mint is not an SPL mint");
  const decoded = unpackMint(key, account, account.owner);
  if (!decoded.isInitialized || decoded.freezeAuthority) throw new Error("Intermediate mint is uninitialized or has a freeze authority");
  if (account.owner.equals(TOKEN_2022_PROGRAM_ID)) assertTradableMint(decoded, key.toBase58(), false);
  return { symbol: key.toBase58().slice(0, 8), mint: key, decimals: decoded.decimals, program: account.owner };
}

/** Parse `USDC/USDT,PYUSD/USDG`. The base token is borrowed and must come back with profit. */
export function parsePairs(value: string): Pair[] {
  const pairs = value.split(",").map((s) => s.trim()).filter(Boolean).map((entry) => {
    const [base, quote, extra] = entry.split("/").map((s) => s.trim().toUpperCase());
    if (!base || !quote || extra !== undefined) throw new Error(`Invalid pair "${entry}"; expected BASE/QUOTE`);
    knownToken(base); knownToken(quote);
    if (base === quote) throw new Error(`Pair ${entry} uses the same token twice`);
    return { base, quote };
  });
  if (!pairs.length) throw new Error("No trading pairs configured");
  return pairs;
}

/** Pairs from PAIRS, or the legacy BASE_TOKEN (+ optional QUOTE_TOKEN) settings. */
export function configuredPairs(env: NodeJS.ProcessEnv = process.env): Pair[] {
  if (env.PAIRS) return parsePairs(env.PAIRS);
  const base = (env.BASE_TOKEN ?? "USDC").toUpperCase();
  const quote = (env.QUOTE_TOKEN ?? (base === "USDC" ? "USDT" : "USDC")).toUpperCase();
  return parsePairs(`${base}/${quote}`);
}
