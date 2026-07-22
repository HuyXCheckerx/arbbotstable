import type { Connection, PublicKey } from "@solana/web3.js";
import {
  generateCandidateSizesRaw,
  generateRefinementSizesRaw,
  stableOutputRaw,
} from "./amounts.js";
import { config } from "./config.js";
import {
  DIRECT_ROUTES,
  TOKEN_CONFIG,
  type IntermediateToken,
  type TokenSymbol,
  type VenueOrder,
} from "./constants.js";
import { describeJupiterRoute, getJupiterBuild } from "./jupiter.js";
import type { RouteQuote } from "./types.js";

export type RouteDefinition = {
  token: IntermediateToken;
  venueOrder: VenueOrder;
};

function protectedReserve(route: RouteDefinition): bigint {
  if (route.venueOrder === "stable_first") return config.stableTokenReserveRaw;
  return route.token === "USDT" ? config.usdtUsdcReserveRaw : config.stableUsdcReserveRaw;
}

export async function quoteRoute(params: {
  route: RouteDefinition;
  principalRaw: bigint;
  wallet: PublicKey;
  poolBalances: Record<TokenSymbol, bigint>;
}): Promise<RouteQuote | null> {
  const { route, principalRaw, wallet, poolBalances } = params;
  const token = TOKEN_CONFIG[route.token];
  const stableFirst = route.venueOrder === "stable_first";
  const stableInputRaw = stableFirst ? principalRaw : 0n;
  const stableFirstOutputRaw = stableFirst
    ? stableOutputRaw("USDC", route.token, stableInputRaw)
    : 0n;
  if (
    stableFirst &&
    poolBalances[route.token] - protectedReserve(route) < stableFirstOutputRaw
  ) {
    return null;
  }

  const build = await getJupiterBuild({
    inputMint: stableFirst ? token.mint : TOKEN_CONFIG.USDC.mint,
    outputMint: stableFirst ? TOKEN_CONFIG.USDC.mint : token.mint,
    amount: stableFirst ? stableFirstOutputRaw : principalRaw,
    taker: wallet,
  });
  const expectedJupiterOutputRaw = BigInt(build.outAmount);
  const minimumJupiterOutputRaw = BigInt(build.otherAmountThreshold);
  const finalStableInputRaw = stableFirst ? stableInputRaw : minimumJupiterOutputRaw;
  const finalStableOutputRaw = stableFirst
    ? stableFirstOutputRaw
    : stableOutputRaw(route.token, "USDC", finalStableInputRaw);
  const expectedFinalUsdcRaw = stableFirst
    ? expectedJupiterOutputRaw
    : stableOutputRaw(route.token, "USDC", expectedJupiterOutputRaw);
  const guaranteedFinalUsdcRaw = stableFirst
    ? minimumJupiterOutputRaw
    : finalStableOutputRaw;

  if (
    !stableFirst &&
    poolBalances.USDC - protectedReserve(route) < finalStableOutputRaw
  ) {
    return null;
  }

  const expectedGrossProfitRaw = expectedFinalUsdcRaw - principalRaw;
  const guaranteedGrossProfitRaw = guaranteedFinalUsdcRaw - principalRaw;
  return {
    token: route.token,
    venueOrder: route.venueOrder,
    principalRaw,
    jupiterInputRaw: stableFirst ? stableFirstOutputRaw : principalRaw,
    jupiterExpectedOutputRaw: expectedJupiterOutputRaw,
    jupiterMinimumOutputRaw: minimumJupiterOutputRaw,
    stableInputRaw: finalStableInputRaw,
    stableOutputRaw: finalStableOutputRaw,
    expectedGrossProfitRaw,
    guaranteedGrossProfitRaw,
    expectedNetProfitRaw: expectedGrossProfitRaw - config.estimatedTxCostRaw,
    guaranteedNetProfitRaw: guaranteedGrossProfitRaw - config.estimatedTxCostRaw,
    expectedIntermediateSurplusRaw: stableFirst
      ? 0n
      : expectedJupiterOutputRaw - minimumJupiterOutputRaw,
    build,
  };
}

export function isEligible(quote: RouteQuote): boolean {
  return (
    quote.principalRaw >= config.minTradeRaw &&
    quote.principalRaw <= config.maxTradeRaw &&
    quote.guaranteedNetProfitRaw >= config.minNetProfitRaw
  );
}

export function routeLabel(route: Pick<RouteQuote, "token" | "venueOrder">): string {
  return route.venueOrder === "stable_first"
    ? `USDC --Stable--> ${route.token} --Jupiter--> USDC`
    : `USDC --Jupiter--> ${route.token} --Stable--> USDC`;
}

export function routeDetail(quote: RouteQuote): string {
  return `${routeLabel(quote)} [${describeJupiterRoute(quote.build)}]`;
}

async function delay(): Promise<void> {
  if (config.quoteDelayMs <= 0) return;
  await new Promise((resolve) => setTimeout(resolve, config.quoteDelayMs));
}

async function safelyQuote(params: Parameters<typeof quoteRoute>[0]): Promise<RouteQuote | null> {
  try {
    return await quoteRoute(params);
  } catch (error) {
    console.warn(
      `[quote] ${params.route.token}/${params.route.venueOrder} ${params.principalRaw}: ${(error as Error).message}`,
    );
    return null;
  }
}

export async function scanRoutes(params: {
  connection: Connection;
  wallet: PublicKey;
  poolBalances: Record<TokenSymbol, bigint>;
}): Promise<{ best: RouteQuote | null; all: RouteQuote[] }> {
  const sizes = generateCandidateSizesRaw(config.minTradeRaw, config.maxTradeRaw);
  const evaluated: RouteQuote[] = [];
  for (const definition of DIRECT_ROUTES) {
    const route: RouteDefinition = { ...definition };
    for (const principalRaw of sizes) {
      const quote = await safelyQuote({
        route,
        principalRaw,
        wallet: params.wallet,
        poolBalances: params.poolBalances,
      });
      if (quote) evaluated.push(quote);
      await delay();
    }
  }

  const coarseBest = evaluated
    .filter(isEligible)
    .sort((a, b) =>
      a.guaranteedNetProfitRaw > b.guaranteedNetProfitRaw
        ? -1
        : a.guaranteedNetProfitRaw < b.guaranteedNetProfitRaw
          ? 1
          : 0,
    )[0];
  if (!coarseBest) return { best: null, all: evaluated };

  const refinementSizes = generateRefinementSizesRaw(
    coarseBest.principalRaw,
    config.minTradeRaw,
    config.maxTradeRaw,
  ).filter((size) => !sizes.includes(size));
  for (const principalRaw of refinementSizes) {
    const quote = await safelyQuote({
      route: { token: coarseBest.token, venueOrder: coarseBest.venueOrder },
      principalRaw,
      wallet: params.wallet,
      poolBalances: params.poolBalances,
    });
    if (quote) evaluated.push(quote);
    await delay();
  }

  const best = evaluated
    .filter(isEligible)
    .sort((a, b) =>
      a.guaranteedNetProfitRaw > b.guaranteedNetProfitRaw
        ? -1
        : a.guaranteedNetProfitRaw < b.guaranteedNetProfitRaw
          ? 1
          : 0,
    )[0] ?? null;
  return { best, all: evaluated };
}
