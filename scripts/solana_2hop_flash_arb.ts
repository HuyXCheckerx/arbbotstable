import "dotenv/config";

import { createHash, randomUUID } from "node:crypto";
import {
  AssetTag,
  Bank,
  MarginfiAccountWrapper,
  Project0Client,
  getConfig,
} from "@0dotxyz/p0-ts-sdk";
import {
  AddressLookupTableAccount,
  ComputeBudgetProgram,
  Connection,
  Keypair,
  PublicKey,
  SystemProgram,
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
// @ts-ignore
import BN from "bn.js";
import bs58 from "bs58";
import fetch from "node-fetch";

export const USDC_MINT = new PublicKey(
  "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
);
export const USDT_MINT = new PublicKey(
  "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
);
export const PYUSD_MINT = new PublicKey(
  "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
);
export const USDG_MINT = new PublicKey(
  "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",
);
export const STABLE_PROGRAM_ID = new PublicKey(
  "2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG",
);
const MAX_WIRE_TRANSACTION_BYTES = 1232;
const JUPITER_API_BASE = "https://api.jup.ag/swap/v1";
const JUPITER_LITE_API_BASE = "https://lite-api.jup.ag/swap/v1";
const SINGLE_CHAIN_SWAP_DISCRIMINATOR = createHash("sha256")
  .update("global:single_chain_swap")
  .digest()
  .subarray(0, 8);

interface JupiterQuote {
  inputMint: string;
  outputMint: string;
  inAmount: string;
  outAmount: string;
  otherAmountThreshold: string;
  swapMode: string;
  slippageBps: number;
  routePlan: unknown[];
  [key: string]: unknown;
}

interface JupiterInstructionJson {
  programId: string;
  accounts: Array<{
    pubkey: string;
    isSigner: boolean;
    isWritable: boolean;
  }>;
  data: string;
}

interface JupiterSwapInstructions {
  computeBudgetInstructions?: JupiterInstructionJson[];
  setupInstructions?: JupiterInstructionJson[];
  swapInstruction: JupiterInstructionJson;
  cleanupInstruction?: JupiterInstructionJson | null;
  otherInstructions?: JupiterInstructionJson[];
  addressLookupTableAddresses?: string[];
  [key: string]: unknown;
}

interface StableStatusAsset {
  asset: string;
  precision: number;
  balance: number;
  min: number;
  max: number;
  nativeFee: number;
  tokenFee: number;
  amountFrom: string;
  amountTo: string;
  executionFeeNative: string;
  [key: string]: unknown;
}

interface StableOrder {
  maintainerSignature: string;
  recoveryId?: number | string;
  nonce: number | string;
  deadline: number | string;
  executionFeeNative?: number | string;
  nativeFee?: number | string;
  amountFrom?: string;
  amountTo?: string;
  [key: string]: unknown;
}

interface StableLeg {
  instruction: TransactionInstruction;
  outputRaw: bigint;
  executionFeeLamports: bigint;
  nonce: bigint;
  deadline: bigint;
}

interface SwapLeg {
  instructions: TransactionInstruction[];
  lookupTableAddresses: PublicKey[];
  ignoredComputeBudgetInstructionCount: number;
}

function loadKeypair(secret: string): Keypair {
  secret = secret.trim();
  if (secret.startsWith("[")) {
    return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(secret)));
  }
  const decoded = bs58.decode(secret);
  return decoded.length === 64
    ? Keypair.fromSecretKey(decoded)
    : Keypair.fromSeed(decoded);
}

function formatRaw(value: bigint, decimals = 6): string {
  const negative = value < 0n;
  const absolute = negative ? -value : value;
  const scale = 10n ** BigInt(decimals);
  const whole = absolute / scale;
  const fraction = (absolute % scale).toString().padStart(decimals, "0");
  const trimmed = fraction.replace(/0+$/, "");
  return `${negative ? "-" : ""}${whole}${trimmed ? `.${trimmed}` : ""}`;
}

function parseDecimalFloor(value: string, decimals = 6): bigint {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) throw new Error(`Invalid token amount: ${value}`);
  const fraction = (match[2] ?? "").slice(0, decimals).padEnd(decimals, "0");
  return BigInt(match[1]) * 10n ** BigInt(decimals) + BigInt(fraction || "0");
}

function parseDecimalCeil(value: string, decimals = 6): bigint {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) throw new Error(`Invalid token amount: ${value}`);
  const fraction = (match[2] ?? "").slice(0, decimals).padEnd(decimals, "0");
  const discarded = (match[2] ?? "").slice(decimals);
  let raw = BigInt(match[1]) * 10n ** BigInt(decimals) + BigInt(fraction || "0");
  if (/[1-9]/.test(discarded)) raw += 1n;
  return raw;
}

function tokenProgramForMint(mint: PublicKey): PublicKey {
  return mint.equals(PYUSD_MINT) || mint.equals(USDG_MINT)
    ? TOKEN_2022_PROGRAM_ID
    : TOKEN_PROGRAM_ID;
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
  order: StableOrder,
  inputMint: PublicKey,
  outputMint: PublicKey,
): StableLeg {
  const signatureHex = order.maintainerSignature?.replace(/^0x/, "");
  const signatureBytes = Buffer.from(signatureHex, "hex");
  const maintainerSignature = signatureBytes.subarray(0, 64);
  const recoveryId =
    signatureBytes.length === 65
      ? signatureBytes[64]
      : Number(order.recoveryId ?? 0);

  const nonce = integerField(order.nonce, "nonce");
  const deadline = integerField(order.deadline, "deadline");
  const executionFeeLamports = integerField(
    order.executionFeeNative ?? order.nativeFee ?? 0,
    "executionFeeNative",
  );

  const [mainState] = PublicKey.findProgramAddressSync(
    [Buffer.from("main_state")],
    STABLE_PROGRAM_ID,
  );
  const [nonceAccount] = PublicKey.findProgramAddressSync(
    [Buffer.from("nonce"), wallet.toBuffer()],
    STABLE_PROGRAM_ID,
  );
  const [nativeFeeAccount] = PublicKey.findProgramAddressSync(
    [Buffer.from("native_fee")],
    STABLE_PROGRAM_ID,
  );
  const [inputPool] = PublicKey.findProgramAddressSync(
    [Buffer.from("pool"), inputMint.toBuffer()],
    STABLE_PROGRAM_ID,
  );
  const [outputPool] = PublicKey.findProgramAddressSync(
    [Buffer.from("pool"), outputMint.toBuffer()],
    STABLE_PROGRAM_ID,
  );

  const inputTokenProgram = tokenProgramForMint(inputMint);
  const outputTokenProgram = tokenProgramForMint(outputMint);

  const userInputAta = getAssociatedTokenAddressSync(
    inputMint,
    wallet,
    false,
    inputTokenProgram,
  );
  const userOutputAta = getAssociatedTokenAddressSync(
    outputMint,
    wallet,
    false,
    outputTokenProgram,
  );
  const poolInputAta = getAssociatedTokenAddressSync(
    inputMint,
    inputPool,
    true,
    inputTokenProgram,
  );
  const poolOutputAta = getAssociatedTokenAddressSync(
    outputMint,
    outputPool,
    true,
    outputTokenProgram,
  );

  const data = Buffer.concat([
    SINGLE_CHAIN_SWAP_DISCRIMINATOR,
    unsignedLe(inputRaw),
    unsignedLe(executionFeeLamports),
    maintainerSignature,
    unsignedLe(nonce),
    signedLe(deadline),
    Buffer.from([recoveryId]),
  ]);

  const instruction = new TransactionInstruction({
    programId: STABLE_PROGRAM_ID,
    keys: [
      { pubkey: wallet, isSigner: true, isWritable: true },
      { pubkey: wallet, isSigner: true, isWritable: true },
      { pubkey: nonceAccount, isSigner: false, isWritable: true },
      { pubkey: mainState, isSigner: false, isWritable: false },
      { pubkey: nativeFeeAccount, isSigner: false, isWritable: true },
      { pubkey: inputPool, isSigner: false, isWritable: true },
      { pubkey: inputMint, isSigner: false, isWritable: false },
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

  return {
    instruction,
    outputRaw: expectedOutputRaw,
    executionFeeLamports,
    nonce,
    deadline,
  };
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

async function fetchJson<T>(url: string, init?: any): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`HTTP ${res.status} from ${url}: ${text.slice(0, 300)}`);
  }
  return (await res.json()) as T;
}

async function getJupiterQuote(
  apiBase: string,
  apiKey: string | undefined,
  inputMint: PublicKey,
  outputMint: PublicKey,
  amountRaw: bigint,
  maxAccounts = 7,
): Promise<JupiterQuote> {
  const params = new URLSearchParams({
    inputMint: inputMint.toBase58(),
    outputMint: outputMint.toBase58(),
    amount: amountRaw.toString(),
    swapMode: "ExactIn",
    slippageBps: "0",
    onlyDirectRoutes: "true",
    maxAccounts: String(maxAccounts),
  });
  const headers: Record<string, string> = { accept: "application/json" };
  if (apiKey) headers["x-api-key"] = apiKey;

  try {
    return await fetchJson<JupiterQuote>(`${apiBase}/quote?${params}`, { headers });
  } catch (err) {
    if (apiBase !== JUPITER_LITE_API_BASE) {
      return await fetchJson<JupiterQuote>(`${JUPITER_LITE_API_BASE}/quote?${params}`);
    }
    throw err;
  }
}

async function getJupiterSwapInstructions(
  apiBase: string,
  apiKey: string | undefined,
  quote: JupiterQuote,
  userPublicKey: PublicKey,
): Promise<JupiterSwapInstructions> {
  const headers: Record<string, string> = {
    accept: "application/json",
    "content-type": "application/json",
  };
  if (apiKey) headers["x-api-key"] = apiKey;

  const body = JSON.stringify({
    quoteResponse: quote,
    userPublicKey: userPublicKey.toBase58(),
    payer: userPublicKey.toBase58(),
    wrapAndUnwrapSol: false,
    useSharedAccounts: false,
    dynamicComputeUnitLimit: false,
    skipUserAccountsRpcCalls: false,
  });

  try {
    return await fetchJson<JupiterSwapInstructions>(`${apiBase}/swap-instructions`, {
      method: "POST",
      headers,
      body,
    });
  } catch (err) {
    if (apiBase !== JUPITER_LITE_API_BASE) {
      return await fetchJson<JupiterSwapInstructions>(
        `${JUPITER_LITE_API_BASE}/swap-instructions`,
        { method: "POST", headers: { "content-type": "application/json" }, body },
      );
    }
    throw err;
  }
}

function decodeJupiterInstruction(
  instruction: JupiterInstructionJson,
): TransactionInstruction {
  return new TransactionInstruction({
    programId: new PublicKey(instruction.programId),
    keys: instruction.accounts.map((account) => ({
      pubkey: new PublicKey(account.pubkey),
      isSigner: account.isSigner,
      isWritable: account.isWritable,
    })),
    data: Buffer.from(instruction.data, "base64"),
  });
}

function buildSwapLeg(response: JupiterSwapInstructions): SwapLeg {
  const instructions: TransactionInstruction[] = [];
  let ignoredComputeBudget = 0;

  for (const group of [
    response.setupInstructions,
    [response.swapInstruction],
    response.cleanupInstruction ? [response.cleanupInstruction] : undefined,
    response.otherInstructions,
  ]) {
    if (!group) continue;
    for (const item of group) {
      if (item.programId === ComputeBudgetProgram.programId.toBase58()) {
        ignoredComputeBudget += 1;
        continue;
      }
      instructions.push(decodeJupiterInstruction(item));
    }
  }

  return {
    instructions,
    lookupTableAddresses: (response.addressLookupTableAddresses ?? []).map(
      (address) => new PublicKey(address),
    ),
    ignoredComputeBudgetInstructionCount: ignoredComputeBudget,
  };
}

async function fetchLookupTables(
  connection: Connection,
  addresses: PublicKey[],
): Promise<AddressLookupTableAccount[]> {
  const unique = [
    ...new Map(addresses.map((addr) => [addr.toBase58(), addr])).values(),
  ];
  if (!unique.length) return [];
  const responses = await Promise.all(
    unique.map(async (address) => ({
      address,
      result: await connection.getAddressLookupTable(address),
    })),
  );
  return responses
    .filter((entry) => entry.result.value !== null)
    .map((entry) => entry.result.value as AddressLookupTableAccount);
}

async function main() {
  const args = process.argv.slice(2);
  const send = args.includes("--send");
  const confirmArg = args[args.indexOf("--confirm-mainnet") + 1];

  const configuredRpc =
    process.env.SOL_FLASH_ARB_RPC_URL ||
    process.env.SOLANA_RPC_URL ||
    "https://api.mainnet-beta.solana.com";
  
  const connection = wrapConnectionWithResilientBatchRequest(
    new Connection(configuredRpc, "confirmed")
  );

  const secret = process.env.SOL_FLASH_ARB_PRIVATE_KEY || process.env.SOLANA_PRIVATE_KEY;
  if (!secret) throw new Error("Missing SOL_FLASH_ARB_PRIVATE_KEY or SOLANA_PRIVATE_KEY");
  const keypair = loadKeypair(secret);
  const wallet = keypair.publicKey;

  console.log(`Wallet: ${wallet.toBase58()}`);
  console.log(`2-Hop Atomic Route: PYUSD -> USDG (Stable.com) -> USDC (Jupiter Direct) -> PYUSD (Jupiter Direct)`);

  const jupApiKey = process.env.SOL_FLASH_ARB_JUPITER_API_KEY;
  const jupBase = jupApiKey ? JUPITER_API_BASE : JUPITER_LITE_API_BASE;
  const stableBase =
    process.env.SOL_FLASH_ARB_STABLE_API_BASE || "https://api-defi.stable.com";

  // Marginfi client setup
  const p0Config = getConfig("production");
  const client = await Project0Client.initialize(connection, p0Config);
  const loanBanks = client.getBanksByMint(PYUSD_MINT, AssetTag.DEFAULT);
  if (loanBanks.length !== 1) throw new Error("Could not resolve Marginfi PYUSD bank");
  const loanBank = loanBanks[0];

  const configuredAccount = process.env.SOL_FLASH_ARB_MARGINFI_ACCOUNT?.trim();
  const account = configuredAccount
    ? await client.fetchAccount(new PublicKey(configuredAccount), true)
    : (await client.fetchAccountsForAuthority(wallet))[0];
  if (!account) throw new Error("No Marginfi account found for this wallet");
  console.log(`Marginfi Account: ${account.address.toBase58()}`);

  let bankLiquidityRaw = 100_000_000_000n;
  try {
    const vaultBal = await connection.getTokenAccountBalance(loanBank.liquidityVault);
    bankLiquidityRaw = (BigInt(vaultBal.value.amount) * 95n) / 100n;
    console.log(`Marginfi Headroom: ${formatRaw(bankLiquidityRaw)} PYUSD`);
  } catch {}

  // 1. Check Stable.com Capacity
  const statusRes = await fetchJson<{ asset: StableStatusAsset } | StableStatusAsset>(
    `${stableBase}/swap/status`,
    {
      method: "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        origin: "https://stable.com",
        referer: "https://stable.com/",
      },
      body: JSON.stringify({
        chainFrom: "102",
        assetFrom: "PYUSD",
        chainTo: "102",
        assetTo: "USDG",
        gasLess: false,
        amountFrom: "1000",
        addressFrom: wallet.toBase58(),
        addressTo: wallet.toBase58(),
      }),
    },
  );
  const status = (statusRes as any).asset || statusRes;
  const capacityRaw = parseDecimalFloor(String(status.balance));
  const minRaw = parseDecimalCeil(String(status.min));
  const maxRaw = parseDecimalFloor(String(status.max));

  console.log(`Stable.com Capacity: ${formatRaw(capacityRaw)} PYUSD (Min: ${formatRaw(minRaw)}, Max: ${formatRaw(maxRaw)})`);

  let loanAmountRaw = bankLiquidityRaw; // Full available Marginfi pool headroom
  if (capacityRaw > 10_000n) {
    const usable = capacityRaw - 10_000n;
    if (loanAmountRaw > usable) loanAmountRaw = usable;
  }
  if (maxRaw > 0n && loanAmountRaw > maxRaw) loanAmountRaw = maxRaw;

  console.log(`Target Sizing (Max Marginfi Flash Loan): ${formatRaw(loanAmountRaw)} PYUSD`);

  // Leg 1: Stable.com PYUSD -> USDG
  const stableQuoteRes = await fetchJson<{ asset: StableStatusAsset } | StableStatusAsset>(
    `${stableBase}/swap/status`,
    {
      method: "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        origin: "https://stable.com",
        referer: "https://stable.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
      },
      body: JSON.stringify({
        chainFrom: "102",
        assetFrom: "PYUSD",
        chainTo: "102",
        assetTo: "USDG",
        gasLess: false,
        amountFrom: formatRaw(loanAmountRaw),
        addressFrom: wallet.toBase58(),
        addressTo: wallet.toBase58(),
      }),
    },
  );
  const stableAsset = (stableQuoteRes as any).asset || stableQuoteRes;
  const stableOutRaw = parseDecimalFloor(stableAsset.amountTo);
  console.log(`Leg 1 (Stable.com): ${formatRaw(loanAmountRaw)} PYUSD -> ${formatRaw(stableOutRaw)} USDG`);

  // Leg 2: Jupiter USDG -> USDC
  const jup1QuoteRes = await fetch(`${jupBase}/quote?inputMint=${USDG_MINT.toBase58()}&outputMint=${USDC_MINT.toBase58()}&amount=${stableOutRaw}&slippageBps=2&dexes=Manifest,AlphaQ`, { headers: { accept: "application/json", ...(jupApiKey ? { "x-api-key": jupApiKey } : {}) } });
  const jup1Quote = (await jup1QuoteRes.json()) as any;
  const usdcOutRaw = BigInt(jup1Quote.outAmount);
  const dex1Label = jup1Quote.routePlan[0]?.swapInfo?.label || "DEX";
  console.log(`Leg 2 (Jupiter): ${formatRaw(stableOutRaw)} USDG -> ${formatRaw(usdcOutRaw)} USDC (${dex1Label})`);

  // Leg 3: Jupiter USDC -> PYUSD
  const jup2QuoteRes = await fetch(`${jupBase}/quote?inputMint=${USDC_MINT.toBase58()}&outputMint=${PYUSD_MINT.toBase58()}&amount=${usdcOutRaw}&slippageBps=2&dexes=Manifest,AlphaQ`, { headers: { accept: "application/json", ...(jupApiKey ? { "x-api-key": jupApiKey } : {}) } });
  const jup2Quote = (await jup2QuoteRes.json()) as any;
  const finalPyusdOutRaw = BigInt(jup2Quote.outAmount);
  const dex2Label = jup2Quote.routePlan[0]?.swapInfo?.label || "DEX";
  console.log(`Leg 3 (Jupiter): ${formatRaw(usdcOutRaw)} USDC -> ${formatRaw(finalPyusdOutRaw)} PYUSD (${dex2Label})`);

  const grossProfitRaw = finalPyusdOutRaw - loanAmountRaw;
  console.log(`--------------------------------------------------`);
  console.log(`Expected Net Realized Profit: ${formatRaw(grossProfitRaw)} PYUSD (+${(Number(grossProfitRaw)/1e6).toFixed(6)})`);
  console.log(`--------------------------------------------------`);

  // Fetch Swap Instructions & ALT tables
  console.log(`Fetching swap instructions and address lookup tables for both Jupiter legs...`);
  const [jup1Swap, jup2Swap] = await Promise.all([
    getJupiterSwapInstructions(jupBase, jupApiKey, jup1Quote, wallet),
    getJupiterSwapInstructions(jupBase, jupApiKey, jup2Quote, wallet),
  ]);

  const leg1Swap = buildSwapLeg(jup1Swap);
  const leg2Swap = buildSwapLeg(jup2Swap);

  const [alts1, alts2] = await Promise.all([
    fetchLookupTables(connection, leg1Swap.lookupTableAddresses),
    fetchLookupTables(connection, leg2Swap.lookupTableAddresses),
  ]);

  // Create Stable Leg
  console.log(`Creating Stable.com order...`);
  const orderRes = await fetchJson<StableOrder | { data: StableOrder }>(
    `${stableBase}/swap/create/singleChain`,
    {
      method: "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        origin: "https://stable.com",
        referer: "https://stable.com/",
      },
      body: JSON.stringify({
        chainFrom: "102",
        assetFrom: "PYUSD",
        chainTo: "102",
        assetTo: "USDG",
        gasLess: false,
        amountFrom: formatRaw(loanAmountRaw),
        amountTo: formatRaw(stableOutRaw),
        addressFrom: wallet.toBase58(),
        addressTo: wallet.toBase58(),
        device: randomUUID(),
      }),
    },
  );
  const stableOrder = (orderRes as any).data || orderRes;
  const stableLeg = buildStableSwapInstruction(
    wallet,
    loanAmountRaw,
    stableOutRaw,
    stableOrder,
    PYUSD_MINT,
    USDG_MINT,
  );

  // Combine instructions
  const swapInstructions: TransactionInstruction[] = [
    stableLeg.instruction,
    ...leg1Swap.instructions,
    ...leg2Swap.instructions,
  ];

  // Merge ALTs
  const mergedAlts: AddressLookupTableAccount[] = [];
  const seenAlts = new Set<string>();
  for (const alt of [...(client.addressLookupTables ?? []), ...alts1, ...alts2]) {
    const key = alt.key.toBase58();
    if (!seenAlts.has(key)) {
      seenAlts.add(key);
      mergedAlts.push(alt);
    }
  }

  const latestBlockhash = await connection.getLatestBlockhash("confirmed");
  const uiAmount = formatRaw(loanAmountRaw, 6);
  const borrow = await account.makeBorrowIx(loanBank.address, uiAmount, {
    createAtas: false,
    wrapAndUnwrapSol: false,
    overrideInferAccounts: {
      authority: wallet,
      group: account.group,
    },
  });
  const repay = await account.makeRepayIx(loanBank.address, uiAmount, true, {
    wrapAndUnwrapSol: false,
    overrideInferAccounts: {
      authority: wallet,
      group: account.group,
    },
  });

  const program = client.program;
  const bank = client.bankMap.get(loanBank.address.toBase58());

  const innerIxs: TransactionInstruction[] = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: 1_200_000 }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 10_000 }),
    ...borrow.instructions,
    ...swapInstructions,
    ...repay.instructions,
  ];

  const endIndex = innerIxs.length + 1;
  const beginFlashLoanIx = await (program.methods as any)
    .lendingAccountStartFlashloan(new BN(endIndex))
    .accounts({
      marginfiAccount: account.address,
    })
    .accountsPartial({
      authority: wallet,
      group: account.group,
      ixsSysvar: new PublicKey("Sysvar1nstructions1111111111111111111111111"),
    })
    .instruction();

  const healthAccounts: { pubkey: PublicKey; isSigner: boolean; isWritable: boolean }[] = [];
  if (bank) {
    healthAccounts.push({ pubkey: bank.address, isSigner: false, isWritable: false });
    healthAccounts.push({ pubkey: bank.oracleKey, isSigner: false, isWritable: false });
  }

  const endFlashLoanIx = new TransactionInstruction({
    programId: program.programId,
    data: Buffer.from([105, 124, 201, 106, 153, 2, 8, 156]), // lending_account_end_flashloan
    keys: [
      { pubkey: account.address, isSigner: false, isWritable: true },
      { pubkey: account.group, isSigner: false, isWritable: false },
      { pubkey: wallet, isSigner: true, isWritable: false },
      ...healthAccounts,
    ],
  });

  const messageV0 = new TransactionMessage({
    payerKey: wallet,
    recentBlockhash: latestBlockhash.blockhash,
    instructions: [beginFlashLoanIx, ...innerIxs, endFlashLoanIx],
  }).compileToV0Message(mergedAlts);

  const tx = new VersionedTransaction(messageV0);
  tx.sign([keypair]);

  const wireSize = tx.serialize().length;
  console.log(`Packed Wire Transaction Size: ${wireSize}/${MAX_WIRE_TRANSACTION_BYTES} bytes`);
  if (wireSize > MAX_WIRE_TRANSACTION_BYTES) {
    throw new Error(`Transaction is ${wireSize} bytes, exceeding Solana limit of ${MAX_WIRE_TRANSACTION_BYTES}!`);
  }

  console.log(`Simulating 2-hop atomic transaction on RPC...`);
  const sim = await connection.simulateTransaction(tx, { commitment: "confirmed", sigVerify: true });
  if (sim.value.err) {
    console.error("Simulation error logs:", sim.value.logs);
    throw new Error(`Simulation failed: ${JSON.stringify(sim.value.err)}`);
  }
  console.log(`Simulation PASSED: Consumed ${sim.value.unitsConsumed} compute units!`);

  if (!send) {
    console.log(`\n[DRY RUN COMPLETE] To broadcast onchain, pass: --send --confirm-mainnet EXECUTE_2HOP_ARB`);
    return;
  }

  if (confirmArg !== "EXECUTE_2HOP_ARB") {
    throw new Error("Missing --confirm-mainnet EXECUTE_2HOP_ARB confirmation flag");
  }

  console.log(`Broadcasting transaction on mainnet...`);
  const sig = await connection.sendRawTransaction(tx.serialize(), {
    skipPreflight: true,
    preflightCommitment: "confirmed",
  });
  console.log(`Submitted: https://solscan.io/tx/${sig}`);

  const confirmation = await connection.confirmTransaction(
    {
      signature: sig,
      blockhash: latestBlockhash.blockhash,
      lastValidBlockHeight: latestBlockhash.lastValidBlockHeight,
    },
    "confirmed",
  );

  if (confirmation.value.err) {
    throw new Error(`Transaction reverted: ${JSON.stringify(confirmation.value.err)}`);
  }
  console.log(`[+] Confirmed 2-Hop Arbitrage onchain: ${sig}`);
}

main().catch((err) => {
  console.error(`ERROR: ${err.message || err}`);
  process.exit(1);
});
