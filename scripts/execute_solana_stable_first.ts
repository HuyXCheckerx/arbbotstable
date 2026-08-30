import dotenv from "dotenv";
import {
  Connection,
  Keypair,
  PublicKey,
  TransactionInstruction,
  ComputeBudgetProgram,
  SystemProgram,
  AddressLookupTableAccount,
} from "@solana/web3.js";
import {
  TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  ASSOCIATED_TOKEN_PROGRAM_ID,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import { Project0Client, getConfig, AssetTag } from "@0dotxyz/p0-ts-sdk";
import bs58 from "bs58";
import fetch from "node-fetch";
import { createHash, randomUUID } from "crypto";

dotenv.config();

const PYUSD_MINT = new PublicKey("2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo");
const USDG_MINT = new PublicKey("2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH");
const STABLE_PROGRAM_ID = new PublicKey("2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG");
const SINGLE_CHAIN_SWAP_DISCRIMINATOR = createHash("sha256")
  .update("global:single_chain_swap")
  .digest()
  .subarray(0, 8);

function loadKeypair(secret: string): Keypair {
  secret = secret.trim();
  if (secret.startsWith("[")) {
    return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(secret)));
  }
  const decoded = bs58.decode(secret);
  return decoded.length === 64 ? Keypair.fromSecretKey(decoded) : Keypair.fromSeed(decoded);
}

function unsignedLe(value: bigint): Buffer {
  const buffer = Buffer.alloc(8);
  buffer.writeBigUInt64LE(value);
  return buffer;
}

function signedLe(value: bigint): Buffer {
  const buffer = Buffer.alloc(8);
  buffer.writeBigInt64LE(value);
  return buffer;
}

function integerField(value: number | string | undefined, name: string): bigint {
  if (value === undefined || !/^\d+$/.test(String(value))) {
    throw new Error(`Stable.com order has invalid ${name}`);
  }
  return BigInt(value);
}

function buildStableSwapInstruction(
  wallet: PublicKey,
  inputRaw: bigint,
  expectedOutputRaw: bigint,
  order: any,
  intermediateMint: PublicKey = PYUSD_MINT,
  outputMint: PublicKey = USDG_MINT,
): TransactionInstruction {
  const signatureHex = order.maintainerSignature?.replace(/^0x/, "");
  const signatureBytes = Buffer.from(signatureHex, "hex");
  const maintainerSignature = signatureBytes.subarray(0, 64);
  const recoveryId = signatureBytes.length === 65 ? signatureBytes[64] : Number(order.recoveryId ?? 0);

  const nonce = integerField(order.nonce, "nonce");
  const deadline = integerField(order.deadline, "deadline");
  const executionFeeLamports = integerField(order.executionFeeNative ?? order.nativeFee ?? 0, "executionFeeNative");

  const [mainState] = PublicKey.findProgramAddressSync([Buffer.from("main_state")], STABLE_PROGRAM_ID);
  const [nonceAccount] = PublicKey.findProgramAddressSync([Buffer.from("nonce"), wallet.toBuffer()], STABLE_PROGRAM_ID);
  const [nativeFeeAccount] = PublicKey.findProgramAddressSync([Buffer.from("native_fee")], STABLE_PROGRAM_ID);
  const [inputPool] = PublicKey.findProgramAddressSync([Buffer.from("pool"), intermediateMint.toBuffer()], STABLE_PROGRAM_ID);
  const [outputPool] = PublicKey.findProgramAddressSync([Buffer.from("pool"), outputMint.toBuffer()], STABLE_PROGRAM_ID);

  const inputTokenProgram = TOKEN_2022_PROGRAM_ID;
  const outputTokenProgram = TOKEN_2022_PROGRAM_ID;

  const userInputAta = getAssociatedTokenAddressSync(intermediateMint, wallet, false, inputTokenProgram);
  const userOutputAta = getAssociatedTokenAddressSync(outputMint, wallet, false, outputTokenProgram);
  const poolInputAta = getAssociatedTokenAddressSync(intermediateMint, inputPool, true, inputTokenProgram);
  const poolOutputAta = getAssociatedTokenAddressSync(outputMint, outputPool, true, outputTokenProgram);

  const data = Buffer.concat([
    SINGLE_CHAIN_SWAP_DISCRIMINATOR,
    unsignedLe(inputRaw),
    unsignedLe(executionFeeLamports),
    maintainerSignature,
    unsignedLe(nonce),
    signedLe(deadline),
    Buffer.from([recoveryId]),
  ]);

  return new TransactionInstruction({
    programId: STABLE_PROGRAM_ID,
    keys: [
      { pubkey: wallet, isSigner: true, isWritable: true },
      { pubkey: wallet, isSigner: true, isWritable: true },
      { pubkey: nonceAccount, isSigner: false, isWritable: true },
      { pubkey: mainState, isSigner: false, isWritable: false },
      { pubkey: nativeFeeAccount, isSigner: false, isWritable: true },
      { pubkey: inputPool, isSigner: false, isWritable: true },
      { pubkey: intermediateMint, isSigner: false, isWritable: false },
      { pubkey: poolInputAta, isSigner: false, isWritable: true },
      { pubkey: userInputAta, isSigner: false, isWritable: true },
      { pubkey: outputPool, isSigner: false, isWritable: true },
      { pubkey: outputMint, isSigner: false, isWritable: false },
      { pubkey: poolOutputAta, isSigner: false, isWritable: true },
      { pubkey: wallet, isSigner: false, isWritable: false },
      { pubkey: userOutputAta, isSigner: false, isWritable: true },
      { pubkey: inputTokenProgram, isSigner: false, isWritable: false },
      { pubkey: outputTokenProgram, isSigner: false, isWritable: false },
      { pubkey: ASSOCIATED_TOKEN_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
    ],
    data,
  });
}

function decodeJupiterInstruction(instruction: any): TransactionInstruction {
  return new TransactionInstruction({
    programId: new PublicKey(instruction.programId),
    keys: instruction.accounts.map((account: any) => ({
      pubkey: new PublicKey(account.pubkey),
      isSigner: Boolean(account.isSigner),
      isWritable: Boolean(account.isWritable),
    })),
    data: Buffer.from(instruction.data, "base64"),
  });
}

function wrapConnectionWithResilientBatchRequest(connection: Connection): Connection {
  const fallbackUrls = [
    connection.rpcEndpoint,
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
  ].filter((u, i, arr) => arr.indexOf(u) === i);

  (connection as any)._rpcBatchRequest = async (requests: any[]) => {
    let lastError: any;
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const url = fallbackUrls[attempt % fallbackUrls.length];
      try {
        const results = await Promise.all(
          requests.map(async (req) => {
            const body = {
              jsonrpc: "2.0",
              id: Math.floor(Math.random() * 1e9),
              method: req.methodName,
              params: req.args,
            };
            const res = await fetch(url, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            if (res.status === 429) {
              throw new Error(`HTTP 429 rate limit on ${url}`);
            }
            return await res.json();
          }),
        );
        if (Array.isArray(results) && results.length === requests.length) {
          return results;
        }
      } catch (err) {
        lastError = err;
      }
      await new Promise((r) => setTimeout(r, (attempt + 1) * 400));
    }
    throw lastError || new Error("Failed to fetch account infos after retries");
  };

  const origGetMultiple = connection.getMultipleAccountsInfo.bind(connection);
  connection.getMultipleAccountsInfo = async (publicKeys: PublicKey[], config?: any) => {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const url = fallbackUrls[attempt % fallbackUrls.length];
      try {
        const body = {
          jsonrpc: "2.0",
          id: Math.floor(Math.random() * 1e9),
          method: "getMultipleAccounts",
          params: [
            publicKeys.map((p) => p.toBase58()),
            { commitment: config?.commitment ?? "confirmed", encoding: "base64" },
          ],
        };
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (res.status === 429) throw new Error("HTTP 429");
        const json = (await res.json()) as any;
        if (json.result && Array.isArray(json.result.value)) {
          return json.result.value.map((val: any) => {
            if (!val) return null;
            return {
              data: Buffer.from(val.data[0], "base64"),
              executable: val.executable,
              lamports: val.lamports,
              owner: new PublicKey(val.owner),
              rentEpoch: val.rentEpoch,
            };
          });
        }
      } catch (err) {
        await new Promise((r) => setTimeout(r, (attempt + 1) * 500));
      }
    }
    return origGetMultiple(publicKeys, config);
  };

  return connection;
}

async function main() {
  const rpcUrl = process.env.SOLANA_RPC_URL && !process.env.SOLANA_RPC_URL.includes("api.mainnet-beta.solana.com")
    ? process.env.SOLANA_RPC_URL
    : "https://solana-rpc.publicnode.com";
  const keypair = loadKeypair(process.env.SOLANA_PRIVATE_KEY!);
  const connection = wrapConnectionWithResilientBatchRequest(new Connection(rpcUrl, "confirmed"));

  console.log(`=== EXECUTING ATOMIC ARB: Marginfi Flash PYUSD -> Stable.com USDG -> Jupiter PYUSD ===`);
  console.log(`Wallet: ${keypair.publicKey.toBase58()}`);

  const loanAmount = 10000;
  const loanAmountRaw = BigInt(loanAmount * 1e6);

  // Initialize Marginfi client
  const client = await Project0Client.initialize(connection, getConfig("production"));
  const configuredAccount = process.env.SOL_FLASH_ARB_MARGINFI_ACCOUNT?.trim();
  const account = configuredAccount
    ? await client.fetchAccount(new PublicKey(configuredAccount), true)
    : (await client.fetchAccountsForAuthority(keypair.publicKey))[0];

  if (!account) throw new Error("No Marginfi account found");
  const pyusdBanks = client.getBanksByMint(PYUSD_MINT, AssetTag.DEFAULT);
  const pyusdBank = pyusdBanks[0];
  if (!pyusdBank) throw new Error("PYUSD bank not found on Marginfi");

  console.log(`Using Marginfi Account: ${account.address.toBase58()}`);

  // Step 1: Create Stable.com SingleChain order
  console.log(`\n[1/4] Creating Stable.com signed order (10,000 PYUSD -> USDG)...`);
  const stableOrderRes = await fetch("https://api-defi.stable.com/swap/create/singleChain", {
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
      amountTo: "10000",
      addressFrom: keypair.publicKey.toBase58(),
      addressTo: keypair.publicKey.toBase58(),
      device: randomUUID(),
    }),
  });

  const stableOrderData = (await stableOrderRes.json()) as any;
  const stableOrder = stableOrderData.maintainerSignature ? stableOrderData : stableOrderData.data;
  if (!stableOrder?.maintainerSignature) {
    throw new Error(`Stable.com order creation failed: ${JSON.stringify(stableOrderData)}`);
  }
  console.log(`Stable.com order signed! Order ID: ${stableOrder.orderId || "OK"}`);

  const stableIx = buildStableSwapInstruction(
    keypair.publicKey,
    loanAmountRaw,
    loanAmountRaw,
    stableOrder,
    PYUSD_MINT,
    USDG_MINT
  );

  // Step 2: Jupiter Quote & Swap Instructions
  console.log(`\n[2/4] Fetching Jupiter quote & swap instructions (10,000 USDG -> PYUSD)...`);
  const jupQuoteRes = await fetch(
    `https://api.jup.ag/swap/v1/quote?inputMint=${USDG_MINT.toBase58()}&outputMint=${PYUSD_MINT.toBase58()}&amount=${loanAmountRaw}&slippageBps=0&restrictIntermediateTokens=true&onlyDirectRoutes=true&maxAccounts=14`
  );
  const jupQuote = (await jupQuoteRes.json()) as any;
  const expectedOutRaw = BigInt(jupQuote.outAmount);
  const profitRaw = expectedOutRaw - loanAmountRaw;
  const profitUi = Number(profitRaw) / 1e6;

  console.log(`Expected Jupiter Return: ${(Number(expectedOutRaw) / 1e6).toFixed(6)} PYUSD`);
  console.log(`Gross Profit: +${profitUi.toFixed(6)} PYUSD`);

  const jupIxRes = await fetch("https://api.jup.ag/swap/v1/swap-instructions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      userPublicKey: keypair.publicKey.toBase58(),
      payer: keypair.publicKey.toBase58(),
      quoteResponse: jupQuote,
      wrapAndUnwrapSol: false,
      useSharedAccounts: true,
      dynamicComputeUnitLimit: false,
      skipUserAccountsRpcCalls: false,
    }),
  });

  const jupIxData = (await jupIxRes.json()) as any;
  if (jupIxData.error || !jupIxData.swapInstruction) {
    throw new Error(`Jupiter swap instructions failed: ${JSON.stringify(jupIxData)}`);
  }

  const jupInstructions = [
    ...(jupIxData.setupInstructions ?? []),
    ...(jupIxData.otherInstructions ?? []),
    jupIxData.swapInstruction,
    ...(jupIxData.cleanupInstruction ? [jupIxData.cleanupInstruction] : []),
  ].map(decodeJupiterInstruction);

  // Step 3: Fetch ALTs & build flash loan
  console.log(`\n[3/4] Building atomic Flash Loan transaction...`);
  const altAddresses = (jupIxData.addressLookupTableAddresses ?? []).map((a: string) => new PublicKey(a));
  const altAccounts = (
    await Promise.all(altAddresses.map((a: PublicKey) => connection.getAddressLookupTable(a)))
  )
    .map((r) => r.value)
    .filter(Boolean) as AddressLookupTableAccount[];

  const combinedAlts = [
    ...new Map([...(client.addressLookupTables ?? []), ...altAccounts].map((t) => [t.key.toBase58(), t])).values(),
  ];

  const borrowIx = await account.makeBorrowIx(pyusdBank.address, "10000", {
    createAtas: false,
    wrapAndUnwrapSol: false,
    overrideInferAccounts: {
      authority: keypair.publicKey,
      group: account.group,
    },
  });

  const repayIx = await account.makeRepayIx(pyusdBank.address, "10000", true, {
    wrapAndUnwrapSol: false,
    overrideInferAccounts: {
      authority: keypair.publicKey,
      group: account.group,
    },
  });

  const latest = await connection.getLatestBlockhash("confirmed");
  const flashTx = await account.makeFlashLoanTx({
    bankMap: client.bankMap,
    ixs: [
      ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }),
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 10_000 }),
      ...borrowIx.instructions,
      stableIx,
      ...jupInstructions,
      ...repayIx.instructions,
    ],
    signers: [],
    blockhash: latest.blockhash,
    addressLookupTableAccounts: combinedAlts,
  });

  flashTx.sign([keypair]);

  // Step 4: Simulate on-chain
  console.log(`\n[4/4] Simulating transaction on Solana mainnet...`);
  const sim = await connection.simulateTransaction(flashTx, {
    commitment: "confirmed",
    sigVerify: true,
  });

  if (sim.value.err) {
    console.error("Simulation failed:", JSON.stringify(sim.value.err));
    console.error("Logs:", sim.value.logs?.slice(-20).join("\n"));
    return;
  }

  console.log(` Simulation SUCCESS! Consumed ${sim.value.unitsConsumed} compute units.`);
  console.log(`Broadcasting transaction to Solana mainnet...`);

  const signature = await connection.sendRawTransaction(flashTx.serialize(), {
    skipPreflight: false,
    preflightCommitment: "confirmed",
  });

  console.log(`\n======================================================`);
  console.log(`TRANSACTION SENT!`);
  console.log(`Signature: ${signature}`);
  console.log(`Solscan: https://solscan.io/tx/${signature}`);
  console.log(`======================================================`);

  const confirmation = await connection.confirmTransaction(
    {
      signature,
      blockhash: latest.blockhash,
      lastValidBlockHeight: latest.lastValidBlockHeight,
    },
    "confirmed"
  );

  if (confirmation.value.err) {
    console.error("Transaction confirmation failed:", confirmation.value.err);
  } else {
    console.log(`TRANSACTION CONFIRMED! Profit captured: +${profitUi.toFixed(6)} PYUSD`);
  }
}

main().catch((err) => {
  console.error("Execution error:", err);
  process.exit(1);
});
