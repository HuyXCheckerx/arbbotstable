import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountIdempotentInstruction,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import {
  Connection,
  PublicKey,
  Transaction,
  sendAndConfirmTransaction,
  type Keypair,
} from "@solana/web3.js";
import { TOKEN_CONFIG, type TokenSymbol } from "./constants.js";

export function getWalletAta(wallet: PublicKey, symbol: TokenSymbol): PublicKey {
  const token = TOKEN_CONFIG[symbol];
  return getAssociatedTokenAddressSync(
    token.mint,
    wallet,
    false,
    token.tokenProgram,
    ASSOCIATED_TOKEN_PROGRAM_ID,
  );
}

export async function findMissingWalletAtas(
  connection: Connection,
  wallet: PublicKey,
): Promise<Array<{ symbol: TokenSymbol; address: PublicKey }>> {
  const symbols: TokenSymbol[] = ["USDC", "USDG", "PYUSD", "USDT"];
  const entries = symbols.map((symbol) => ({ symbol, address: getWalletAta(wallet, symbol) }));
  const infos = await connection.getMultipleAccountsInfo(
    entries.map((entry) => entry.address),
    "confirmed",
  );
  return entries.filter((_, index) => !infos[index]);
}

export async function initializeWalletAtas(
  connection: Connection,
  payer: Keypair,
): Promise<string | null> {
  const missing = await findMissingWalletAtas(connection, payer.publicKey);
  if (missing.length === 0) return null;
  const transaction = new Transaction();
  for (const entry of missing) {
    const token = TOKEN_CONFIG[entry.symbol];
    transaction.add(
      createAssociatedTokenAccountIdempotentInstruction(
        payer.publicKey,
        entry.address,
        payer.publicKey,
        token.mint,
        token.tokenProgram,
        ASSOCIATED_TOKEN_PROGRAM_ID,
      ),
    );
  }
  return sendAndConfirmTransaction(connection, transaction, [payer], {
    commitment: "confirmed",
    preflightCommitment: "confirmed",
  });
}
