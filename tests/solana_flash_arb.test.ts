import assert from "node:assert/strict";
import test from "node:test";

import {
  MAINNET_GENESIS_HASH,
  PYUSD_MINT,
  STABLE_PROGRAM_ID,
  USDC_MINT,
  USDG_MINT,
  bufferedFeeRaw,
  buildStableSwapInstruction,
  capacityLimitedLoanAmount,
  conservativeCycle,
  finalComputeUnitLimit,
  formatRaw,
  intermediateTokenProgram,
  isTwoHopJupiterRoute,
  parseDecimalToRawCeil,
  parseDecimalToRawFloor,
  parseUiAmountToRaw,
  resolveLoanMint,
} from "../src/engines/solana_flash_arb.js";
import { PublicKey } from "@solana/web3.js";
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
