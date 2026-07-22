import type { TransactionInstruction } from "@solana/web3.js";
import type { IntermediateToken, VenueOrder } from "./constants.js";

export type ApiAccount = {
  pubkey: string;
  isSigner: boolean;
  isWritable: boolean;
};

export type ApiInstruction = {
  programId: string;
  accounts: ApiAccount[];
  data: string;
};

export type JupiterBuildResponse = {
  inputMint: string;
  outputMint: string;
  inAmount: string;
  outAmount: string;
  otherAmountThreshold: string;
  slippageBps: number;
  routePlan: Array<{
    swapInfo: {
      label?: string;
      inAmount: string;
      outAmount: string;
      inputMint: string;
      outputMint: string;
    };
    bps?: number;
    percent?: number;
  }>;
  computeBudgetInstructions: ApiInstruction[];
  setupInstructions: ApiInstruction[];
  swapInstruction: ApiInstruction;
  cleanupInstruction: ApiInstruction | null;
  otherInstructions: ApiInstruction[];
  tipInstruction: ApiInstruction | null;
  addressesByLookupTableAddress: Record<string, string[]> | null;
  blockhashWithMetadata?: {
    blockhash: number[];
    lastValidBlockHeight: number;
  };
};

export type RouteQuote = {
  token: IntermediateToken;
  venueOrder: VenueOrder;
  principalRaw: bigint;
  jupiterInputRaw: bigint;
  jupiterExpectedOutputRaw: bigint;
  jupiterMinimumOutputRaw: bigint;
  stableInputRaw: bigint;
  stableOutputRaw: bigint;
  expectedGrossProfitRaw: bigint;
  guaranteedGrossProfitRaw: bigint;
  expectedNetProfitRaw: bigint;
  guaranteedNetProfitRaw: bigint;
  expectedIntermediateSurplusRaw: bigint;
  build: JupiterBuildResponse;
};

export type StableInstructionResult = {
  instruction: TransactionInstruction;
  nativeFeeLamports: bigint;
  nonce: bigint;
  deadline: bigint;
};

export type PendingSubmission = {
  signature: string;
  blockhash: string;
  lastValidBlockHeight: number;
  route: string;
  principalRaw: string;
  createdAt: string;
};

export type BotState = {
  status: "idle" | "scanning" | "simulating" | "submitting" | "pending" | "error";
  updatedAt: string;
  message?: string;
  lastOpportunity?: {
    route: string;
    principalRaw: string;
    guaranteedNetProfitRaw: string;
    expectedNetProfitRaw: string;
  };
  lastConfirmedSignature?: string;
  pending?: PendingSubmission;
};
