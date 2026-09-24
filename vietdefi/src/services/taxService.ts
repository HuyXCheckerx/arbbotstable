/**
 * Vietnam Crypto Tax Calculation Engine
 * Aligned with Resolution 05/2025/NQ-CP, Law on Personal Income Tax (PIT / Thuế TNCN),
 * and General Department of Taxation (Tổng Cục Thuế) guidelines.
 */

export type CostBasisMethod = "fifo" | "specific_id" | "weighted_average";

export interface CryptoTransaction {
  id: string;
  timestamp: string;
  txHash: string;
  type: "swap" | "lending_yield" | "fiat_exit" | "collateral_borrow";
  assetIn: string;
  amountIn: number;
  priceInVND: number;
  assetOut: string;
  amountOut: number;
  priceOutVND: number;
  feeInVND: number;
}

export interface TaxReportSummary {
  taxYear: number;
  costBasisMethod: CostBasisMethod;
  totalGrossProceedsVND: number;
  totalCostBasisVND: number;
  netTaxableGainVND: number;
  totalLendingYieldVND: number;
  estimatedTaxPayableVND: number; // 5% for capital gains or flat withholding under NQ 05 sandbox
  taxFormName: string;            // 'Mẫu 02/KK-TNCN (Tài Sản Số)'
  transactionsCount: number;
}

/**
 * Standard FIFO Cost-Basis Tax Calculator
 */
export function calculateVietnamCryptoTax(
  transactions: CryptoTransaction[],
  method: CostBasisMethod = "fifo",
  taxYear = 2026
): TaxReportSummary {
  const filtered = transactions.filter(
    (tx) => new Date(tx.timestamp).getFullYear() === taxYear
  );

  let totalGrossProceedsVND = 0;
  let totalCostBasisVND = 0;
  let totalLendingYieldVND = 0;

  // Inventory queue for FIFO: asset -> array of { amount, unitCostVND }
  const inventory: Record<string, Array<{ amount: number; unitCostVND: number }>> = {};

  for (const tx of filtered) {
    if (tx.type === "lending_yield") {
      // Lending income in Vietnam is treated as capital investment income
      const yieldVND = tx.amountIn * tx.priceInVND;
      totalLendingYieldVND += yieldVND;
      continue;
    }

    if (tx.type === "swap" || tx.type === "fiat_exit") {
      const proceeds = tx.amountOut * tx.priceOutVND - tx.feeInVND;
      totalGrossProceedsVND += proceeds;

      // Calculate cost basis for asset sold (assetIn)
      let costBasis = 0;
      let remainingToCost = tx.amountIn;

      const queue = inventory[tx.assetIn] || [];

      if (method === "fifo" && queue.length > 0) {
        while (remainingToCost > 0 && queue.length > 0) {
          const batch = queue[0];
          if (batch.amount <= remainingToCost) {
            costBasis += batch.amount * batch.unitCostVND;
            remainingToCost -= batch.amount;
            queue.shift();
          } else {
            costBasis += remainingToCost * batch.unitCostVND;
            batch.amount -= remainingToCost;
            remainingToCost = 0;
          }
        }
      } else {
        // Fallback / Weighted average / immediate acquisition cost
        costBasis = tx.amountIn * tx.priceInVND;
      }

      totalCostBasisVND += costBasis;

      // Add acquired asset (assetOut) to inventory
      if (!inventory[tx.assetOut]) {
        inventory[tx.assetOut] = [];
      }
      inventory[tx.assetOut].push({
        amount: tx.amountOut,
        unitCostVND: tx.priceOutVND,
      });
    }
  }

  const netCapitalGains = Math.max(0, totalGrossProceedsVND - totalCostBasisVND);
  const netTaxableGainVND = netCapitalGains + totalLendingYieldVND;

  // Under Vietnamese Tax Sandbox (Resolution 05/2025/NQ-CP pilot proposal):
  // 5% flat rate on net capital transfer gains or 0.1% transaction turnover tax
  // Here calculated as 5% on net taxable profit
  const estimatedTaxPayableVND = Math.round(netTaxableGainVND * 0.05);

  return {
    taxYear,
    costBasisMethod: method,
    totalGrossProceedsVND: Math.round(totalGrossProceedsVND),
    totalCostBasisVND: Math.round(totalCostBasisVND),
    netTaxableGainVND: Math.round(netTaxableGainVND),
    totalLendingYieldVND: Math.round(totalLendingYieldVND),
    estimatedTaxPayableVND,
    taxFormName: "Mẫu số 02/KK-TNCN (Tài Sản Kỹ Thuật Số)",
    transactionsCount: filtered.length,
  };
}

export function formatVND(amount: number): string {
  return new Intl.NumberFormat("vi-VN", {
    style: "currency",
    currency: "VND",
  }).format(amount);
}
