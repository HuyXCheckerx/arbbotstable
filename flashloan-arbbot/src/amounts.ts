import { RAW_PER_USDC } from "./constants.js";

export function usdcToRaw(value: string | number): bigint {
  const text = String(value).trim();
  if (!/^\d+(?:\.\d{1,6})?$/.test(text)) {
    throw new Error(`Invalid six-decimal USDC amount: ${text}`);
  }
  const [whole, fractional = ""] = text.split(".");
  return BigInt(whole) * RAW_PER_USDC + BigInt(fractional.padEnd(6, "0"));
}

export function rawToUsdc(raw: bigint): string {
  const sign = raw < 0n ? "-" : "";
  const absolute = raw < 0n ? -raw : raw;
  const whole = absolute / RAW_PER_USDC;
  const fractional = (absolute % RAW_PER_USDC).toString().padStart(6, "0");
  return `${sign}${whole}.${fractional}`;
}

export function formatStableAmount(raw: bigint): string {
  const rendered = rawToUsdc(raw);
  return rendered.replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
}

export function stableOutputRaw(assetFrom: string, assetTo: string, inputRaw: bigint): bigint {
  const pair = new Set([assetFrom.toUpperCase(), assetTo.toUpperCase()]);
  if (pair.has("USDC") && pair.has("USDT")) {
    return (inputRaw * 9_990n) / 10_000n;
  }
  return inputRaw;
}

export function generateCandidateSizesRaw(minRaw: bigint, maxRaw: bigint): bigint[] {
  if (minRaw <= 0n || maxRaw < minRaw) return [];
  const values = new Set<bigint>([minRaw, maxRaw]);
  for (const multiplier of [2n, 5n]) {
    const candidate = minRaw * multiplier;
    if (candidate <= maxRaw) values.add(candidate);
  }
  return [...values].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

export function generateRefinementSizesRaw(
  anchorRaw: bigint,
  minRaw: bigint,
  maxRaw: bigint,
): bigint[] {
  const ratios: Array<[bigint, bigint]> = [
    [3n, 4n],
    [9n, 10n],
    [19n, 20n],
    [21n, 20n],
    [11n, 10n],
    [5n, 4n],
  ];
  const values = new Set<bigint>();
  for (const [numerator, denominator] of ratios) {
    const candidate = (anchorRaw * numerator) / denominator;
    if (candidate >= minRaw && candidate <= maxRaw) values.add(candidate);
  }
  return [...values].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}
