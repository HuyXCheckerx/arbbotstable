// Replays a prestate dump from `evm-quoter dump-state` in @ethereumjs/evm and times each swap.
// Usage: node run.mjs ../artifacts/prestate-eth.json [iterations] [hardfork]
import fs from 'fs';
import { createEVM } from '@ethereumjs/evm';
import { Common, Mainnet, Hardfork } from '@ethereumjs/common';
import { SimpleStateManager } from '@ethereumjs/statemanager';
import { Account, bytesToBigInt, createAddressFromString, hexToBytes } from '@ethereumjs/util';

const d = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const iters = Number(process.argv[3] ?? 200);
const hardfork = process.argv[4] ?? (d.chainId === 1 ? Hardfork.Osaka : Hardfork.Prague);
const common = new Common({ chain: { ...Mainnet, chainId: d.chainId }, hardfork });
const stateManager = new SimpleStateManager();
for (const [a, v] of Object.entries(d.accounts)) {
  const addr = createAddressFromString(a);
  await stateManager.putAccount(addr, new Account(BigInt(v.nonce), BigInt(v.balance)));
  if (v.code && v.code !== '0x') await stateManager.putCode(addr, hexToBytes(v.code));
  for (const [k, val] of Object.entries(v.storage)) await stateManager.putStorage(addr, hexToBytes(k), hexToBytes(val));
}
const evm = await createEVM({ common, stateManager });
const b = d.block;
const block = {
  header: {
    number: BigInt(b.number),
    coinbase: createAddressFromString(b.coinbase),
    timestamp: BigInt(b.timestamp),
    difficulty: 0n,
    prevRandao: hexToBytes(b.prevrandao),
    gasLimit: BigInt(b.gasLimit) > 24_000_000n ? BigInt(b.gasLimit) : 24_000_000n,
    baseFeePerGas: BigInt(b.basefee),
    getBlobGasPrice: () => 1n,
  },
};
const caller = createAddressFromString(d.caller);
const to = createAddressFromString(d.to);

async function once(data) {
  await stateManager.checkpoint();
  const r = await evm.runCall({ caller, origin: caller, to, data, gasLimit: 12_000_000n, gasPrice: BigInt(b.gasPrice), block, skipBalance: true });
  await stateManager.revert();
  return r;
}

for (const c of d.cases) {
  const data = hexToBytes(c.data);
  const r = await once(data);
  const ret = r.execResult.returnValue;
  const out = r.execResult.exceptionError ? `error ${r.execResult.exceptionError.error}` : bytesToBigInt(ret.subarray(0, 32)).toString();
  for (let i = 0; i < Math.min(20, iters); i++) await once(data);
  const t = process.hrtime.bigint();
  for (let i = 0; i < iters; i++) await once(data);
  const us = Number(process.hrtime.bigint() - t) / 1e3 / iters;
  console.log(`ethereumjs ${c.label.padEnd(36)} out ${out} (expected ${c.out})  gas ${String(r.execResult.executionGasUsed).padStart(7)}  ${us.toFixed(1).padStart(8)} us/swap`);
}
