import { randomUUID } from "node:crypto";
import { getAssociatedTokenAddressSync } from "@solana/spl-token";
import { PublicKey, TransactionInstruction, type AccountMeta } from "@solana/web3.js";
import { formatStableAmount, stableOutputRaw } from "./amounts.js";
import { config } from "./config.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  MAIN_STATE_SEED,
  NATIVE_FEE_SEED,
  NONCE_SEED,
  POOL_SEED,
  PROGRAM_IDS,
  SINGLE_CHAIN_SWAP_DISCRIMINATOR,
  STABLE_API,
  STABLE_CHAIN_ID,
  STABLE_PROGRAM_ID,
  TOKEN_CONFIG,
  type TokenSymbol,
} from "./constants.js";
import type { StableInstructionResult } from "./types.js";

type StableOrder = {
  maintainerSignature: string;
  recoveryId?: number | string;
  nonce: number | string;
  deadline: number | string;
  executionFeeNative?: number | string;
  nativeFee?: number | string;
};

function pda(seeds: Buffer[]): PublicKey {
  return PublicKey.findProgramAddressSync(seeds, STABLE_PROGRAM_ID)[0];
}

export function getStableSwapAccountMetas(
  wallet: PublicKey,
  assetFrom: TokenSymbol,
  assetTo: TokenSymbol,
): AccountMeta[] {
  const input = TOKEN_CONFIG[assetFrom];
  const output = TOKEN_CONFIG[assetTo];
  const mainState = pda([MAIN_STATE_SEED]);
  const nonce = pda([NONCE_SEED, wallet.toBuffer()]);
  const nativeFee = pda([NATIVE_FEE_SEED]);
  const inputPool = pda([POOL_SEED, input.mint.toBuffer()]);
  const outputPool = pda([POOL_SEED, output.mint.toBuffer()]);
  const userInputAta = getAssociatedTokenAddressSync(
    input.mint,
    wallet,
    false,
    input.tokenProgram,
    ASSOCIATED_TOKEN_PROGRAM_ID,
  );
  const inputPoolAta = getAssociatedTokenAddressSync(
    input.mint,
    inputPool,
    true,
    input.tokenProgram,
    ASSOCIATED_TOKEN_PROGRAM_ID,
  );
  const userOutputAta = getAssociatedTokenAddressSync(
    output.mint,
    wallet,
    false,
    output.tokenProgram,
    ASSOCIATED_TOKEN_PROGRAM_ID,
  );
  const outputPoolAta = getAssociatedTokenAddressSync(
    output.mint,
    outputPool,
    true,
    output.tokenProgram,
    ASSOCIATED_TOKEN_PROGRAM_ID,
  );

  return [
    { pubkey: wallet, isSigner: true, isWritable: true },
    { pubkey: wallet, isSigner: true, isWritable: true },
    { pubkey: nonce, isSigner: false, isWritable: true },
    { pubkey: mainState, isSigner: false, isWritable: false },
    { pubkey: nativeFee, isSigner: false, isWritable: true },
    { pubkey: inputPool, isSigner: false, isWritable: true },
    { pubkey: input.mint, isSigner: false, isWritable: false },
    { pubkey: inputPoolAta, isSigner: false, isWritable: true },
    { pubkey: userInputAta, isSigner: false, isWritable: true },
    { pubkey: outputPool, isSigner: false, isWritable: true },
    { pubkey: output.mint, isSigner: false, isWritable: false },
    { pubkey: outputPoolAta, isSigner: false, isWritable: true },
    { pubkey: wallet, isSigner: false, isWritable: false },
    { pubkey: userOutputAta, isSigner: false, isWritable: true },
    { pubkey: input.tokenProgram, isSigner: false, isWritable: false },
    { pubkey: output.tokenProgram, isSigner: false, isWritable: false },
    { pubkey: PROGRAM_IDS.associatedToken, isSigner: false, isWritable: false },
    { pubkey: PROGRAM_IDS.system, isSigner: false, isWritable: false },
  ];
}

function encodeOrderData(order: StableOrder, amountRaw: bigint): Buffer {
  const signatureHex = order.maintainerSignature.replace(/^0x/, "");
  const signatureBytes = Buffer.from(signatureHex, "hex");
  let signature: Buffer;
  let recoveryId: number;
  if (signatureBytes.length === 65) {
    signature = signatureBytes.subarray(0, 64);
    recoveryId = signatureBytes[64];
  } else if (signatureBytes.length === 64) {
    signature = signatureBytes;
    recoveryId = Number(order.recoveryId ?? 0);
  } else {
    throw new Error(`Stable.com returned a ${signatureBytes.length}-byte maintainer signature`);
  }

  const nativeFee = BigInt(order.executionFeeNative ?? order.nativeFee ?? 0);
  const nonce = BigInt(order.nonce);
  const deadline = BigInt(order.deadline);
  const data = Buffer.alloc(105);
  SINGLE_CHAIN_SWAP_DISCRIMINATOR.copy(data, 0);
  data.writeBigUInt64LE(amountRaw, 8);
  data.writeBigUInt64LE(nativeFee, 16);
  signature.copy(data, 24);
  data.writeBigUInt64LE(nonce, 88);
  data.writeBigInt64LE(deadline, 96);
  data.writeUInt8(recoveryId, 104);
  return data;
}

export async function buildStableSwapInstruction(params: {
  wallet: PublicKey;
  assetFrom: TokenSymbol;
  assetTo: TokenSymbol;
  amountRaw: bigint;
}): Promise<StableInstructionResult> {
  if (params.amountRaw <= 0n) throw new Error("Stable.com swap amount must be positive");
  const outputRaw = stableOutputRaw(params.assetFrom, params.assetTo, params.amountRaw);
  const response = await fetch(`${STABLE_API}/swap/create/singleChain`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      assetFrom: params.assetFrom,
      assetTo: params.assetTo,
      chainFrom: String(STABLE_CHAIN_ID),
      chainTo: String(STABLE_CHAIN_ID),
      amountFrom: formatStableAmount(params.amountRaw),
      amountTo: formatStableAmount(outputRaw),
      addressFrom: params.wallet.toBase58(),
      addressTo: params.wallet.toBase58(),
      device: randomUUID(),
      gasLess: false,
    }),
    signal: AbortSignal.timeout(config.httpTimeoutMs),
  });
  const body = await response.text();
  if (!response.ok) {
    throw new Error(`Stable.com order HTTP ${response.status}: ${body.slice(0, 800)}`);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    throw new Error(`Stable.com returned non-JSON data: ${body.slice(0, 500)}`);
  }
  const container = parsed as { data?: StableOrder } & StableOrder;
  const order = container.data ?? container;
  if (!order.maintainerSignature) throw new Error("Stable.com order has no maintainer signature");
  const nativeFeeLamports = BigInt(order.executionFeeNative ?? order.nativeFee ?? 0);
  if (nativeFeeLamports > config.maxStableNativeFeeLamports) {
    throw new Error(
      `Stable.com native fee ${nativeFeeLamports} exceeds configured maximum ${config.maxStableNativeFeeLamports}`,
    );
  }

  return {
    instruction: new TransactionInstruction({
      programId: STABLE_PROGRAM_ID,
      keys: getStableSwapAccountMetas(params.wallet, params.assetFrom, params.assetTo),
      data: encodeOrderData(order, params.amountRaw),
    }),
    nativeFeeLamports,
    nonce: BigInt(order.nonce),
    deadline: BigInt(order.deadline),
  };
}
