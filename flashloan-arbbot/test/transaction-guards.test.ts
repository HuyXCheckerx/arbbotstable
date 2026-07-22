import assert from "node:assert/strict";
import test from "node:test";
import type { Connection } from "@solana/web3.js";
import { Keypair } from "@solana/web3.js";
import { config } from "../src/config.js";
import { prepareAtomicTransaction } from "../src/transaction.js";
import type { RouteQuote } from "../src/types.js";

function quote(overrides: Partial<RouteQuote>): RouteQuote {
  return {
    token: "USDG",
    venueOrder: "stable_first",
    principalRaw: config.minTradeRaw,
    jupiterInputRaw: config.minTradeRaw,
    jupiterExpectedOutputRaw: config.minTradeRaw,
    jupiterMinimumOutputRaw: config.minTradeRaw,
    stableInputRaw: config.minTradeRaw,
    stableOutputRaw: config.minTradeRaw,
    expectedGrossProfitRaw: 0n,
    guaranteedGrossProfitRaw: 0n,
    expectedNetProfitRaw: 0n,
    guaranteedNetProfitRaw: 0n,
    expectedIntermediateSurplusRaw: 0n,
    build: {} as RouteQuote["build"],
    ...overrides,
  };
}

test("transaction construction rejects amounts outside the hard configured range", async () => {
  await assert.rejects(
    prepareAtomicTransaction({
      connection: {} as Connection,
      wallet: Keypair.generate().publicKey,
      quote: quote({
        principalRaw: config.minTradeRaw - 1n,
        guaranteedNetProfitRaw: config.minNetProfitRaw,
      }),
    }),
    /outside the configured 10,000–100,000 USDC range/,
  );
});

test("transaction construction rejects a quote below the guaranteed profit floor", async () => {
  await assert.rejects(
    prepareAtomicTransaction({
      connection: {} as Connection,
      wallet: Keypair.generate().publicKey,
      quote: quote({ guaranteedNetProfitRaw: config.minNetProfitRaw - 1n }),
    }),
    /below the guaranteed net-profit floor/,
  );
});
