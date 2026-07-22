import bs58 from "bs58";
import { Keypair, PublicKey } from "@solana/web3.js";
import { config } from "./config.js";

export function loadKeypair(secret = config.privateKey): Keypair {
  if (!secret) throw new Error("SOLANA_PRIVATE_KEY is required for this command");
  try {
    if (secret.trim().startsWith("[")) {
      return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(secret) as number[]));
    }
    return Keypair.fromSecretKey(bs58.decode(secret));
  } catch (error) {
    throw new Error(`Cannot decode SOLANA_PRIVATE_KEY: ${(error as Error).message}`);
  }
}

export function loadWalletPublicKey(): { publicKey: PublicKey; keypair?: Keypair } {
  if (config.privateKey) {
    const keypair = loadKeypair();
    return { publicKey: keypair.publicKey, keypair };
  }
  if (config.walletPublicKey) {
    return { publicKey: new PublicKey(config.walletPublicKey) };
  }
  throw new Error("Set SOLANA_PRIVATE_KEY, or WALLET_PUBLIC_KEY for scan-only mode");
}
