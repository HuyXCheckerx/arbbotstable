#!/usr/bin/env tsx
import {
  AddressLookupTableProgram,
  ComputeBudgetProgram,
  Connection,
  Keypair,
  PublicKey,
  SystemProgram,
  SYSVAR_INSTRUCTIONS_PUBKEY,
  TransactionInstruction,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import { Project0Client, getConfig, AssetTag } from "@0dotxyz/p0-ts-sdk";
import bs58 from "bs58";
import fs from "fs";
import path from "path";
import dotenv from "dotenv";

dotenv.config();

const USDC_MINT = new PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");
const USDT_MINT = new PublicKey("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB");
const PYUSD_MINT = new PublicKey("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo");
const USDG_MINT = new PublicKey("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH");
const STABLE_PROGRAM_ID = new PublicKey("2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG");
const MARGINFI_PROGRAM_ID = new PublicKey("MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA");
const MARGINFI_GROUP_ID = new PublicKey("4qp6pqmr6MuT4vZErFMcrvjxneJW5umHok21cmHYUtsb");

function loadKeypair(secret: string): Keypair {
  let bytes: Uint8Array;
  if (secret.trim().startsWith("[")) {
    const parsed = JSON.parse(secret) as number[];
    bytes = Uint8Array.from(parsed);
  } else {
    bytes = bs58.decode(secret.trim());
  }
  if (bytes.length === 64) return Keypair.fromSecretKey(bytes);
  if (bytes.length === 32) return Keypair.fromSeed(bytes);
  throw new Error(`SOLANA_PRIVATE_KEY decoded to ${bytes.length} bytes; expected 32 or 64`);
}

function tokenProgramForMint(mint: PublicKey): PublicKey {
  return mint.equals(PYUSD_MINT) || mint.equals(USDG_MINT)
    ? TOKEN_2022_PROGRAM_ID
    : TOKEN_PROGRAM_ID;
}

async function sendAndConfirm(
  connection: Connection,
  payer: Keypair,
  instructions: TransactionInstruction[],
): Promise<string> {
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash("confirmed");
  const message = new TransactionMessage({
    payerKey: payer.publicKey,
    recentBlockhash: blockhash,
    instructions: [
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 50_000 }),
      ComputeBudgetProgram.setComputeUnitLimit({ units: 200_000 }),
      ...instructions,
    ],
  }).compileToV0Message();

  const tx = new VersionedTransaction(message);
  tx.sign([payer]);
  const signature = await connection.sendTransaction(tx, {
    skipPreflight: false,
    preflightCommitment: "confirmed",
  });

  const confirmation = await connection.confirmTransaction(
    { signature, blockhash, lastValidBlockHeight },
    "confirmed",
  );
  if (confirmation.value.err) {
    throw new Error(`Transaction failed: ${JSON.stringify(confirmation.value.err)}`);
  }
  return signature;
}

function updateEnvFile(key: string, value: string): void {
  const envPath = path.resolve(process.cwd(), ".env");
  let content = "";
  if (fs.existsSync(envPath)) {
    content = fs.readFileSync(envPath, "utf8");
  }
  const regex = new RegExp(`^${key}=.*$`, "m");
  if (regex.test(content)) {
    content = content.replace(regex, `${key}=${value}`);
  } else {
    content = `${content.trimEnd()}\n${key}=${value}\n`;
  }
  fs.writeFileSync(envPath, content, "utf8");
}

async function main(): Promise<void> {
  const rpcUrl = process.env.SOLANA_RPC_URL;
  if (!rpcUrl) throw new Error("SOLANA_RPC_URL is missing in .env");
  const privateKey = process.env.SOLANA_PRIVATE_KEY;
  if (!privateKey) throw new Error("SOLANA_PRIVATE_KEY is missing in .env");

  const connection = new Connection(rpcUrl, "confirmed");
  const wallet = loadKeypair(privateKey);
  console.log(`Payer Wallet: ${wallet.publicKey.toBase58()}`);

  const balance = await connection.getBalance(wallet.publicKey);
  console.log(`SOL Balance: ${balance / 1e9} SOL`);
  if (balance < 5_000_000) {
    throw new Error("Insufficient SOL balance. At least ~0.005 SOL is required for rent.");
  }

  const accountsToInclude: PublicKey[] = [];

  // 1. System Programs & Sysvars
  accountsToInclude.push(
    SystemProgram.programId,
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
    ComputeBudgetProgram.programId,
    SYSVAR_INSTRUCTIONS_PUBKEY,
  );

  // 2. Stable.com Core
  const [mainState] = PublicKey.findProgramAddressSync([Buffer.from("main_state")], STABLE_PROGRAM_ID);
  const [nativeFeeAccount] = PublicKey.findProgramAddressSync([Buffer.from("native_fee")], STABLE_PROGRAM_ID);
  const [nonceAccount] = PublicKey.findProgramAddressSync([Buffer.from("nonce"), wallet.publicKey.toBuffer()], STABLE_PROGRAM_ID);

  accountsToInclude.push(STABLE_PROGRAM_ID, mainState, nativeFeeAccount, nonceAccount);

  // 3. Supported Tokens & Stable.com Pools / ATAs
  const supportedMints = [USDC_MINT, USDT_MINT, PYUSD_MINT, USDG_MINT];
  for (const mint of supportedMints) {
    accountsToInclude.push(mint);
    const prog = tokenProgramForMint(mint);
    const [poolPda] = PublicKey.findProgramAddressSync([Buffer.from("pool"), mint.toBuffer()], STABLE_PROGRAM_ID);
    accountsToInclude.push(poolPda);
    const poolAta = getAssociatedTokenAddressSync(mint, poolPda, true, prog);
    accountsToInclude.push(poolAta);
    const userAta = getAssociatedTokenAddressSync(mint, wallet.publicKey, false, prog);
    accountsToInclude.push(userAta);
  }

  // 4. Marginfi Core & Banks
  accountsToInclude.push(MARGINFI_PROGRAM_ID, MARGINFI_GROUP_ID);

  if (process.env.SOL_FLASH_ARB_MARGINFI_ACCOUNT) {
    try {
      accountsToInclude.push(new PublicKey(process.env.SOL_FLASH_ARB_MARGINFI_ACCOUNT.trim()));
    } catch {
      // Ignore malformed config
    }
  }

  console.log("Discovering Marginfi banks and client accounts...");
  try {
    const client = await Project0Client.initialize(connection, getConfig("production"));
    for (const mint of [USDC_MINT, PYUSD_MINT]) {
      const banks = client.getBanksByMint(mint, AssetTag.DEFAULT);
      for (const bank of banks) {
        accountsToInclude.push(
          bank.address,
          bank.oracleKey,
          bank.liquidityVault,
          bank.insuranceVault,
          bank.feeVault,
        );
      }
    }
    const userMarginfiAccounts = await client.getAccountAddresses(wallet.publicKey);
    for (const acc of userMarginfiAccounts) {
      accountsToInclude.push(acc);
    }
  } catch (err) {
    console.warn("Marginfi dynamic discovery failed; proceeding with base accounts:", err);
  }

  // 5. Deduplicate and remove wallet itself (signers cannot be indexed via ALT)
  const uniqueMap = new Map<string, PublicKey>();
  for (const acc of accountsToInclude) {
    if (!acc.equals(wallet.publicKey)) {
      uniqueMap.set(acc.toBase58(), acc);
    }
  }
  const finalAddresses = Array.from(uniqueMap.values());
  console.log(`Found ${finalAddresses.length} static harness accounts to compress.`);

  // 6. Check existing ALT or create new
  let lookupTableAddress: PublicKey;
  let addressesToAdd: PublicKey[] = finalAddresses;

  const existingAltEnv = process.env.SOL_FLASH_ARB_CUSTOM_ALT?.trim();
  if (existingAltEnv) {
    try {
      const testPk = new PublicKey(existingAltEnv);
      const existingAlt = await connection.getAddressLookupTable(testPk);
      if (existingAlt.value) {
        lookupTableAddress = testPk;
        console.log(`Using existing lookup table: ${lookupTableAddress.toBase58()}`);
        const existingSet = new Set(existingAlt.value.state.addresses.map((a) => a.toBase58()));
        addressesToAdd = finalAddresses.filter((a) => !existingSet.has(a.toBase58()));
        console.log(`${existingSet.size} addresses already present; ${addressesToAdd.length} missing addresses to add.`);
      } else {
        console.log(`Lookup table ${existingAltEnv} not found on-chain. Creating a fresh table.`);
      }
    } catch {
      console.log("Existing lookup table config invalid; creating a fresh table.");
    }
  }

  const isCreatingNew = !lookupTableAddress!;
  if (isCreatingNew) {
    const currentSlot = await connection.getSlot("finalized");
    const [createIx, newAltAddress] = AddressLookupTableProgram.createLookupTable({
      authority: wallet.publicKey,
      payer: wallet.publicKey,
      recentSlot: currentSlot,
    });
    lookupTableAddress = newAltAddress;
    console.log(`Creating new Address Lookup Table at: ${lookupTableAddress.toBase58()} (recent slot: ${currentSlot})...`);

    // Chunk first 25 addresses into the create transaction
    const firstBatch = addressesToAdd.slice(0, 25);
    const extendIx = AddressLookupTableProgram.extendLookupTable({
      payer: wallet.publicKey,
      authority: wallet.publicKey,
      lookupTable: lookupTableAddress,
      addresses: firstBatch,
    });

    console.log(`Sending creation tx with initial ${firstBatch.length} addresses...`);
    const sig = await sendAndConfirm(connection, wallet, [createIx, extendIx]);
    console.log(`Create & Initial Extend Tx confirmed: ${sig}`);
    addressesToAdd = addressesToAdd.slice(25);
  }

  // Extend remaining addresses in batches of 25
  while (addressesToAdd.length > 0) {
    const batch = addressesToAdd.slice(0, 25);
    addressesToAdd = addressesToAdd.slice(25);
    console.log(`Extending table with next batch of ${batch.length} addresses...`);
    const extendIx = AddressLookupTableProgram.extendLookupTable({
      payer: wallet.publicKey,
      authority: wallet.publicKey,
      lookupTable: lookupTableAddress,
      addresses: batch,
    });
    const sig = await sendAndConfirm(connection, wallet, [extendIx]);
    console.log(`Extend Tx confirmed: ${sig}`);
  }

  console.log("Waiting 2 slots for lookup table activation on-chain...");
  await new Promise((resolve) => setTimeout(resolve, 3000));

  const finalAlt = await connection.getAddressLookupTable(lookupTableAddress);
  const totalCount = finalAlt.value?.state.addresses.length ?? 0;
  console.log(`Lookup table ready with ${totalCount} active accounts!`);

  // Update .env
  updateEnvFile("SOL_FLASH_ARB_CUSTOM_ALT", lookupTableAddress.toBase58());
  console.log(`Updated .env with SOL_FLASH_ARB_CUSTOM_ALT=${lookupTableAddress.toBase58()}`);

  const estimatedSavings = totalCount * 31;
  console.log(`Estimated transaction wire savings: ~${estimatedSavings} bytes!`);
  console.log("Done.");
}

main().catch((err) => {
  console.error("Error creating lookup table:", err);
  process.exit(1);
});
