# THORChain USDP swap audit — 22 October 2026

Wallet: `0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C`. Checked: 2026-10-22T15:01:59.799318+00:00.

Coverage: all 15 Midgard actions returned for this address, plus an empty page at offset 15. Seven USDC→USDP deposits after deduplicating paired swap/refund actions. Excludes the separate USDC→RUNE purchase and THORName registration. All seven have completed THORNode outbound signing; no active stream was found for this destination. Other wallets are outside this audit.

Largest delivery: **1,655.83953350 USDP**, transaction `0x7617a9828485b38b438d55e971903a8c8cc06a46f9fb698f47ddb9201c6597ba`. Highest input fill percentage: **13.88888889%**, transaction `0x2796557a1e535f469b725942ec17e6d7538738e29b1655a3b74aadbbf2b7f9bd`.

All P&L below values 1 USDP = 1 USDC. It covers acquisition only, before sender/approval gas, later Stable.com conversion and exit sales, capital cost, and any affiliate receipts outside the Ethereum wallet. Negative acquisition surplus does not prove the eventual complete arbitrage lost money.

| THORChain start (Vietnam) | Deposit USDC | Swapped USDC | Delivered USDP | Refunded USDC | Fill % | Parity surplus before sender gas | Interval / requested quantity | Successful / attempted | Swap hours |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| [2026-10-21 07:26](https://etherscan.io/tx/0x7617a9828485b38b438d55e971903a8c8cc06a46f9fb698f47ddb9201c6597ba) | 100,000 | 1,657.35567977 | 1,655.83953350 | 98,342.394454 | 1.6574 | -1.76601250 | 1 / 5375 | 89 / 5370 | 9.209 |
| [2026-10-20 00:21](https://etherscan.io/tx/0x2796557a1e535f469b725942ec17e6d7538738e29b1655a3b74aadbbf2b7f9bd) | 10,000 | 1,388.88888900 | 1,388.28553626 | 8,610.861447 | 13.8889 | -0.85301674 | 20 / 720 | 100 / 720 | 24.577 |
| [2026-10-21 17:07](https://etherscan.io/tx/0x6d30d0dd750cd9cfebbf175e562dc36ee697eb701151e65e1dc686e83ea1ce71) | 50,000 | 540.64131252 | 538.54233531 | 49,459.108790 | 1.0813 | -2.34887469 | 0 / 2682 | 29 / 2682 | 4.628 |
| [2026-10-19 21:06](https://etherscan.io/tx/0x3a28744fee417c97b4064fc4d38fa38bf58af0ca8498b56b718c440f3cc2d203) | 100,000 | 423.21644505 | 420.96431161 | 99,576.533672 | 0.4232 | -2.50201639 | 0 / 1655 | 7 / 1654 | 2.826 |
| [2026-10-21 22:22](https://etherscan.io/tx/0x628c192ab6e734b035412295c2f6e7fb6faddf247650d50d9cf0738751c67c24) | 100,000 | 333.33333336 | 330.92987040 | 99,666.416977 | 0.3333 | -2.65315260 | 1 / 2400 | 8 / 2400 | 4.131 |
| [2026-10-19 12:11](https://etherscan.io/tx/0xce98133010002812f44dd2e9239b5963c2e4e94c7d36a8590293e0dda145816e) | 10,000 | 67.19609050 | 64.86802666 | 9,932.554280 | 0.6720 | -2.57769334 | 0 / 1637 | 11 / 1637 | 2.801 |
| [2026-10-19 20:35](https://etherscan.io/tx/0x3dfaf115a61e469fccb987a7977d32326e32edc5683f26b16775c6da826f0610) | 10,000 | 0.00000000 | 0.00000000 | 9,999.750238 | 0.0000 | -0.24976200 | 0 / 164 | 0 / 0 | 0.000 |

Times run from THORChain consensus to swap finalisation; Ethereum deposit and outbound delivery add latency. Requested quantity can be normalised: the 5,375 request executed a 5,370-attempt stream; the 1,655 request became 1,654. Midgard’s swap action can begin at the first successful fill rather than deposit consensus, so block times were used for duration.

## Totals

- input_usdc: 380000
- swapped_usdc: 4410.63175020
- usdp_received: 4399.42961374
- usdc_refunded: 375587.619858
- surplus_at_parity_before_sender_gas: -12.95052826
- gross_surplus_before_outbound_refund_gas: 3.81552201
- usdp_outbound_deduction: 15.01765847
- usdc_refund_deduction: 1.74839180

The 380,000 USDC figure sums deposits across orders, including recycled capital; it is not unique capital invested. Only 4,410.63175020 USDC swapped (1.1607% of cumulative deposited volume). Gross swap surplus was 3.81552201; USDP delivery deductions totalled 15.01765847 and USDC refund deductions 1.74839180. Therefore 3.81552201 − 15.01765847 − 1.74839180 = **−12.95052826** before sender gas. Embedded liquidity/affiliate deductions are already represented in gross emitted USDP and are not charged again in this calculation.

## Interpretation and refinement

1. Optimise expected full-cycle profit and capital use, not deposit size or quoted full delivery. The largest deposit acquired only 1.66% of its input. The 10,000 order delivered 84% as much USDP as the best 100,000 order while reserving one tenth the initial capital, but took roughly 24.58 hours versus 9.21 hours. These are observations under different markets, not a controlled interval comparison.

2. Jointly evaluate deposit size, slice size, and interval. The best absolute fill used interval 1, requested quantity 5,375, about 18.60 USDC per requested slice, and 89 successful attempts. The best percentage fill used interval 20, quantity 720, about 13.89 USDC per slice, and 100 successes. The latest 100,000 deposit used interval 1 and 2,400 slices (~41.67 USDC), cleared only eight, and received 330.93 USDP. Compare intervals 1/5/10/20 and deposits such as 2k/5k/10k using recorded pool states; do not treat any grid point as an established optimum.

3. Require enough expected successful volume to cover fixed delivery/refund fees and all sender and exit costs. On the winning order, 1,657.35567977 USDC filled and generated 1.08684027 surplus before delivery/refund deductions; those deductions were 2.85285277, leaving −1.76601250. At its observed 6.5577 bps gross edge, roughly 4,350 USDC would have needed to fill just to cover those deductions before gas. That is a local break-even illustration, not a reliable future fill forecast. The +2 bps memo protects portions of the stream, not full-cycle P&L.

4. Use executable exit quotes. Evaluate P = Q*c*r + R − D − C, where Q is delivered USDP, c the realised conversion rate, r the executable USDC sale rate, R the refund, D the deposit, and C the remaining costs. Do not lower the floor simply to make more USDP fill. The latest order already used a parity floor and filled only 0.3333%.

5. Validate the affiliate on every attempt and choose by all-in quote. One order was completely refunded because the affiliate address was empty with nonzero basis points. The current Mimir snapshot marks symbiosis active for dynamic fees, but its floor is variable. Keep quote-based tier discovery and account for partial fills; do not disable the capacity check based on a large headline quote.

6. Maintain two models: a conservative immediate-fill model with no external rebalancing, and a historical pool/event replay for a resting stream. The existing capacity planner is the former; the resting path should also budget expected successful fills, total costs, and capital lock time. Log predicted versus settled fills, and rank candidates by net profit with a capital-time constraint. Seven orders are insufficient to establish a universal optimal schedule.

## Sources and verification

- [Midgard address actions](https://gateway.liquify.com/chain/thorchain_midgard/v2/actions?address=0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C&limit=50&offset=0)
- [THORChain streaming mechanics](https://dev.thorchain.org/swap-guide/streaming-swaps.html)
- [THORChain fees and dynamic affiliate tiers](https://dev.thorchain.org/concepts/fees.html)
- Raw address actions, per-transaction THORNode status/details, block times, current Mimir, and active-stream snapshot are saved beside this report.
- Ethereum receipt reads through two public JSON-RPC endpoints returned HTTP 403; direct ERC20 receipt logs were not independently verified. Settlement amounts agree between Midgard and THORNode. All calculations use Decimal with THORChain 1e8 scaling.
- No transactions sent, live settings changed, or strategy source files edited.
