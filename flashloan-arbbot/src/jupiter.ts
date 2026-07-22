import { PublicKey, TransactionInstruction } from "@solana/web3.js";
import { config } from "./config.js";
import { JUPITER_BUILD_API } from "./constants.js";
import type { ApiInstruction, JupiterBuildResponse } from "./types.js";

let apiKeyCursor = 0;

function nextHeaders(): Record<string, string> {
  if (config.jupiterApiKeys.length === 0) return {};
  const key = config.jupiterApiKeys[apiKeyCursor % config.jupiterApiKeys.length];
  apiKeyCursor += 1;
  return { "x-api-key": key };
}

function validateBuild(value: unknown): JupiterBuildResponse {
  const result = value as Partial<JupiterBuildResponse> & { error?: string };
  if (result.error) throw new Error(`Jupiter build error: ${result.error}`);
  if (
    !result.swapInstruction ||
    !result.inAmount ||
    !result.outAmount ||
    !result.otherAmountThreshold ||
    !Array.isArray(result.setupInstructions)
  ) {
    throw new Error(`Malformed Jupiter /build response: ${JSON.stringify(value).slice(0, 500)}`);
  }
  return result as JupiterBuildResponse;
}

export async function getJupiterBuild(params: {
  inputMint: PublicKey;
  outputMint: PublicKey;
  amount: bigint;
  taker: PublicKey;
}): Promise<JupiterBuildResponse> {
  const query = new URLSearchParams({
    inputMint: params.inputMint.toBase58(),
    outputMint: params.outputMint.toBase58(),
    amount: params.amount.toString(),
    taker: params.taker.toBase58(),
    slippageBps: String(config.slippageBps),
    maxAccounts: String(config.maxAccounts),
    wrapAndUnwrapSol: "false",
  });
  if (config.fastQuotes) query.set("mode", "fast");

  const response = await fetch(`${JUPITER_BUILD_API}?${query}`, {
    headers: nextHeaders(),
    signal: AbortSignal.timeout(config.httpTimeoutMs),
  });
  const body = await response.text();
  if (!response.ok) {
    throw new Error(`Jupiter /build HTTP ${response.status}: ${body.slice(0, 500)}`);
  }
  try {
    return validateBuild(JSON.parse(body));
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`Jupiter returned non-JSON data: ${body.slice(0, 500)}`);
    }
    throw error;
  }
}

export function toTransactionInstruction(ix: ApiInstruction): TransactionInstruction {
  return new TransactionInstruction({
    programId: new PublicKey(ix.programId),
    keys: ix.accounts.map((account) => ({
      pubkey: new PublicKey(account.pubkey),
      isSigner: account.isSigner,
      isWritable: account.isWritable,
    })),
    data: Buffer.from(ix.data, "base64"),
  });
}

export function describeJupiterRoute(build: JupiterBuildResponse): string {
  const labels = build.routePlan
    .map((step) => step.swapInfo.label ?? "unknown")
    .filter((label, index, all) => all.indexOf(label) === index);
  return labels.join(" -> ") || "unknown";
}
