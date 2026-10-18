Output before network fees. Δ = local − aggregator, in bp of the aggregator's output; positive means the local quote pays more.

| chain | pair | size | local out | local ms | sync ms | Δ ParaSwap | Δ ParaSwap (no RFQ) | Δ CoW | Δ LI.FI | aggregator ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| eth | PYUSD->USDG | 1000 | 999.95 | 88 | 3871 | – | – | -0.24 | -0.18 | 2259 / 1533 |
| eth | PYUSD->USDG | 10000 | 9999.52 | 102 | 216 | – | – | +0.25 | -0.19 | 888 / 601 |
| eth | PYUSD->USDG | 100000 | 99993.54 | 140 | 3002 | – | – | +0.30 | -0.03 | 1753 / 771 |
| eth | USDG->PYUSD | 1000 | 999.96 | 73 | 2499 | – | – | -0.25 | -0.28 | 1412 / 857 |
| eth | USDG->PYUSD | 10000 | 9999.54 | 90 | 209 | – | – | +0.31 | -0.31 | 1474 / 860 |
| eth | USDG->PYUSD | 100000 | 99995.05 | 126 | 2848 | – | – | -0.12 | -0.31 | 1457 / 809 |

Net of gas: local net (legs + 60000 base gas at the chain's gas price) vs each aggregator's output minus its own gas estimate (CoW: its fee-reduced buy amount).

| chain | pair | size | local net | local gas | Δ ParaSwap | Δ ParaSwap (no RFQ) | Δ CoW | Δ LI.FI |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| eth | PYUSD->USDG | 1000 | 999.9153 | 367736 | n/a | n/a | +1.31 | +0.19 |
| eth | PYUSD->USDG | 10000 | 9999.4772 | 367736 | n/a | n/a | +0.40 | -0.15 |
| eth | PYUSD->USDG | 100000 | 99993.4969 | 471585 | n/a | n/a | +0.32 | -0.03 |
| eth | USDG->PYUSD | 1000 | 999.9191 | 380566 | n/a | n/a | +0.36 | +0.13 |
| eth | USDG->PYUSD | 10000 | 9999.4927 | 427960 | n/a | n/a | +0.40 | -0.27 |
| eth | USDG->PYUSD | 100000 | 99994.9842 | 603246 | n/a | n/a | -0.09 | -0.31 |

Routes:

- eth PYUSD->USDG 1000: local [via USDS hop 1] Uniswap v4 0.0005% PYUSD/USDS 100.0% + [via USDS hop 2] Sky LitePSM USDC/USDS 100.0% + [via USDC hop 3] Maverick v2 USDC/USDG 100.0%
  - CoW: fee 193561 PYUSD
  - LI.FI: nordstern
- eth PYUSD->USDG 10000: local [via USDS hop 1] Uniswap v4 0.0005% PYUSD/USDS 100.0% + [via USDS hop 2] Sky LitePSM USDC/USDS 100.0% + [via USDC hop 3] Maverick v2 USDC/USDG 100.0%
  - CoW: fee 193561 PYUSD
  - LI.FI: nordstern
- eth PYUSD->USDG 100000: local [via USDS hop 1] Uniswap v4 0.0005% PYUSD/USDS 100.0% + [via USDS hop 2] Sky LitePSM USDC/USDS 100.0% + [via USDC hop 3] Maverick v2 USDC/USDG 65.7% + [via USDC hop 3] Uniswap v4 dyn hooked USDC/USDG 34.3%
  - CoW: fee 208480 PYUSD
  - LI.FI: nordstern
- eth USDG->PYUSD 1000: local [via USDC hop 1] Uniswap v4 0.0075% USDC/USDG 100.0% + [via USDC hop 2] Sky LitePSM USDC/USDS 100.0% + [via USDS hop 3] Uniswap v4 0.0005% PYUSD/USDS 100.0%
  - CoW: fee 98965 USDG
  - LI.FI: nordstern
- eth USDG->PYUSD 10000: local [via USDC hop 1] Curve USDG/USDC 100.0% + [via USDC hop 2] Sky LitePSM USDC/USDS 100.0% + [via USDS hop 3] Uniswap v4 0.0005% PYUSD/USDS 100.0%
  - CoW: fee 139693 USDG
  - LI.FI: nordstern
- eth USDG->PYUSD 100000: local [via USDC hop 1] Curve USDG/USDC 65.8% + [via USDC hop 1] Uniswap v4 dyn hooked USDC/USDG 22.6% + [via USDC hop 1] Maverick v2 USDC/USDG 11.6% + [via USDC hop 2] Sky LitePSM USDC/USDS 100.0% + [via USDS hop 3] Uniswap v4 0.0005% PYUSD/USDS 100.0%
  - CoW: fee 388512 USDG
  - LI.FI: nordstern
