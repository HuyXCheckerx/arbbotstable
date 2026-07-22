import { Connection } from "@solana/web3.js";
import { initializeWalletAtas } from "../src/accounts.js";
import { config } from "../src/config.js";
import { loadKeypair } from "../src/wallet.js";

const connection = new Connection(config.rpcUrl, "confirmed");
const payer = loadKeypair();
const signature = await initializeWalletAtas(connection, payer);
if (signature) {
  console.log(`Created missing token accounts: ${signature}`);
} else {
  console.log("All required token accounts already exist.");
}
