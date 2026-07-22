import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import BN from "bn.js";
import { getFlashloanIx } from "@jup-ag/lend/flashloan";
import {
  AddressLookupTableProgram,
  Connection,
  PublicKey,
  Transaction,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import { config } from "../src/config.js";
import { DIRECT_ROUTES, USDC_MINT } from "../src/constants.js";
import { getStableSwapAccountMetas } from "../src/stable.js";
import { loadKeypair } from "../src/wallet.js";

const connection = new Connection(config.rpcUrl, "confirmed");
const payer = loadKeypair();
const flashloan = await getFlashloanIx({
  connection,
  signer: payer.publicKey,
  asset: USDC_MINT,
  amount: new BN(config.minTradeRaw.toString()),
  market: config.flashLoanMarket,
});

const addresses = new Map<string, PublicKey>();
function add(pubkey: PublicKey, isSigner = false): void {
  if (!isSigner && !pubkey.equals(payer.publicKey)) addresses.set(pubkey.toBase58(), pubkey);
}
for (const instruction of [flashloan.borrowIx, flashloan.paybackIx]) {
  for (const key of instruction.keys) add(key.pubkey, key.isSigner);
}
for (const route of DIRECT_ROUTES) {
  const from = route.venueOrder === "stable_first" ? "USDC" : route.token;
  const to = route.venueOrder === "stable_first" ? route.token : "USDC";
  for (const key of getStableSwapAccountMetas(payer.publicKey, from, to)) {
    add(key.pubkey, key.isSigner);
  }
}

const recentSlot = await connection.getSlot("finalized");
const [createInstruction, lookupTableAddress] = AddressLookupTableProgram.createLookupTable({
  authority: payer.publicKey,
  payer: payer.publicKey,
  recentSlot,
});
const createSignature = await sendAndConfirmTransaction(
  connection,
  new Transaction().add(createInstruction),
  [payer],
  { commitment: "confirmed", preflightCommitment: "confirmed" },
);
console.log(`Created ALT ${lookupTableAddress.toBase58()}: ${createSignature}`);

const values = [...addresses.values()];
for (let index = 0; index < values.length; index += 20) {
  const chunk = values.slice(index, index + 20);
  const extendInstruction = AddressLookupTableProgram.extendLookupTable({
    authority: payer.publicKey,
    payer: payer.publicKey,
    lookupTable: lookupTableAddress,
    addresses: chunk,
  });
  const signature = await sendAndConfirmTransaction(
    connection,
    new Transaction().add(extendInstruction),
    [payer],
    { commitment: "confirmed", preflightCommitment: "confirmed" },
  );
  console.log(`Extended ALT with ${chunk.length} addresses: ${signature}`);
}

await mkdir(path.dirname(config.customAltFile), { recursive: true });
await writeFile(
  config.customAltFile,
  `${JSON.stringify(
    {
      address: lookupTableAddress.toBase58(),
      addressCount: values.length,
      createdAt: new Date().toISOString(),
    },
    null,
    2,
  )}\n`,
  "utf8",
);
console.log(`Saved ${config.customAltFile}. Wait one slot before running the bot.`);
