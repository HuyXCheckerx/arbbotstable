import dotenv from "dotenv";
import {
  Connection,
  Keypair,
  PublicKey,
  TransactionInstruction,
  VersionedTransaction,
  ComputeBudgetProgram,
} from "@solana/web3.js";
import {
  TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import { MarginfiAccountWrapper, MarginfiClient } from "@mrgnlabs/marginfi-client-v2";
import bs58 from "bs58";
import fetch from "node-fetch";

dotenv.config();

const PYUSD_MINT = new PublicKey("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo");
const USDG_MINT = new PublicKey("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH");
const STABLE_PROGRAM_ID = new PublicKey("2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG");

function loadKeypair(secret: string): Keypair {
  secret = secret.trim();
  if (secret.startsWith("[")) {
    return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(secret)));
  }
  const decoded = bs58.decode(secret);
  return decoded.length === 64 ? Keypair.fromSecretKey(decoded) : Keypair.fromSeed(decoded);
}

async function main() {
  const rpcUrl = process.env.SOLANA_RPC_URL!;
  const keypair = loadKeypair(process.env.SOLANA_PRIVATE_KEY!);
  const connection = new Connection(rpcUrl, "confirmed");

  console.log(`=== SOLANA ARB: Borrow PYUSD -> Stable.com (USDG) -> Jupiter (PYUSD) ===`);
  console.log(`Wallet: ${keypair.publicKey.toBase58()}`);

  const loanAmount = 10000;
  const loanAmountRaw = BigInt(loanAmount * 1e6);

  // 1. Get Stable.com status and order for PYUSD -> USDG
  console.log(`\n[1/3] Quoting Stable.com (10,000 PYUSD -> USDG)...`);
  const stableStatusRes = await fetch("https://api-defi.stable.com/swap/status", {
    method: "POST",
    headers: {
      "accept": "application/json",
      "content-type": "application/json",
      "origin": "https://stable.com",
      "referer": "https://stable.com/",
    },
    body: JSON.stringify({
      chainFrom: "102",
      assetFrom: "PYUSD",
      chainTo: "102",
      assetTo: "USDG",
      gasLess: false,
      amountFrom: "10000",
      addressFrom: keypair.publicKey.toBase58(),
      addressTo: keypair.publicKey.toBase58(),
    }),
  });

  const stableStatus = (await stableStatusRes.json()) as any;
  console.log(`Stable.com quote status: ${stableStatusRes.status}`);
  const stableOutUi = stableStatus?.asset?.amountTo || "10000";
  console.log(`Stable.com Output: ${stableOutUi} USDG`);

  // 2. Get Jupiter Quote for USDG -> PYUSD
  console.log(`\n[2/3] Quoting Jupiter (10,000 USDG -> PYUSD)...`);
  const jupRes = await fetch(
    `https://api.jup.ag/swap/v1/quote?inputMint=${USDG_MINT.toBase58()}&outputMint=${PYUSD_MINT.toBase58()}&amount=${loanAmountRaw}&slippageBps=0&restrictIntermediateTokens=true`
  );
  const jupQuote = (await jupRes.json()) as any;
  const jupOutRaw = BigInt(jupQuote.outAmount);
  const jupOutUi = Number(jupOutRaw) / 1e6;
  const grossProfitUi = jupOutUi - loanAmount;

  console.log(`Jupiter Expected Output: ${jupOutUi.toFixed(6)} PYUSD`);
  console.log(`Gross Spread: +${grossProfitUi.toFixed(6)} PYUSD`);

  if (grossProfitUi <= 0) {
    console.log(`Gross profit is negative (${grossProfitUi.toFixed(6)} PYUSD). Aborting.`);
    return;
  }

  console.log(`\n[3/3] Ready for execution. (Gross Profit: +${grossProfitUi.toFixed(6)} PYUSD)`);
}

main().catch(console.error);
