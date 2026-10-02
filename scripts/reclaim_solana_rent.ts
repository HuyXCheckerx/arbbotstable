import dotenv from "dotenv";
import { Connection, Keypair } from "@solana/web3.js";
import bs58 from "bs58";
import {
  deriveSubAccountKeypair,
  reclaimUnusedSubAccountRents,
  wrapConnectionWithResilientRpc,
} from "../src/engines/solana_flash_arb.js";

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

async function main(): Promise<void> {
  const rpcUrl = process.env.SOLANA_RPC_URL;
  if (!rpcUrl) throw new Error("SOLANA_RPC_URL is missing in .env");

  const privKey = process.env.SOLANA_PRIVATE_KEY;
  if (!privKey) throw new Error("SOLANA_PRIVATE_KEY is missing in .env");

  const masterKeypair = loadKeypair(privKey);
  const fallbacks = (process.env.SOLANA_RPC_FALLBACKS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const connection = wrapConnectionWithResilientRpc(
    new Connection(rpcUrl, "confirmed"),
    fallbacks,
  );

  const args = process.argv.slice(2);
  let maxIndex = 25;
  let includeActive = false;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--max-index" || args[i] === "--max-sub-taker-index") {
      maxIndex = parseInt(args[++i], 10);
    } else if (args[i] === "--include-active") {
      includeActive = true;
    }
  }

  const currentSubTaker = parseInt(process.env.SOL_FLASH_ARB_SUB_TAKER_INDEX || "1", 10);
  const activePubkey = deriveSubAccountKeypair(masterKeypair, currentSubTaker).publicKey.toBase58();

  console.log(`=====================================================================`);
  console.log(`   SOLANA SUB-TAKER RENT RECLAMATION UTILITY`);
  console.log(`=====================================================================`);
  console.log(`Master Wallet: ${masterKeypair.publicKey.toBase58()}`);
  console.log(`Current Active Sub-Taker: #${currentSubTaker} (${activePubkey})`);
  console.log(`Scanning Sub-Taker Range: 1 to ${maxIndex}`);
  console.log(`Include Active: ${includeActive ? "YES" : "NO (active sub-taker is preserved)"}\n`);

  const results = await reclaimUnusedSubAccountRents(connection, masterKeypair, {
    currentSubTakerIndex: currentSubTaker,
    maxIndex,
    includeActive,
    silent: false,
  });

  const totalClosed = results.reduce((sum, r) => sum + r.accountsClosed, 0);
  const totalReclaimedLamports = results.reduce((sum, r) => sum + r.lamportsReclaimed, 0n);
  const totalSol = (Number(totalReclaimedLamports) / 1e9).toFixed(6);

  console.log(`\n=====================================================================`);
  console.log(`Reclamation Summary:`);
  console.log(`  Sub-Takers Cleaned:     ${results.length}`);
  console.log(`  Token Accounts Closed:  ${totalClosed}`);
  console.log(`  Rent Reclaimed:         ${totalSol} SOL`);
  console.log(`=====================================================================`);
}

main().catch((err) => {
  console.error("Fatal error during rent reclamation:", err);
  process.exit(1);
});
