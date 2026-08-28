import dotenv from "dotenv";
import {
  Connection,
  Keypair,
  PublicKey,
  Transaction,
  ComputeBudgetProgram,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import {
  TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  getAssociatedTokenAddressSync,
  createAssociatedTokenAccountIdempotentInstruction,
} from "@solana/spl-token";
import bs58 from "bs58";

dotenv.config();

function loadKeypair(secret: string): Keypair {
  secret = secret.trim();
  if (secret.startsWith("[")) {
    const parsed = JSON.parse(secret) as number[];
    return Keypair.fromSecretKey(Uint8Array.from(parsed));
  }
  const decoded = bs58.decode(secret);
  if (decoded.length === 64) return Keypair.fromSecretKey(decoded);
  if (decoded.length === 32) return Keypair.fromSeed(decoded);
  throw new Error(`Invalid private key length: ${decoded.length}`);
}

async function main() {
  const rpcUrl = process.env.SOLANA_RPC_URL;
  if (!rpcUrl) throw new Error("SOLANA_RPC_URL is missing in .env");

  const privKey = process.env.SOLANA_PRIVATE_KEY;
  if (!privKey) throw new Error("SOLANA_PRIVATE_KEY is missing in .env");

  const payer = loadKeypair(privKey);
  const connection = new Connection(rpcUrl, "confirmed");

  console.log(`Wallet address: ${payer.publicKey.toBase58()}`);

  const tokens = [
    {
      symbol: "USDC",
      mint: new PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
      programId: TOKEN_PROGRAM_ID,
    },
    {
      symbol: "PYUSD",
      mint: new PublicKey("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"),
      programId: TOKEN_2022_PROGRAM_ID,
    },
    {
      symbol: "USDG",
      mint: new PublicKey("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"),
      programId: TOKEN_2022_PROGRAM_ID,
    },
  ];

  const instructions = [];

  for (const token of tokens) {
    const ata = getAssociatedTokenAddressSync(
      token.mint,
      payer.publicKey,
      false,
      token.programId
    );
    const info = await connection.getAccountInfo(ata);
    if (!info) {
      console.log(`Missing ATA for ${token.symbol}: ${ata.toBase58()} -> Adding creation instruction...`);
      instructions.push(
        createAssociatedTokenAccountIdempotentInstruction(
          payer.publicKey,
          ata,
          payer.publicKey,
          token.mint,
          token.programId
        )
      );
    } else {
      console.log(`ATA already exists for ${token.symbol}: ${ata.toBase58()}`);
    }
  }

  if (instructions.length === 0) {
    console.log("All token accounts already exist.");
    return;
  }

  const tx = new Transaction().add(
    ComputeBudgetProgram.setComputeUnitLimit({ units: 100_000 }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 200_000 }),
    ...instructions,
  );
  console.log("Sending transaction to create missing token accounts...");
  const signature = await sendAndConfirmTransaction(connection, tx, [payer], {
    commitment: "confirmed",
  });

  console.log(`Success! Transaction signature: ${signature}`);
}

main().catch((err) => {
  console.error("Error creating token accounts:", err);
  process.exit(1);
});
