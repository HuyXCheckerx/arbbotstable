import bs58 from "bs58";
import type { Connection } from "@solana/web3.js";
import type { StateStore } from "./state.js";
import type { PreparedAtomicTransaction } from "./transaction.js";
import type { RouteQuote } from "./types.js";
import { routeLabel } from "./routes.js";

export async function reconcilePending(connection: Connection, stateStore: StateStore): Promise<boolean> {
  const state = stateStore.snapshot();
  if (!state.pending) return true;
  const pending = state.pending;
  const status = (await connection.getSignatureStatuses([pending.signature], {
    searchTransactionHistory: true,
  })).value[0];
  if (status?.err) {
    console.error(`[pending] ${pending.signature} failed: ${JSON.stringify(status.err)}`);
    await stateStore.clearPending();
    return true;
  }
  if (status?.confirmationStatus === "confirmed" || status?.confirmationStatus === "finalized") {
    console.log(`[confirmed] ${pending.signature}`);
    await stateStore.clearPending(pending.signature);
    return true;
  }
  const blockHeight = await connection.getBlockHeight("confirmed");
  if (blockHeight > pending.lastValidBlockHeight) {
    console.warn(`[expired] ${pending.signature} can no longer land`);
    await stateStore.clearPending();
    return true;
  }
  console.log(`[pending] ${pending.signature} is still live; submissions remain locked`);
  return false;
}

export async function submitPrepared(params: {
  connection: Connection;
  stateStore: StateStore;
  prepared: PreparedAtomicTransaction;
  quote: RouteQuote;
}): Promise<string> {
  const { connection, stateStore, prepared, quote } = params;
  const signature = bs58.encode(prepared.transaction.signatures[0]);
  await stateStore.setPending({
    signature,
    blockhash: prepared.blockhash,
    lastValidBlockHeight: prepared.lastValidBlockHeight,
    route: routeLabel(quote),
    principalRaw: quote.principalRaw.toString(),
    createdAt: new Date().toISOString(),
  });
  try {
    const returnedSignature = await connection.sendRawTransaction(
      prepared.transaction.serialize(),
      { skipPreflight: false, maxRetries: 0, preflightCommitment: "confirmed" },
    );
    if (returnedSignature !== signature) {
      throw new Error(`RPC returned unexpected signature ${returnedSignature}; expected ${signature}`);
    }
  } catch (error) {
    console.error(
      `[submit] RPC response may be ambiguous for ${signature}; pending lock retained: ${(error as Error).message}`,
    );
    throw error;
  }
  console.log(`[submitted] ${signature}`);
  return signature;
}
