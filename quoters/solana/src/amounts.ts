export const U64_MAX = (1n << 64n) - 1n;
export const FEE_SCALE = 1n << 60n;

export function atomic(value: string, decimals = 6): bigint {
  if (!/^\d+(\.\d+)?$/.test(value)) throw new Error(`Invalid unsigned decimal amount: ${value}`);
  const [whole, fraction = ""] = value.split(".");
  if (fraction.length > decimals) throw new Error(`Amount exceeds ${decimals} decimal places`);
  const result = BigInt(whole) * 10n ** BigInt(decimals) + BigInt(fraction.padEnd(decimals, "0") || "0");
  if (result > U64_MAX) throw new Error("Amount exceeds u64");
  return result;
}

export function ui(value: bigint, decimals = 6): string {
  const sign = value < 0n ? "-" : "";
  const abs = value < 0n ? -value : value;
  const unit = 10n ** BigInt(decimals);
  return `${sign}${abs / unit}.${(abs % unit).toString().padStart(decimals, "0")}`;
}

// Kamino uses 60 fractional bits. Round up conservatively; U64_MAX disables flash loans.
export function flashFee(principal: bigint, feeSf: bigint): bigint {
  if (principal <= 0n || principal > U64_MAX) throw new Error("Invalid flash-loan principal");
  if (feeSf < 0n || feeSf >= U64_MAX) throw new Error("Flash loans disabled or invalid fee");
  if (feeSf === 0n) return 0n;
  const fee = (principal * feeSf + FEE_SCALE - 1n) / FEE_SCALE;
  if (fee >= principal) throw new Error("Flash fee consumes principal");
  return fee > 0n ? fee : 1n;
}
