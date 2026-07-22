import assert from "node:assert/strict";
import test from "node:test";
import {
  formatStableAmount,
  generateCandidateSizesRaw,
  generateRefinementSizesRaw,
  rawToUsdc,
  stableOutputRaw,
  usdcToRaw,
} from "../src/amounts.js";

test("USDC conversion is exact at six decimals", () => {
  assert.equal(usdcToRaw("10000"), 10_000_000_000n);
  assert.equal(usdcToRaw("100000.123456"), 100_000_123_456n);
  assert.equal(rawToUsdc(-123_456n), "-0.123456");
  assert.equal(formatStableAmount(10_000_100_000n), "10000.1");
});

test("USDT Stable.com legs apply the ten-basis-point fee", () => {
  assert.equal(stableOutputRaw("USDC", "USDT", 100_000_000_000n), 99_900_000_000n);
  assert.equal(stableOutputRaw("USDT", "USDC", 100_000_000_000n), 99_900_000_000n);
  assert.equal(stableOutputRaw("USDG", "USDC", 100_000_000_000n), 100_000_000_000n);
});

test("default candidate grid spans 10k through 100k", () => {
  assert.deepEqual(
    generateCandidateSizesRaw(usdcToRaw("10000"), usdcToRaw("100000")),
    ["10000", "20000", "50000", "100000"].map(usdcToRaw),
  );
});

test("refinement never escapes configured bounds", () => {
  const minimum = usdcToRaw("10000");
  const maximum = usdcToRaw("100000");
  const values = generateRefinementSizesRaw(usdcToRaw("100000"), minimum, maximum);
  assert.ok(values.every((value) => value >= minimum && value <= maximum));
  assert.ok(values.includes(usdcToRaw("95000")));
});
