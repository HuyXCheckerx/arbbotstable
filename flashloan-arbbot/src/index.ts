import { Connection } from "@solana/web3.js";
import { findMissingWalletAtas } from "./accounts.js";
import { rawToUsdc } from "./amounts.js";
import { config } from "./config.js";
import { fetchStablePoolBalances } from "./pools.js";
import { isEligible, quoteRoute, routeDetail, routeLabel, scanRoutes } from "./routes.js";
import { StateStore } from "./state.js";
import { reconcilePending, submitPrepared } from "./submission.js";
import { prepareAtomicTransaction, signPreparedTransaction } from "./transaction.js";
import { loadWalletPublicKey } from "./wallet.js";

const once = process.argv.includes("--once");
const connection = new Connection(config.rpcUrl, "confirmed");
const wallet = loadWalletPublicKey();
const stateStore = new StateStore(config.stateFile);

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function verifyAccounts(): Promise<void> {
  const missing = await findMissingWalletAtas(connection, wallet.publicKey);
  if (missing.length > 0) {
    throw new Error(
      `Missing wallet token accounts for ${missing.map((entry) => entry.symbol).join(", ")}. ` +
        "Set SOLANA_PRIVATE_KEY and run npm run init-atas once.",
    );
  }
}

async function scanOnce(): Promise<void> {
  if (!(await reconcilePending(connection, stateStore))) return;
  await stateStore.update({ status: "scanning", message: "Scanning atomic flash-loan routes" });
  const poolBalances = await fetchStablePoolBalances(connection);
  console.log(
    `[pools] USDC=${rawToUsdc(poolBalances.USDC)} USDG=${rawToUsdc(poolBalances.USDG)} ` +
      `PYUSD=${rawToUsdc(poolBalances.PYUSD)} USDT=${rawToUsdc(poolBalances.USDT)}`,
  );
  const scan = await scanRoutes({
    connection,
    wallet: wallet.publicKey,
    poolBalances,
  });
  const eligible = scan.all.filter(isEligible);
  if (!scan.best) {
    console.log(`[scan] ${scan.all.length} quotes checked; no guaranteed profitable route`);
    await stateStore.update({ status: "idle", message: "No guaranteed profitable route" });
    return;
  }

  console.log(`[scan] ${scan.all.length} quotes checked; ${eligible.length} eligible`);
  console.log(
    `[opportunity] ${routeDetail(scan.best)} principal=${rawToUsdc(scan.best.principalRaw)} ` +
      `guaranteedNet=${rawToUsdc(scan.best.guaranteedNetProfitRaw)} ` +
      `expectedNet=${rawToUsdc(scan.best.expectedNetProfitRaw)} ` +
      `expectedTokenSurplus=${rawToUsdc(scan.best.expectedIntermediateSurplusRaw)}`,
  );
  await stateStore.update({
    status: "simulating",
    message: routeLabel(scan.best),
    lastOpportunity: {
      route: routeLabel(scan.best),
      principalRaw: scan.best.principalRaw.toString(),
      guaranteedNetProfitRaw: scan.best.guaranteedNetProfitRaw.toString(),
      expectedNetProfitRaw: scan.best.expectedNetProfitRaw.toString(),
    },
  });

  const refreshedPools = await fetchStablePoolBalances(connection);
  const refreshed = await quoteRoute({
    route: { token: scan.best.token, venueOrder: scan.best.venueOrder },
    principalRaw: scan.best.principalRaw,
    wallet: wallet.publicKey,
    poolBalances: refreshedPools,
  });
  if (!refreshed || !isEligible(refreshed)) {
    console.log("[revalidate] opportunity disappeared before transaction construction");
    await stateStore.update({ status: "idle", message: "Opportunity failed revalidation" });
    return;
  }

  if (!config.simulateDryRun && !config.executionEnabled) {
    console.log("[dry-run] executable quote accepted; transaction simulation is disabled");
    await stateStore.update({ status: "idle", message: "Dry-run quote accepted" });
    return;
  }
  const prepared = await prepareAtomicTransaction({
    connection,
    wallet: wallet.publicKey,
    quote: refreshed,
  });
  console.log(
    `[simulation] success units=${prepared.unitsConsumed} limit=${prepared.computeUnitLimit} ` +
      `size=${prepared.serializedLength}/1232`,
  );

  if (!config.executionEnabled) {
    console.log("[dry-run] transaction was simulated but will not be signed or submitted");
    await stateStore.update({ status: "idle", message: "Dry-run simulation succeeded" });
    return;
  }
  if (!wallet.keypair) throw new Error("Live execution requires SOLANA_PRIVATE_KEY");
  signPreparedTransaction(prepared, wallet.keypair);
  await stateStore.update({ status: "submitting", message: routeLabel(refreshed) });
  await submitPrepared({ connection, stateStore, prepared, quote: refreshed });
}

async function main(): Promise<void> {
  await stateStore.load();
  await verifyAccounts();
  console.log(`[startup] wallet=${wallet.publicKey.toBase58()}`);
  console.log(
    `[startup] range=${rawToUsdc(config.minTradeRaw)}-${rawToUsdc(config.maxTradeRaw)} ` +
      `execution=${config.executionEnabled ? "ENABLED" : "disabled"} dryRun=${config.dryRun}`,
  );
  do {
    try {
      await scanOnce();
    } catch (error) {
      const message = (error as Error).message;
      console.error(`[error] ${message}`);
      await stateStore.update({ status: "error", message });
    }
    if (!once) await sleep(config.scanIntervalMs);
  } while (!once);
}

main().catch(async (error) => {
  console.error(error);
  try {
    await stateStore.update({ status: "error", message: (error as Error).message });
  } finally {
    process.exitCode = 1;
  }
});
