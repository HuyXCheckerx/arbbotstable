# PYUSD/USDG: local vs 0x (Matcha v2) and 1inch via LlamaSwap

Ethereum, 2026-10-18 ~12:45 (+07). LlamaSwap (swap.defillama.com) driven in the built-in browser at 0.3% slippage; its `dexAggregatorQuote` responses were read from `window.fetch` (exact `amountReturned`). The local quote came from `serve` (warm mirror) within a second of each capture. Δ = local − aggregator in bp of the aggregator's output, before network fees.

| pair | size | local out | 0x out | Δ 0x | 1inch out | Δ 1inch | local leg gas | 0x gas | 1inch gas | block |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| PYUSD->USDG | 1000 | 999.953533 | 999.953533 | +0.0000 | 999.853537 | +1.00 | 307736 | 625445 | 887428 | 26218341 |
| PYUSD->USDG | 10000 | 9999.515568 | 9999.515568 | +0.0000 | 9998.515619 | +1.00 | 307736 | 625481 | 887428 | 26218337 |
| PYUSD->USDG | 100000 | 99993.543435 | 99993.543425 | +0.0000 | 99983.544174 | +1.00 | 411585 | 731903 | 870467 | 26218339 |
| USDG->PYUSD | 1000 | 999.954026 | 999.954219 | -0.0019 | 999.854083 | +1.00 | 306440 | 660583 | 409663 | 26218342 |
| USDG->PYUSD | 10000 | 9999.535997 | 9999.530245 | +0.0058 | 9998.536043 | +1.00 | 367960 | 665776 | 693909 | 26218344 |
| USDG->PYUSD | 100000 | 99995.046309 | 99995.262551 | -0.0216 | 99985.059960 | +1.00 | 543246 | 880974 | 976588 | 26218345 |

Local routes: PYUSD→USDG went PYUSD →(Uniswap v4 0.0005% PYUSD/USDS)→ USDS →(Sky LitePSM)→ USDC →(Maverick v2, plus the hooked Uniswap v4 USDC/USDG at 100k)→ USDG. USDG→PYUSD was the reverse path, through Maverick at 1k, Curve USDG/USDC at 10k, and Curve 66% + Uniswap v4 23% + Maverick 12% at 100k. 0x's output matched the local quote to the atomic unit at 1k and 10k PYUSD→USDG, which suggests the same path. Gas figures differ in scope: 0x and 1inch report whole-transaction estimates; local is the sum of leg gas (the README's 60k base cost comes on top).
