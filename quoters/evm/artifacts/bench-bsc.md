Output before network fees. Δ = local − aggregator, in bp of the aggregator's output; positive means the local quote pays more.

| chain | pair | size | local out | local ms | sync ms | Δ ParaSwap | Δ ParaSwap (no RFQ) | Δ CoW | Δ LI.FI | aggregator ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| bsc | USDC->USDT | 1000 | 1001.14 | 1115 | 1759 | – | – | -0.33 | -0.36 | 2817 / 896 |
| bsc | USDC->USDT | 10000 | 10011.37 | 3285 | 1685 | – | – | -0.34 | -0.43 | 1475 / 623 |
| bsc | USDC->USDT | 100000 | 100112.06 | 237 | 1893 | – | – | -0.04 | -0.21 | 1989 / 621 |
| bsc | USDC->USDT | 1000000 | 1001070.85 | 427 | 1851 | – | – | +0.16 | -0.23 | 2271 / 633 |
| bsc | USDT->USDC | 1000 | 998.84 | 3145 | 2278 | – | – | +0.24 | -0.32 | 1683 / 779 |
| bsc | USDT->USDC | 10000 | 9988.35 | 244 | 2131 | – | – | +0.24 | -0.36 | 1882 / 623 |
| bsc | USDT->USDC | 100000 | 99875.01 | 229 | 1578 | – | – | -0.56 | -1.20 | 1744 / 718 |
| bsc | USDT->USDC | 1000000 | 998637.44 | 233 | 2429 | – | – | -0.26 | -0.64 | 2038 / 717 |

Net of gas: local net (legs + 60000 base gas at the chain's gas price) vs each aggregator's output minus its own gas estimate (CoW: its fee-reduced buy amount).

| chain | pair | size | local net | local gas | Δ ParaSwap | Δ ParaSwap (no RFQ) | Δ CoW | Δ LI.FI |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| bsc | USDC->USDT | 1000 | 1001.1394 | 131330 | n/a | n/a | -0.21 | -0.30 |
| bsc | USDC->USDT | 10000 | 10011.3632 | 295645 | n/a | n/a | -0.33 | -0.43 |
| bsc | USDC->USDT | 100000 | 100112.0419 | 393147 | n/a | n/a | -0.04 | -0.21 |
| bsc | USDC->USDT | 1000000 | 1001070.8344 | 511526 | n/a | n/a | +0.16 | -0.23 |
| bsc | USDT->USDC | 1000 | 998.8367 | 130295 | n/a | n/a | +0.26 | -0.26 |
| bsc | USDT->USDC | 10000 | 9988.3362 | 270885 | n/a | n/a | +0.24 | -0.36 |
| bsc | USDT->USDC | 100000 | 99874.9944 | 465475 | n/a | n/a | -0.56 | -1.20 |
| bsc | USDT->USDC | 1000000 | 998637.4166 | 600966 | n/a | n/a | -0.26 | -0.64 |

Routes:

- bsc USDC->USDT 1000: local Uniswap v4 0.0006% 100.0%
  - CoW: fee 16780871293390828 USDC
  - LI.FI: nordstern
- bsc USDC->USDT 10000: local Uniswap v4 0.0006% 82.3% + Uniswap v4 0.0002% 13.2% + Pancake Infinity CL 0.0001% 4.5%
  - CoW: fee 16780871293390828 USDC
  - LI.FI: nordstern
- bsc USDC->USDT 100000: local PancakeSwap v3 0.01% 82.0% + Uniswap v4 0.0006% 15.2% + Uniswap v4 0.0002% 2.3% + Pancake Infinity CL 0.0001% 0.4%
  - CoW: fee 36382575685086088 USDC
  - LI.FI: nordstern
- bsc USDC->USDT 1000000: local PancakeSwap v3 0.01% 88.6% + Uniswap v4 0.0006% 4.8% + Pancake Infinity CL 0.0001% hooked 4.4% + Uniswap v4 0.0002% 2.3%
  - CoW: fee 38422078869524416 USDC
  - LI.FI: nordstern
- bsc USDT->USDC 1000: local Uniswap v4 0.0002% 100.0%
  - CoW: fee 5936621550185044 USDT
  - LI.FI: nordstern
- bsc USDT->USDC 10000: local Uniswap v4 0.0006% 80.4% + Uniswap v4 0.0002% 15.1% + Uniswap v4 0.0001% 4.5%
  - CoW: fee 17451308881777400 USDT
  - LI.FI: nordstern
- bsc USDT->USDC 100000: local Uniswap v4 0.0006% 70.6% + PancakeSwap v3 0.01% 22.1% + Uniswap v4 0.0001% 3.7% + Uniswap v4 0.0002% 3.6%
  - CoW: fee 48237692301851032 USDT
  - LI.FI: nordstern
- bsc USDT->USDC 1000000: local PancakeSwap v3 0.01% 87.9% + Uniswap v4 0.0006% 7.8% + PancakeSwap v3 0.05% 3.4% + Uniswap v3 0.01% 1.0%
  - CoW: fee 126448693895582688 USDT
  - LI.FI: nordstern
