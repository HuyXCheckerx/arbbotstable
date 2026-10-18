Output before network fees. Δ = local − aggregator, in bp of the aggregator's output; positive means the local quote pays more.

| chain | pair | size | local out | local ms | sync ms | Δ ParaSwap | Δ ParaSwap (no RFQ) | Δ CoW | Δ LI.FI | aggregator ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| eth | USDC->USDT | 1000 | 1001.18 | 12 | 2452 | – | – | +0.89 | -0.12 | 2065 / 905 |
| eth | USDC->USDT | 10000 | 10011.77 | 22 | 213 | – | – | +0.30 | -0.02 | 1511 / 765 |
| eth | USDC->USDT | 100000 | 100117.31 | 13 | 2117 | – | – | +0.30 | -0.13 | 642 / 771 |
| eth | USDC->USDT | 1000000 | 1001146.65 | 19 | 213 | – | – | +0.30 | -0.33 | 1500 / 856 |
| eth | USDT->USDC | 1000 | 998.83 | 10 | 2606 | – | – | +0.65 | -1.20 | 1233 / 826 |
| eth | USDT->USDC | 10000 | 9988.26 | 13 | 2137 | – | – | +0.28 | -0.60 | 1248 / 793 |
| eth | USDT->USDC | 100000 | 99881.69 | 19 | 223 | – | – | +0.25 | -0.38 | 986 / 967 |
| eth | USDT->USDC | 1000000 | 998789.94 | 19 | 2075 | – | – | +0.23 | -0.36 | 1823 / 973 |
| polygon | USDC->USDT | 1000 | 1001.15 | 12 | 2165 | – | – | +0.30 | +0.00 | 1460 / 634 |
| polygon | USDC->USDT | 10000 | 10011.48 | 13 | 1668 | – | – | +0.28 | -0.00 | 1516 / 522 |
| polygon | USDC->USDT | 100000 | 100110.48 | 15 | 2545 | – | – | +0.09 | -0.12 | 1630 / 847 |
| polygon | USDC->USDT | 1000000 | 1000507.98 | 25 | 2121 | – | – | -4.21 | -4.41 | 4010 / 834 |
| polygon | USDT->USDC | 1000 | 998.83 | 8 | 1743 | – | – | +0.31 | -0.08 | 1424 / 582 |
| polygon | USDT->USDC | 10000 | 9988.19 | 11 | 1399 | – | – | +0.23 | -0.11 | 1377 / 809 |
| polygon | USDT->USDC | 100000 | 99875.47 | 26 | 1715 | – | – | +0.20 | -0.10 | 2094 / 811 |

Net of gas: local net (legs + 60000 base gas at the chain's gas price) vs each aggregator's output minus its own gas estimate (CoW: its fee-reduced buy amount).

| chain | pair | size | local net | local gas | Δ ParaSwap | Δ ParaSwap (no RFQ) | Δ CoW | Δ LI.FI |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| eth | USDC->USDT | 1000 | 1001.1599 | 145926 | n/a | n/a | +2.07 | +0.50 |
| eth | USDC->USDT | 10000 | 10011.7356 | 205671 | n/a | n/a | +0.59 | +0.03 |
| eth | USDC->USDT | 100000 | 100117.2653 | 291609 | n/a | n/a | +0.35 | -0.12 |
| eth | USDC->USDT | 1000000 | 1001146.5747 | 491981 | n/a | n/a | +0.32 | -0.33 |
| eth | USDT->USDC | 1000 | 998.8095 | 143257 | n/a | n/a | +2.33 | -0.30 |
| eth | USDT->USDC | 10000 | 9988.2254 | 226514 | n/a | n/a | +0.65 | -0.42 |
| eth | USDT->USDC | 100000 | 99881.6294 | 415043 | n/a | n/a | +0.39 | -0.38 |
| eth | USDT->USDC | 1000000 | 998789.8649 | 541229 | n/a | n/a | +0.24 | -0.36 |
| polygon | USDC->USDT | 1000 | 1001.1501 | 137921 | n/a | n/a | +0.38 | +0.11 |
| polygon | USDC->USDT | 10000 | 10011.4806 | 137921 | n/a | n/a | +0.30 | +0.01 |
| polygon | USDC->USDT | 100000 | 100110.4680 | 642422 | n/a | n/a | +0.10 | -0.11 |
| polygon | USDC->USDT | 1000000 | 1000507.9440 | 1234210 | n/a | n/a | -4.21 | -4.41 |
| polygon | USDT->USDC | 1000 | 998.8229 | 137947 | n/a | n/a | +0.38 | +0.05 |
| polygon | USDT->USDC | 10000 | 9988.1878 | 137947 | n/a | n/a | +0.25 | -0.06 |
| polygon | USDT->USDC | 100000 | 99875.4577 | 550573 | n/a | n/a | +0.21 | -0.10 |

Not quoted: polygon USDT->USDC 1000000: direct pools cannot absorb 1000000.000000 USDT within a ~20% price move (--full-fill quotes it anyway) (aggregators: CoW 996058099239, LI.FI 993467413499)

Routes:

- eth USDC->USDT 1000: local DODO PMM 100.0%
  - CoW: fee 140321 USDC
  - LI.FI: nordstern
- eth USDC->USDT 10000: local Fluid DEX 100.0%
  - CoW: fee 320843 USDC
  - LI.FI: nordstern
- eth USDC->USDT 100000: local Fluid DEX 96.5% + DODO PMM 3.5%
  - CoW: fee 581783 USDC
  - LI.FI: nordstern
- eth USDC->USDT 1000000: local Fluid DEX 66.9% + Uniswap v4 dyn hooked 21.7% + Uniswap v4 0.0007% 9.5% + DODO PMM 1.9%
  - CoW: fee 1936450 USDC
  - LI.FI: nordstern
- eth USDT->USDC 1000: local Uniswap v4 0.0008% 100.0%
  - CoW: fee 188136 USDT
  - LI.FI: okx
- eth USDT->USDC 10000: local Uniswap v4 0.0008% 56.8% + Uniswap v4 0.0007% 43.2%
  - CoW: fee 393245 USDT
  - LI.FI: okx
- eth USDT->USDC 100000: local Uniswap v4 dyn hooked 44.3% + Uniswap v4 0.0007% 36.9% + Uniswap v4 0.0008% 16.2% + DODO PMM 2.5%
  - CoW: fee 1520627 USDT
  - LI.FI: nordstern
- eth USDT->USDC 1000000: local Fluid DEX 56.2% + Uniswap v4 dyn hooked 24.5% + Uniswap v4 0.0007% 15.2% + Uniswap v4 0.0008% 4.0%
  - CoW: fee 1320087 USDT
  - LI.FI: nordstern
- polygon USDC->USDT 1000: local Uniswap v4 0.0008% 100.0%
  - CoW: fee 11852 USDC
  - LI.FI: sushiswap
- polygon USDC->USDT 10000: local Uniswap v4 0.0008% 100.0%
  - CoW: fee 31221 USDC
  - LI.FI: sushiswap
- polygon USDC->USDT 100000: local Uniswap v4 0.0008% 68.1% + Uniswap v3 0.01% 29.5% + Fluid DEX 1.4% + QuickSwap v3 dyn 1.0%
  - CoW: fee 111708 USDC
  - LI.FI: okx
- polygon USDC->USDT 1000000: local Uniswap v3 0.01% 56.5% + Uniswap v4 0.0008% 39.3% + Fluid DEX 2.7% + QuickSwap v3 dyn 1.5%
  - CoW: fee 113973 USDC
  - LI.FI: bitget
- polygon USDT->USDC 1000: local Uniswap v4 0.0008% 100.0%
  - CoW: fee 10850 USDT
  - LI.FI: fly
- polygon USDT->USDC 10000: local Uniswap v4 0.0008% 100.0%
  - CoW: fee 27314 USDT
  - LI.FI: kyberswap
- polygon USDT->USDC 100000: local Uniswap v4 0.0008% 94.8% + Uniswap v3 0.01% 2.9% + QuickSwap v3 dyn 1.8% + Uniswap v4 0.0009% 0.5%
  - CoW: fee 149805 USDT
  - LI.FI: bitget
