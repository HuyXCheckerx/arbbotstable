Output before network fees. Δ = local − aggregator, in bp of the aggregator's output; positive means the local quote pays more.

| chain | pair | size | local out | local ms | sync ms | Δ ParaSwap | Δ ParaSwap (no RFQ) | Δ CoW | Δ LI.FI | aggregator ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| eth | USDC->USDT | 1000 | 1001.10 | 16 | 1842 | n/a | – | +0.14 | +0.09 | 990 / 1634 / 830 |
| eth | USDC->USDT | 10000 | 10011.27 | 6 | 2253 | n/a | – | +0.30 | -0.01 | 1030 / 1242 / 901 |
| eth | USDC->USDT | 100000 | 100109.88 | 14 | 1898 | n/a | – | +0.29 | -0.27 | 970 / 1373 / 783 |
| eth | USDC->USDT | 1000000 | 1001075.64 | 17 | 2644 | n/a | – | +0.21 | -0.17 | 1098 / 2090 / 887 |
| eth | USDT->USDC | 1000 | 998.89 | 13 | 2299 | n/a | – | +0.16 | -0.03 | 1028 / 2022 / 700 |
| eth | USDT->USDC | 10000 | 9988.87 | 13 | 2276 | n/a | – | +0.28 | -0.14 | 1107 / 1734 / 917 |
| eth | USDT->USDC | 100000 | 99888.20 | 861 | 2103 | n/a | – | +0.27 | -0.13 | 989 / 2457 / 843 |
| eth | USDT->USDC | 1000000 | 998857.96 | 20 | 2127 | n/a | – | +0.18 | -0.33 | 1033 / 2737 / 909 |
| bsc | USDC->USDT | 1000 | 1001.12 | 243 | 1281 | n/a | – | -0.57 | -0.16 | 1068 / 1851 / 883 |
| bsc | USDC->USDT | 10000 | 10011.07 | 227 | 1928 | n/a | – | -0.64 | -0.32 | 1022 / 1477 / 690 |
| bsc | USDC->USDT | 100000 | 100108.38 | 234 | 1955 | n/a | – | -0.35 | -0.33 | 1288 / 2021 / 1007 |
| bsc | USDC->USDT | 1000000 | 1001044.33 | 243 | 1592 | n/a | – | +0.14 | -0.09 | 1088 / 2262 / 1111 |
| bsc | USDT->USDC | 1000 | 998.87 | 221 | 2228 | n/a | – | +0.30 | -0.32 | 2655 / 1593 / 708 |
| bsc | USDT->USDC | 10000 | 9988.52 | 223 | 2177 | n/a | – | +0.24 | -0.16 | 1090 / 1877 / 894 |
| bsc | USDT->USDC | 100000 | 99879.83 | 300 | 1529 | n/a | – | -0.37 | -0.72 | 1031 / 3486 / 930 |
| bsc | USDT->USDC | 1000000 | 998661.39 | 452 | 1836 | n/a | – | -0.35 | -0.91 | 969 / 2206 / 927 |
| polygon | USDC->USDT | 1000 | 1001.10 | 9 | 5136 | n/a | – | +0.30 | -0.01 | 984 / 1496 / 1055 |
| polygon | USDC->USDT | 10000 | 10010.99 | 7 | 1684 | n/a | – | +0.30 | -0.00 | 1203 / 2287 / 785 |
| polygon | USDC->USDT | 100000 | 100106.08 | 6170 | 1482 | n/a | – | +0.24 | -0.07 | 1746 / 2623 / 1421 |
| polygon | USDC->USDT | 1000000 | 1000557.79 | 22 | 1627 | n/a | – | -3.19 | -3.45 | 1479 / 2427 / 894 |
| polygon | USDT->USDC | 1000 | 998.87 | 225 | 1619 | n/a | – | +0.16 | -0.00 | 1014 / 4510 / 672 |
| polygon | USDT->USDC | 10000 | 9988.69 | 1157 | 1640 | n/a | – | +0.15 | -0.00 | 1016 / 1439 / 591 |
| polygon | USDT->USDC | 100000 | 99881.79 | 5127 | 1578 | n/a | – | +0.06 | -0.09 | 1097 / 1806 / 893 |

Net of gas: local net (legs + 60000 base gas at the chain's gas price) vs each aggregator's output minus its own gas estimate (CoW: its fee-reduced buy amount).

| chain | pair | size | local net | local gas | Δ ParaSwap | Δ ParaSwap (no RFQ) | Δ CoW | Δ LI.FI |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| eth | USDC->USDT | 1000 | 1001.0873 | 145926 | n/a | n/a | +0.67 | +5.92 |
| eth | USDC->USDT | 10000 | 10011.2571 | 145913 | n/a | n/a | +0.36 | +0.02 |
| eth | USDC->USDT | 100000 | 100109.8412 | 396637 | n/a | n/a | +0.30 | -0.27 |
| eth | USDC->USDT | 1000000 | 1001075.5784 | 549048 | n/a | n/a | +0.21 | -0.17 |
| eth | USDT->USDC | 1000 | 998.8775 | 147567 | n/a | n/a | +1.03 | +0.20 |
| eth | USDT->USDC | 10000 | 9988.8445 | 286789 | n/a | n/a | +0.40 | -0.13 |
| eth | USDT->USDC | 100000 | 99888.1742 | 286789 | n/a | n/a | +0.29 | -0.13 |
| eth | USDT->USDC | 1000000 | 998857.9136 | 477655 | n/a | n/a | +0.18 | -0.33 |
| bsc | USDC->USDT | 1000 | 1001.1151 | 131330 | n/a | n/a | -0.45 | -0.10 |
| bsc | USDC->USDT | 10000 | 10011.0606 | 366975 | n/a | n/a | -0.64 | -0.32 |
| bsc | USDC->USDT | 100000 | 100108.3599 | 460882 | n/a | n/a | -0.35 | -0.32 |
| bsc | USDC->USDT | 1000000 | 1001044.3180 | 435946 | n/a | n/a | +0.14 | -0.09 |
| bsc | USDT->USDC | 1000 | 998.8651 | 130295 | n/a | n/a | +0.31 | -0.26 |
| bsc | USDT->USDC | 10000 | 9988.5062 | 362823 | n/a | n/a | +0.24 | -0.16 |
| bsc | USDT->USDC | 100000 | 99879.8109 | 473875 | n/a | n/a | -0.37 | -0.71 |
| bsc | USDT->USDC | 1000000 | 998661.3687 | 676958 | n/a | n/a | -0.35 | -0.91 |
| polygon | USDC->USDT | 1000 | 1001.1010 | 137921 | n/a | n/a | +0.36 | +0.10 |
| polygon | USDC->USDT | 10000 | 10010.9854 | 137921 | n/a | n/a | +0.31 | +0.01 |
| polygon | USDC->USDT | 100000 | 100106.0622 | 612233 | n/a | n/a | +0.24 | -0.06 |
| polygon | USDC->USDT | 1000000 | 1000557.7653 | 1138937 | n/a | n/a | -3.19 | -3.45 |
| polygon | USDT->USDC | 1000 | 998.8713 | 137947 | n/a | n/a | +0.32 | +0.09 |
| polygon | USDT->USDC | 10000 | 9988.6893 | 137947 | n/a | n/a | +0.17 | +0.01 |
| polygon | USDT->USDC | 100000 | 99881.7792 | 460255 | n/a | n/a | +0.06 | -0.09 |

Not quoted: polygon USDT->USDC 1000000: direct pools cannot absorb 1000000.000000 USDT within a ~20% price move (--full-fill quotes it anyway) (aggregators: ParaSwap n/a, CoW 997572028689, LI.FI 995136322245)

Routes:

- eth USDC->USDT 1000: local DODO PMM 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 69093 USDC
  - LI.FI: sushiswap
- eth USDC->USDT 10000: local DODO PMM 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 76811 USDC
  - LI.FI: bitget
- eth USDC->USDT 100000: local Fluid DEX 46.9% + Uniswap v4 dyn hooked 37.0% + Uniswap v4 0.0007% 16.1%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 110445 USDC
  - LI.FI: nordstern
- eth USDC->USDT 1000000: local Fluid DEX 58.4% + Uniswap v4 dyn hooked 23.6% + Uniswap v4 0.0007% 13.9% + Uniswap v4 0.0008% 4.1%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 282123 USDC
  - LI.FI: nordstern
- eth USDT->USDC 1000: local DODO PMM 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 101727 USDT
  - LI.FI: nordstern
- eth USDT->USDC 10000: local Fluid DEX 59.1% + DODO PMM 40.9%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 150330 USDT
  - LI.FI: nordstern
- eth USDT->USDC 100000: local Fluid DEX 92.7% + DODO PMM 7.3%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 223148 USDT
  - LI.FI: nordstern
- eth USDT->USDC 1000000: local Fluid DEX 62.3% + Uniswap v4 dyn hooked 21.9% + Uniswap v4 0.0007% 11.8% + Uniswap v4 0.0008% 3.9%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 298185 USDT
  - LI.FI: nordstern
- bsc USDC->USDT 1000: local Uniswap v4 0.0002% 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 16970281765130400 USDC
  - LI.FI: nordstern
- bsc USDC->USDT 10000: local Uniswap v4 0.0006% 78.5% + Uniswap v4 0.0002% 13.2% + Uniswap v4 0.0001% 5.0% + Pancake Infinity CL 0.0001% 3.4%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 16970281765130400 USDC
  - LI.FI: nordstern
- bsc USDC->USDT 100000: local PancakeSwap v3 0.01% 66.3% + Uniswap v4 0.0006% 15.5% + Pancake Infinity CL 0.0001% hooked 11.2% + Uniswap v4 0.0002% 7.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 21305395752300416 USDC
  - LI.FI: nordstern
- bsc USDC->USDT 1000000: local PancakeSwap v3 0.01% 94.0% + Uniswap v4 0.0006% 3.5% + Uniswap v4 0.0002% 2.1% + Uniswap v4 0.0001% 0.3%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 25640509739470432 USDC
  - LI.FI: bitget
- bsc USDT->USDC 1000: local Uniswap v4 0.0002% 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 5804493647380260 USDT
  - LI.FI: nordstern
- bsc USDT->USDC 10000: local Uniswap v4 0.0006% 75.7% + Uniswap v4 0.0002% 15.1% + Uniswap v4 0.0001% 5.5% + Pancake Infinity CL 0.0001% 3.7%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 13369199051137256 USDT
  - LI.FI: nordstern
- bsc USDT->USDC 100000: local Uniswap v4 0.0006% 66.2% + Pancake Infinity CL 0.0001% hooked 25.6% + Uniswap v4 0.0002% 4.8% + Uniswap v4 0.0001% 3.3%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 8354083215981117 USDT
  - LI.FI: nordstern
- bsc USDT->USDC 1000000: local PancakeSwap v3 0.01% 81.9% + Uniswap v4 0.0006% 9.8% + PancakeSwap v3 0.05% 7.0% + Uniswap v3 0.01% 1.3%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 110236702720890384 USDT
  - LI.FI: nordstern
- polygon USDC->USDT 1000: local Uniswap v4 0.0008% 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 9121 USDC
  - LI.FI: okx
- polygon USDC->USDT 10000: local Uniswap v4 0.0008% 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 9410 USDC
  - LI.FI: okx
- polygon USDC->USDT 100000: local Uniswap v4 0.0008% 82.4% + Uniswap v3 0.01% 15.9% + Fluid DEX 0.9% + QuickSwap v3 dyn 0.8%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 57694 USDC
  - LI.FI: bitget
- polygon USDC->USDT 1000000: local Uniswap v3 0.01% 55.6% + Uniswap v4 0.0008% 40.8% + Fluid DEX 2.2% + QuickSwap v3 dyn 1.3%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 92276 USDC
  - LI.FI: bitget
- polygon USDT->USDC 1000: local Uniswap v4 0.0008% 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 18603 USDT
  - LI.FI: sushiswap
- polygon USDT->USDC 10000: local Uniswap v4 0.0008% 100.0%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 22377 USDT
  - LI.FI: sushiswap
- polygon USDT->USDC 100000: local Uniswap v4 0.0008% 98.4% + QuickSwap v3 dyn 1.3% + Uniswap v4 0.0009% 0.3%
  - ParaSwap: HTTP 429: {"error":"Rate limit reached. Subscribe to our premium services to extend the rate limit, visit https://portal.velora.xyz"}
  - CoW: fee 58962 USDT
  - LI.FI: bitget
