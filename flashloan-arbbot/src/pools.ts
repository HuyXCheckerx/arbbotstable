import { getAssociatedTokenAddressSync } from "@solana/spl-token";
import { Connection, PublicKey } from "@solana/web3.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  POOL_SEED,
  STABLE_PROGRAM_ID,
  TOKEN_CONFIG,
  type TokenSymbol,
} from "./constants.js";

export function getStablePoolAta(symbol: TokenSymbol): PublicKey {
  const token = TOKEN_CONFIG[symbol];
  const pool = PublicKey.findProgramAddressSync(
    [POOL_SEED, token.mint.toBuffer()],
    STABLE_PROGRAM_ID,
  )[0];
  return getAssociatedTokenAddressSync(
    token.mint,
    pool,
    true,
    token.tokenProgram,
    ASSOCIATED_TOKEN_PROGRAM_ID,
  );
}

export async function fetchStablePoolBalances(
  connection: Connection,
): Promise<Record<TokenSymbol, bigint>> {
  const symbols: TokenSymbol[] = ["USDC", "USDG", "PYUSD", "USDT"];
  const balances = await Promise.all(
    symbols.map(async (symbol) => {
      const response = await connection.getTokenAccountBalance(getStablePoolAta(symbol), "confirmed");
      return [symbol, BigInt(response.value.amount)] as const;
    }),
  );
  return Object.fromEntries(balances) as Record<TokenSymbol, bigint>;
}
