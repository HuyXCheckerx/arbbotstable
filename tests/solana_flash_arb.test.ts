import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test, { type TestContext } from "node:test";

import {
  MAINNET_GENESIS_HASH,
  PYUSD_MINT,
  STABLE_PROGRAM_ID,
  USDC_MINT,
  USDG_MINT,
  bufferedFeeRaw,
  buildJupiterQuoteParameters,
  buildStableSwapInstruction,
  capacityLimitedLoanAmount,
  capacityLimitedStableFirstLoanAmount,
  conservativeCycle,
  fetchJson,
  finalComputeUnitLimit,
  formatRaw,
  getMetaMatchaQuote,
  intermediateTokenProgram,
  isBlockheightExpiry,
  isTwoHopJupiterRoute,
  jupiterRouteConstraints,
  parseDecimalToRawCeil,
  parseDecimalToRawFloor,
  parseUiAmountToRaw,
  parseCli,
  resolveIntermediateMint,
  resolveLoanMint,
  shouldFallbackToJupiterLite,
  assertNoExistingLoanLiability,
  stableInputSymbol,
  stableOutputSymbol,
  classifySignatureStatus,
  effectiveScaledMinimumProfitRaw,
  prerunCandidateQuotes,
  parseSolanaRpcEndpoints,
  wrapConnectionWithResilientRpc,
  type JupiterQuote,
  type StableLeg,
} from "../src/engines/solana_flash_arb.js";
import {
  PublicKey,
  Keypair,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
  type Connection,
} from "@solana/web3.js";
import { TOKEN_2022_PROGRAM_ID } from "@solana/spl-token";

test("uses the Solana mainnet-beta genesis hash", () => {
  assert.equal(
    MAINNET_GENESIS_HASH,
    "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
  );
});

test("uses the canonical Token-2022 USDG mint", () => {
  assert.equal(
    USDG_MINT.toBase58(),
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",
  );
  assert.ok(intermediateTokenProgram(USDG_MINT).equals(TOKEN_2022_PROGRAM_ID));
});

test("resolves PYUSD and USDG as flash-loan mints", () => {
  assert.ok(resolveLoanMint("PYUSD").equals(PYUSD_MINT));
  assert.ok(resolveLoanMint("USDG").equals(USDG_MINT));
  assert.throws(() => resolveLoanMint("USDT"), /must be USDC, PYUSD, or USDG/);
});

test("identifies direct Jupiter routing for atomic flash loans", () => {
  assert.equal(isTwoHopJupiterRoute(PYUSD_MINT, USDG_MINT), false);
  assert.equal(isTwoHopJupiterRoute(USDG_MINT, PYUSD_MINT), false);
  assert.equal(isTwoHopJupiterRoute(USDC_MINT, PYUSD_MINT), false);
  assert.equal(isTwoHopJupiterRoute(USDC_MINT, USDG_MINT), false);
  assert.equal(isTwoHopJupiterRoute(PYUSD_MINT, USDC_MINT), false);
});

test("gives PYUSD/USDG enough accounts for Jupiter-managed USDC routing", () => {
  assert.deepEqual(jupiterRouteConstraints(PYUSD_MINT, USDG_MINT, 20, true), {
    maxAccounts: 24,
    onlyDirectRoutes: false,
  });
  assert.deepEqual(jupiterRouteConstraints(USDG_MINT, PYUSD_MINT, 30, true), {
    maxAccounts: 30,
    onlyDirectRoutes: false,
  });
  assert.deepEqual(jupiterRouteConstraints(USDC_MINT, PYUSD_MINT, 20, true), {
    maxAccounts: 20,
    onlyDirectRoutes: true,
  });
});

test("enforces zero slippage on every Solana quote", () => {
  const params = buildJupiterQuoteParameters(
    USDG_MINT,
    PYUSD_MINT,
    10_000_000n,
    false,
    24,
  );

  assert.equal(params.get("slippageBps"), "0");
  assert.equal(params.get("amount"), "10000000");
  assert.equal(params.get("onlyDirectRoutes"), "false");
  assert.equal(params.get("maxAccounts"), "24");
});

test("distinguishes an unrecorded blockhash expiry from an ambiguous signature", () => {
  const expiry = Object.assign(new Error("Signature has expired: block height exceeded"), {
    name: "TransactionExpiredBlockheightExceededError",
  });

  assert.equal(isBlockheightExpiry(expiry), true);
  assert.equal(classifySignatureStatus(null), "missing");
  assert.equal(
    classifySignatureStatus({ err: null, confirmationStatus: "processed" }),
    "ambiguous",
  );
  assert.equal(
    classifySignatureStatus({ err: null, confirmationStatus: "confirmed" }),
    "confirmed",
  );
  assert.equal(
    classifySignatureStatus({
      err: { InstructionError: [5, { Custom: 6001 }] },
      confirmationStatus: "confirmed",
    }),
    "reverted",
  );
});

test("parses and formats six-decimal stablecoin amounts exactly", () => {
  assert.equal(parseUiAmountToRaw("100000"), 100_000_000_000n);
  assert.equal(parseUiAmountToRaw("0.000001"), 1n);
  assert.equal(formatRaw(100_080_000_001n), "100080.000001");
  assert.equal(formatRaw(-20_000_000n), "-20");
  assert.throws(() => parseUiAmountToRaw("1.0000001"), /more than 6/);
  assert.throws(() => parseUiAmountToRaw("0"), /positive/);
});

test("uses the Jupiter threshold and Stable output for the guaranteed cycle", () => {
  const result = conservativeCycle(
    { otherAmountThreshold: "100079000000" },
    100_020_000_000n,
    100_000_000_000n,
  );
  assert.deepEqual(result, {
    firstLegMinimumRaw: 100_079_000_000n,
    secondLegMinimumRaw: 100_020_000_000n,
    grossProfitRaw: 20_000_000n,
  });
});

test("floors Stable decimal outputs to token precision", () => {
  assert.equal(parseDecimalToRawFloor("100027.9195177515"), 100_027_919_517n);
  assert.equal(parseDecimalToRawFloor("0.0000009"), 0n);
  assert.equal(parseDecimalToRawCeil("100027.9195170001"), 100_027_919_518n);
  assert.equal(parseDecimalToRawCeil("100027.919517"), 100_027_919_517n);
});

test("scales the flash loan to Stable's usable capacity", () => {
  assert.equal(
    capacityLimitedLoanAmount(
      100_000_000_000n,
      100_077_424_472n,
      50_733_283_823n,
    ),
    50_694_034_234n,
  );
  assert.equal(
    capacityLimitedLoanAmount(
      50_000_000_000n,
      49_990_000_000n,
      53_056_160_000n,
    ),
    50_000_000_000n,
  );
});

test("resolves all three sniper tokens as Stable.com output mints", () => {
  assert.ok(resolveIntermediateMint("USDC").equals(USDC_MINT));
  assert.ok(resolveIntermediateMint("PYUSD").equals(PYUSD_MINT));
  assert.ok(resolveIntermediateMint("USDG").equals(USDG_MINT));
});

test("stable-first sizing cannot exceed Stable's input capacity", () => {
  assert.equal(stableInputSymbol("stable-first", "PYUSD", "USDG"), "PYUSD");
  assert.equal(stableInputSymbol("dex-first", "PYUSD", "USDG"), "USDG");
  assert.equal(stableOutputSymbol("stable-first", "PYUSD", "USDG"), "USDG");
  assert.equal(stableOutputSymbol("dex-first", "PYUSD", "USDG"), "PYUSD");
  assert.equal(
    capacityLimitedStableFirstLoanAmount(
      100_000_000_000n,
      100_000_000_000n,
      32_625_824_000n,
    ),
    32_625_824_000n,
  );
});

test("does not retry a definitive Jupiter HTTP 400", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response(
      JSON.stringify({ error: "No routes found", errorCode: "NO_ROUTES_FOUND" }),
      { status: 400, headers: { "content-type": "application/json" } },
    );
  };
  try {
    await assert.rejects(
      fetchJson(
        "https://api.jup.ag/swap/v1/quote",
        {},
        { httpTimeoutMs: 1_000, httpAttempts: 3 },
        "Jupiter quote",
      ),
      /Jupiter quote failed: Jupiter quote returned HTTP 400/,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

function metaMatchaHelperFixture(t: TestContext, failures: string[]) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "metamatcha-quote-test-"));
  const helperPath = path.join(directory, "helper.cjs");
  const countPath = `${helperPath}.count`;
  t.after(() => {
    fs.rmSync(helperPath, { force: true });
    fs.rmSync(countPath, { force: true });
    fs.rmdirSync(directory);
  });
  fs.writeFileSync(helperPath, `
    const fs = require("node:fs");
    const request = JSON.parse(fs.readFileSync(0, "utf8"));
    const countPath = __filename + ".count";
    const attempt = fs.existsSync(countPath) ? Number(fs.readFileSync(countPath, "utf8")) : 0;
    fs.writeFileSync(countPath, String(attempt + 1));
    const failures = ${JSON.stringify(failures)};
    if (attempt < failures.length) {
      process.stderr.write(failures[attempt] + "\\n");
      process.exitCode = 1;
    } else {
      process.stdout.write(JSON.stringify({
        provider: "MetaMatcha",
        inputMint: request.inputMint,
        outputMint: request.outputMint,
        inAmount: request.amount,
        outAmount: "10000001",
        otherAmountThreshold: "10000001",
        swapMode: "ExactIn",
        slippageBps: request.slippageBps,
        routePlan: [{ swapInfo: { label: "fixture" } }],
        serializedTransaction: "Zml4dHVyZQ=="
      }));
    }
  `);
  return {
    config: {
      matchaApiBase: "https://unused.invalid",
      matchaAggregators: ["0x", "OKX"],
      matchaPython: process.execPath,
      matchaHelperPath: helperPath,
      httpTimeoutMs: 1_000,
      httpAttempts: 3,
    },
    calls: () => Number(fs.readFileSync(countPath, "utf8")),
  };
}

for (const status of [401, 403, 429]) {
  test(`does not rerun the MetaMatcha helper after HTTP ${status}`, async (t) => {
    const message = status === 429
      ? "MetaMatcha quote failed: HTTP 429: Too Many Requests; retry-after=120s"
      : `MetaMatcha quote failed: HTTP ${status}: access denied`;
    const fixture = metaMatchaHelperFixture(t, [message]);
    await assert.rejects(
      getMetaMatchaQuote(fixture.config, PYUSD_MINT, USDG_MINT, 10_000_000n, USDC_MINT),
      { message },
    );
    assert.equal(fixture.calls(), 1);
  });
}

test("preserves an aggregator HTTP denial without repeating the helper error prefix", async (t) => {
  const message = "MetaMatcha quote failed: no simulated executable quote; request failures: 0x: HTTP 403: access denied";
  const fixture = metaMatchaHelperFixture(t, [message]);
  await assert.rejects(
    getMetaMatchaQuote(fixture.config, PYUSD_MINT, USDG_MINT, 10_000_000n, USDC_MINT),
    { message },
  );
  assert.equal(fixture.calls(), 1);
});

test("preserves the Vercel challenge diagnosis without retrying the helper", async (t) => {
  const message = "MetaMatcha quote failed: https://meta.matcha.xyz/api/competitions access blocked by Vercel Security Checkpoint (HTTP 429; x-vercel-mitigated=challenge); request-id=test-request";
  const fixture = metaMatchaHelperFixture(t, [message]);
  await assert.rejects(
    getMetaMatchaQuote(fixture.config, PYUSD_MINT, USDG_MINT, 10_000_000n, USDC_MINT),
    { message },
  );
  assert.equal(fixture.calls(), 1);
});

for (const status of [503]) {
  test(`retries a transient MetaMatcha HTTP ${status} and returns the recovered quote`, async (t) => {
    const fixture = metaMatchaHelperFixture(t, [
      `MetaMatcha quote failed: HTTP ${status}: transient failure for amount 403`,
    ]);
    const quote = await getMetaMatchaQuote(
      fixture.config, PYUSD_MINT, USDG_MINT, 10_000_000n, USDC_MINT,
    );
    assert.equal(fixture.calls(), 2);
    assert.equal(quote.provider, "MetaMatcha");
    assert.equal(quote.inAmount, "10000000");
    assert.equal(quote.slippageBps, 0);
    assert.equal(quote.serializedTransaction, "Zml4dHVyZQ==");
  });
}

test("bounds transient MetaMatcha retries and adds a missing helper error prefix once", async (t) => {
  const fixture = metaMatchaHelperFixture(t, Array(3).fill("HTTP 503: unavailable"));
  await assert.rejects(
    getMetaMatchaQuote(fixture.config, PYUSD_MINT, USDG_MINT, 10_000_000n, USDC_MINT),
    { message: "MetaMatcha quote failed: HTTP 503: unavailable" },
  );
  assert.equal(fixture.calls(), 3);
});

test("falls back to Jupiter Lite only for api.jup.ag authentication failures", () => {
  assert.equal(
    shouldFallbackToJupiterLite(
      "https://api.jup.ag/swap/v1",
      new Error("Jupiter quote returned HTTP 401: Unauthorized"),
    ),
    true,
  );
  assert.equal(
    shouldFallbackToJupiterLite(
      "https://lite-api.jup.ag/swap/v1",
      new Error("Jupiter quote returned HTTP 401: Unauthorized"),
    ),
    false,
  );
  assert.equal(
    shouldFallbackToJupiterLite(
      "https://api.jup.ag/swap/v1",
      new Error("Jupiter quote returned HTTP 400: No routes found"),
    ),
    false,
  );
});

test("validates CLI route and provider choices", () => {
  const options = parseCli([
    "--swap-order",
    "stable-first",
    "--provider",
    "marginfi",
    "--dex-provider",
    "metamatcha",
    "--lookup-table",
    "BoTWvDa5rCYuoseapp9X2puDQZavWTjeva384e4b619S",
  ]);
  assert.equal(options.swapOrder, "stable-first");
  assert.equal(options.provider, "marginfi");
  assert.equal(options.dexProvider, "metamatcha");
  assert.equal(options.lookupTable, "BoTWvDa5rCYuoseapp9X2puDQZavWTjeva384e4b619S");
  assert.throws(() => parseCli(["--swap-order", "sideways"]), /must be/);
  assert.throws(() => parseCli(["--provider", "solana"]), /must be/);
  assert.throws(() => parseCli(["--dex-provider", "raydium"]), /must be/);
});

test("rejects a Marginfi account with an existing loan liability", () => {
  const bankAddress = new PublicKey("11111111111111111111111111111111");
  const account = {
    balances: [
      {
        bankPk: bankAddress,
        active: true,
        liabilityShares: { isZero: () => false },
      },
    ],
  };
  assert.throws(
    () => assertNoExistingLoanLiability(account as never, bankAddress, "PYUSD"),
    /already has a PYUSD liability/,
  );
});

test("encodes the Stable single-chain USDT to USDC instruction", () => {
  const wallet = new PublicKey("G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he");
  const leg = buildStableSwapInstruction(
    wallet,
    100_077_958_497n,
    100_027_919_517n,
    {
      maintainerSignature: `0x${"11".repeat(64)}`,
      recoveryId: 1,
      nonce: "7",
      deadline: "2000000000",
      executionFeeNative: "9213",
      amountFrom: "100077.958497",
      amountTo: "100027.919517",
    },
    100_000n,
  );

  assert.ok(leg.instruction.programId.equals(STABLE_PROGRAM_ID));
  assert.equal(leg.instruction.keys.length, 18);
  assert.equal(leg.instruction.data.length, 105);
  assert.equal(leg.instruction.data.readBigUInt64LE(8), 100_077_958_497n);
  assert.equal(leg.instruction.data.readBigUInt64LE(16), 9_213n);
  assert.equal(leg.instruction.data.readBigUInt64LE(88), 7n);
  assert.equal(leg.instruction.data.readBigInt64LE(96), 2_000_000_000n);
  assert.equal(leg.instruction.data[104], 1);
});

test("encodes a Token-2022 USDG to PYUSD Stable instruction", () => {
  const wallet = new PublicKey("G3yfNkUaTvr1QvAPThRuNL9H5oogVDrzSVopCsY1f1he");
  const leg = buildStableSwapInstruction(
    wallet,
    1_000_000n,
    1_000_001n,
    {
      maintainerSignature: `0x${"22".repeat(64)}`,
      recoveryId: 0,
      nonce: "9",
      deadline: "2000000000",
      executionFeeNative: "1000",
      amountFrom: "1",
      amountTo: "1.000001",
    },
    100_000n,
    USDG_MINT,
    PYUSD_MINT,
  );

  assert.ok(leg.instruction.keys[6].pubkey.equals(USDG_MINT));
  assert.ok(leg.instruction.keys[10].pubkey.equals(PYUSD_MINT));
  assert.ok(leg.instruction.keys[14].pubkey.equals(TOKEN_2022_PROGRAM_ID));
  assert.ok(leg.instruction.keys[15].pubkey.equals(TOKEN_2022_PROGRAM_ID));
});

test("fee conversion rounds up and includes its price buffer", () => {
  assert.equal(bufferedFeeRaw(10_000n, 200, 0), 2_000n);
  assert.equal(bufferedFeeRaw(10_000n, 200, 100), 2_020n);
});

test("compute limit adds safety padding and respects bounds", () => {
  assert.equal(finalComputeUnitLimit(500_000, 1_500, 1_400_000), 575_000);
  assert.equal(finalComputeUnitLimit(100_000, 1_500, 1_400_000), 200_000);
  assert.equal(finalComputeUnitLimit(1_300_000, 1_500, 1_400_000), 1_400_000);
});

test("proportional profit scaling calculates minimums according to principal size", () => {
  const base100kProfit = 1_000_000n; // $1.00 for 100,000 tokens
  const max100kLoan = 100_000_000_000n; // 100,000 tokens

  // Full 100,000 loan -> exact base minimum ($1.00)
  assert.equal(
    effectiveScaledMinimumProfitRaw(base100kProfit, max100kLoan, max100kLoan),
    1_000_000n,
  );

  // Scaled down to 15,511.802846 loan (Marginfi PYUSD headroom)
  const loan15k = 15_511_802_846n;
  const scaled15k = effectiveScaledMinimumProfitRaw(base100kProfit, loan15k, max100kLoan);
  assert.equal(scaled15k, 155_118n); // ~0.155118 tokens

  // Scaled down to 2,601.659317 loan (Marginfi USDG headroom)
  const loan2k = 2_601_659_317n;
  const scaled2k = effectiveScaledMinimumProfitRaw(base100kProfit, loan2k, max100kLoan);
  assert.equal(scaled2k, 26_016n); // ~0.026016 tokens

  // Very small loan clamps to absolute safety floor (10_000n = 0.01 tokens)
  const tinyLoan = 500_000_000n; // 500 tokens
  const scaledTiny = effectiveScaledMinimumProfitRaw(base100kProfit, tinyLoan, max100kLoan);
  assert.equal(scaledTiny, 10_000n); // clamped to safety floor 0.01

  // Loan greater than or equal to configured max returns base minimum
  assert.equal(
    effectiveScaledMinimumProfitRaw(base100kProfit, 120_000_000_000n, max100kLoan),
    1_000_000n,
  );
});

function createMockMatchaQuote(
  wallet: PublicKey,
  aggregator: string,
  buyAmount: bigint,
): JupiterQuote {
  const message = new TransactionMessage({
    payerKey: wallet,
    recentBlockhash: "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
    instructions: [
      SystemProgram.transfer({
        fromPubkey: wallet,
        toPubkey: PublicKey.default,
        lamports: 100,
      }),
    ],
  }).compileToV0Message();
  const tx = new VersionedTransaction(message);
  const serializedTransaction = Buffer.from(tx.serialize()).toString("base64");

  return {
    provider: "MetaMatcha",
    aggregator,
    inputMint: USDG_MINT.toBase58(),
    outputMint: PYUSD_MINT.toBase58(),
    inAmount: "10000000",
    outAmount: buyAmount.toString(),
    otherAmountThreshold: buyAmount.toString(),
    swapMode: "ExactIn",
    slippageBps: 0,
    routePlan: [{ swapInfo: { label: aggregator } }],
    serializedTransaction,
  };
}

test("prerunCandidateQuotes filters out poisoned quotes and selects the best passing candidate", async () => {
  const wallet = Keypair.generate();
  const dflow = createMockMatchaQuote(wallet.publicKey, "DFlow", 10_050_000n);
  const zerox = createMockMatchaQuote(wallet.publicKey, "0x", 10_030_000n);
  const jup = createMockMatchaQuote(wallet.publicKey, "Jupiter", 10_020_000n);

  const firstQuote: JupiterQuote = {
    ...dflow,
    candidates: [dflow, zerox, jup],
  };

  const mockConnection = {
    getAccountInfo: async () => null,
    getMultipleAccountsInfo: async () => [],
  } as unknown as Connection;

  const mockStableLeg: StableLeg = {
    instruction: SystemProgram.transfer({
      fromPubkey: wallet.publicKey,
      toPubkey: PublicKey.default,
      lamports: 10,
    }),
    outputRaw: 10_000_000n,
    executionFeeLamports: 10_000n,
    nonce: 1n,
    deadline: 9999999999n,
  };

  const mockConfig: any = {
    keypair: wallet,
    swapOrder: "stable-first",
    dexProvider: "metamatcha",
    loanSymbol: "PYUSD",
    intermediateSymbol: "USDG",
    probeComputeUnitLimit: 1_400_000,
    maxAccounts: 24,
    customLookupTableAddresses: [],
  };

  let dflowSimulated = false;
  const mockSimulate = async (_conn: Connection, tx: VersionedTransaction) => {
    if (!dflowSimulated) {
      dflowSimulated = true;
      throw new Error("Atomic simulation reverted: custom program error: 0x1771");
    }
    return 214_000;
  };

  const winning = await prerunCandidateQuotes(
    mockConfig,
    mockConnection,
    wallet.publicKey,
    undefined,
    undefined,
    10_000_000n,
    true,
    firstQuote,
    { inputRaw: 10_000_000n, outputRaw: 10_000_000n } as any,
    10_000n,
    [],
    [],
    "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
    mockSimulate,
    mockStableLeg,
  );

  assert.equal(winning.aggregatorName, "0x");
  assert.equal(winning.grossProfitRaw, 30_000n);
  assert.equal(winning.probeUnits, 214_000);
});

test("prerunCandidateQuotes throws clear error when all candidate quotes are poisoned", async () => {
  const wallet = Keypair.generate();
  const dflow = createMockMatchaQuote(wallet.publicKey, "DFlow", 10_050_000n);
  const zerox = createMockMatchaQuote(wallet.publicKey, "0x", 10_030_000n);

  const firstQuote: JupiterQuote = {
    ...dflow,
    candidates: [dflow, zerox],
  };

  const mockConnection = {
    getAccountInfo: async () => null,
    getMultipleAccountsInfo: async () => [],
  } as unknown as Connection;

  const mockStableLeg: StableLeg = {
    instruction: SystemProgram.transfer({
      fromPubkey: wallet.publicKey,
      toPubkey: PublicKey.default,
      lamports: 10,
    }),
    outputRaw: 10_000_000n,
    executionFeeLamports: 10_000n,
    nonce: 1n,
    deadline: 9999999999n,
  };

  const mockConfig: any = {
    keypair: wallet,
    swapOrder: "stable-first",
    dexProvider: "metamatcha",
    loanSymbol: "PYUSD",
    intermediateSymbol: "USDG",
    probeComputeUnitLimit: 1_400_000,
    maxAccounts: 24,
    customLookupTableAddresses: [],
  };

  const allPoisonedSimulate = async () => {
    throw new Error("Atomic simulation reverted: custom program error: 0x1771");
  };

  await assert.rejects(
    prerunCandidateQuotes(
      mockConfig,
      mockConnection,
      wallet.publicKey,
      undefined,
      undefined,
      10_000_000n,
      true,
      firstQuote,
      { inputRaw: 10_000_000n, outputRaw: 10_000_000n } as any,
      10_000n,
      [],
      [],
      "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
      allPoisonedSimulate,
      mockStableLeg,
    ),
    /All candidate quotes failed atomic prerun simulation or fell below the profit floor/,
  );
});

test("prerunCandidateQuotes rejects candidate quotes that fall below scaled minimum floor", async () => {
  const wallet = Keypair.generate();
  const dflow = createMockMatchaQuote(wallet.publicKey, "DFlow", 10_050_000n);
  const zerox = createMockMatchaQuote(wallet.publicKey, "0x", 10_005_000n);

  const firstQuote: JupiterQuote = {
    ...dflow,
    candidates: [dflow, zerox],
  };

  const mockConnection = {
    getAccountInfo: async () => null,
    getMultipleAccountsInfo: async () => [],
  } as unknown as Connection;

  const mockStableLeg: StableLeg = {
    instruction: SystemProgram.transfer({
      fromPubkey: wallet.publicKey,
      toPubkey: PublicKey.default,
      lamports: 10,
    }),
    outputRaw: 10_000_000n,
    executionFeeLamports: 10_000n,
    nonce: 1n,
    deadline: 9999999999n,
  };

  const mockConfig: any = {
    keypair: wallet,
    swapOrder: "stable-first",
    dexProvider: "metamatcha",
    loanSymbol: "PYUSD",
    intermediateSymbol: "USDG",
    probeComputeUnitLimit: 1_400_000,
    maxAccounts: 24,
    customLookupTableAddresses: [],
  };

  const dflowPoisonedSimulate = async () => {
    throw new Error("Atomic simulation reverted: custom program error: 0x1771");
  };

  await assert.rejects(
    prerunCandidateQuotes(
      mockConfig,
      mockConnection,
      wallet.publicKey,
      undefined,
      undefined,
      10_000_000n,
      true,
      firstQuote,
      { inputRaw: 10_000_000n, outputRaw: 10_000_000n } as any,
      10_000n,
      [],
      [],
      "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
      dflowPoisonedSimulate,
      mockStableLeg,
    ),
    /All candidate quotes failed atomic prerun simulation or fell below the profit floor/,
  );
});

test("parseSolanaRpcEndpoints deduplicates primary, fallbacks, and env variables", () => {
  const origRpc = process.env.SOLANA_RPC_URL;
  const origFallbacks = process.env.SOLANA_RPC_FALLBACKS;
  try {
    process.env.SOLANA_RPC_URL = "https://custom-1.solana.com,https://custom-2.solana.com";
    process.env.SOLANA_RPC_FALLBACKS = "https://custom-3.solana.com,https://custom-1.solana.com";

    const endpoints = parseSolanaRpcEndpoints(
      "https://primary.solana.com",
      ["https://extra.solana.com"],
    );

    assert.equal(endpoints[0], "https://primary.solana.com");
    assert.ok(endpoints.includes("https://extra.solana.com"));
    assert.ok(endpoints.includes("https://custom-1.solana.com"));
    assert.ok(endpoints.includes("https://custom-2.solana.com"));
    assert.ok(endpoints.includes("https://custom-3.solana.com"));
    assert.ok(endpoints.includes("https://api.mainnet-beta.solana.com"));
    // Ensure no duplicates
    assert.equal(new Set(endpoints).size, endpoints.length);
  } finally {
    if (origRpc !== undefined) process.env.SOLANA_RPC_URL = origRpc;
    else delete process.env.SOLANA_RPC_URL;
    if (origFallbacks !== undefined) process.env.SOLANA_RPC_FALLBACKS = origFallbacks;
    else delete process.env.SOLANA_RPC_FALLBACKS;
  }
});

test("wrapConnectionWithResilientRpc falls back to backup endpoint when primary fails", async () => {
  const mockConn: any = {
    rpcEndpoint: "https://dead-primary-rpc.invalid",
    _rpcRequest: async () => {
      throw new Error("Primary connection refused");
    },
    _rpcBatchRequest: async () => {
      throw new Error("Primary batch connection refused");
    },
  };

  const wrapped = wrapConnectionWithResilientRpc(mockConn, [
    "https://backup-mock-rpc.invalid",
  ]);

  // Intercept fetch globally or verify wrap behavior
  const origFetch = globalThis.fetch;
  let fetchedUrl = "";
  (globalThis as any).fetch = async (url: string, init: any) => {
    fetchedUrl = url;
    return {
      status: 200,
      json: async () => ({
        jsonrpc: "2.0",
        id: "1",
        result: { value: { blockhash: "test-hash-123", lastValidBlockHeight: 100 } },
      }),
    };
  };

  try {
    const res = await (wrapped as any)._rpcRequest("getLatestBlockhash", [
      { commitment: "confirmed" },
    ]);
    assert.equal(res.result.value.blockhash, "test-hash-123");
    assert.ok(fetchedUrl.length > 0);
  } finally {
    globalThis.fetch = origFetch;
  }
});

