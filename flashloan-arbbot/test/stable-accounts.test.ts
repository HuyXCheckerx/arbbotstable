import assert from "node:assert/strict";
import test from "node:test";
import { Keypair } from "@solana/web3.js";
import { getStableSwapAccountMetas } from "../src/stable.js";

test("Stable.com direct instruction uses the expected 18 account metas", () => {
  const wallet = Keypair.generate().publicKey;
  const metas = getStableSwapAccountMetas(wallet, "USDC", "USDG");
  assert.equal(metas.length, 18);
  assert.equal(metas[0].isSigner, true);
  assert.equal(metas[0].isWritable, true);
  assert.equal(metas[17].isWritable, false);
});
