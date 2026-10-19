# THORChain fees, zero-bps routes, and THORSwap documentation notes

Checked 19 October 2026, Asia/Ho_Chi_Minh. Public read-only queries only; no wallets connected, approvals signed, or transactions submitted.

## Main findings

1. Ordinary native Ethereum USDC → native Ethereum Paxos USDP has a real liquidity-fee floor. Live quotes returned approximately 20 bps of liquidity fees with zero affiliate fees. Changing the interface's Price Protection slider does not change that protocol fee.
2. Zero affiliate/service basis points are available by interacting directly with THORNode without an affiliate fee. This does not make liquidity, outbound delivery, or sender gas free.
3. A lower-fee path was verified at the quote level: an enrolled affiliate THORName with a non-zero fee selects its dynamic liquidity-fee floor. A 10,000 USDC quote carrying `affiliate=symbiosis&affiliate_bps=1` returned 9.43710466 USDP total quoted fees, compared with 22.49457818 USDP for the direct zero-affiliate quote. Its estimated settlement was much longer. This is an API-level configuration, not a claim that Symbiosis's retail interface offers a 1 bp service fee.
4. A special arbitrage contract has a current zero-bps minimum setting, but that is a minimum-floor exemption for a particular contract, not a public zero-cost native USDP swap feature. Live state also reported WASM globally halted.
5. THORSwap's documented 100% discount removes THORSwap's exchange fee, not THORChain liquidity or chain costs. The published image puts the full waiver at at least 1.5 million THOR staked.
6. No currently usable ordinary native-USDC-to-native-USDP route with zero total swap costs was verified.

## Documentation coverage

Fetched all 102 Markdown pages linked by the [official THORSwap documentation index](https://docs.thorswap.finance/thorswap/llms.txt): 102 successful, zero failed; 337,779 bytes of Markdown. All pages were searched for fee, zero-fee, discount, and related claims. Fee, discount, referral, routing, streaming, staking, and deprecated-feature pages were read in greater detail. The fee-tier chart embedded in the documentation was fetched and visually inspected.

The local source cache is `/var/folders/71/v02nr5md7qq94td0wgdhymx80000gn/T/thorswap-docs-20261019-l5c1jx0x/`. It includes the index, Markdown sources, discount chart, and fetch manifest. This is temporary storage. Embedded videos and every image were not downloaded or transcribed; the 102/102 count refers to the indexed Markdown pages.

The companion [evidence file](./thorchain-fee-evidence-2026-10-19.json) preserves all source-page URLs, local paths, SHA-256 digests, query timestamps, exact quote requests/responses, live network configuration, and dynamic fee records. That file is a point-in-time record; live URLs will return new state.

## Why the 0.2% appears

For this native-token pair, THORChain routes through USDC/RUNE and RUNE/USDP. The live `L1SLIPMINBPS` override was 10. Ordinary well-streamed double-swap quotes returned approximately 20 bps in aggregate, consistent with the two internal legs. A minimum is a floor: insufficient splitting or shallow liquidity can produce a higher actual fee.

Do not confuse three numbers:

- Price Protection 0.2%: execution tolerance used when constructing a price limit.
- Liquidity/slip fee around 0.2%: value charged by THORChain's pool mechanism in the tested ordinary route.
- THORSwap exchange fee: a separate frontend charge, documented as 0.5% before discounts.

The definitive check is the quote's `fees.affiliate`, `fees.liquidity`, `fees.outbound`, and `fees.total`. THORNode's amounts use 1e8 units, even though Ethereum USDC has six decimals and USDP has eighteen. Divide those quote fee fields by 100,000,000 to obtain output-token units.

Primary references: [THORChain fees](https://dev.thorchain.org/concepts/fees.html), [live Mimir](https://gateway.liquify.com/chain/thorchain_api/thorchain/mimir), [THORSwap exchange fees](https://docs.thorswap.finance/thorswap/thorswap/fees).

## Tested ways to reduce fees

These observations used native Ethereum USDC → native Ethereum USDP, input 10,000 USDC. They were fetched sequentially at 09:27:43–09:29:27 local time. Pool state can change between requests, so output differences are not a controlled measurement of affiliate savings. Fee fields and timing show the observed tradeoffs. No execution or wallet-specific quote validation was performed.

| Configuration | Liquidity fee, USDP | Affiliate fee, USDP | Outbound fee, USDP | Total quoted fees, USDP | Expected USDP output | Estimated completion |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Direct, no affiliate, automatic rapid streaming | 20.08589508 | 0 | 2.40868310 | 22.49457818 | 10,004.15145481 | 354 sec / 5m 54s |
| `ss`, 1 bp affiliate, automatic rapid streaming | 17.05612188 | 1.00162490 | 2.41206307 | 20.46980985 | 10,013.38088083 | 1,122 sec / 18m 42s |
| `ej`, 1 bp affiliate, automatic rapid streaming | 11.03831190 | 1.00243201 | 2.41302716 | 14.45377107 | 10,021.45117346 | 3,318 sec / 55m 18s |
| `symbiosis`, 1 bp affiliate, automatic rapid streaming | 6.02114415 | 1.00293359 | 2.41302692 | 9.43710466 | 10,026.46640848 | 3,318 sec / 55m 18s |
| `symbiosis`, 0 bp affiliate, automatic rapid streaming | 20.08318416 | 0 | 2.40868353 | 22.49186769 | 10,003.25283464 | 354 sec / 5m 54s |
| `symbiosis`, 1 bp affiliate, explicitly 164 sub-swaps | 15.07563440 | 1.00101169 | 2.40868112 | 18.48532721 | 10,007.24937527 | 354 sec / 5m 54s |

Sender Ethereum gas, any required approval gas, the subsequent Stable.com conversion, and final USDG/PYUSD sale costs are not in this total. These fee deductions are already reflected in the expected USDP output: do not subtract them twice.

The useful counterexample is the fifth row: retaining the affiliate name while setting its fee to zero lost the dynamic discount and returned to roughly 20 bps. Paying a small affiliate fee can therefore reduce the aggregate cost. The last row illustrates a time-budget compromise: a fixed count reduced the quoted fee somewhat while matching the direct route's estimated duration in that observation. It did not reach the cheapest automatic route's fee.

### Reproducing the public quote

Base endpoint:

`GET https://gateway.liquify.com/chain/thorchain_api/thorchain/quote/swap`

Parameters for the cheaper observed quote:

```text
from_asset=ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48
to_asset=ETH.USDP-0X8E870D67F660D95D5BE530380D0EC0BD388289E1
amount=1000000000000
streaming_interval=0
streaming_quantity=0
affiliate=symbiosis
affiliate_bps=1
```

This is a read-only quote request, not a transaction or ready-to-execute memo. For the zero-affiliate comparison omit affiliate fields, or use zero affiliate bps. For a shorter custom stream request a fixed streaming_quantity and compare actual output, fees, and duration.

Any eventual transaction must use a refreshed executable quote, correct destination, real price limit, correct token approvals and chain, and the SAME affiliate and streaming configuration that was quoted. Do not cache vault addresses or reuse expired quote data. Omitting the affiliate during execution after quoting a discounted affiliate configuration changes the expected fee calculation. Reference: [THORChain integration guide](https://dev.thorchain.org/swap-guide/quickstart-guide.html).

### Dynamic floor caveats

Live Mimir reported `L1DYNAMICFEEENABLED=1` and active enrollment for `ss`, `ej`, and `symbiosis`. The mechanism requires an eligible registered THORName carrying non-zero affiliate bps; simply registering your own THORName does not enroll you. Raw addresses and ineligible/zero-fee entries do not receive this treatment. Governance controls enrollment, and floor values can move over time.

The live fee-record endpoint displayed USDC/RUNE floors of 14 bps for `ss`, 10 for `ej`, and 5 for `symbiosis`; `ss` also had a USDP/RUNE record at 3 bps. Some endpoint records were per RUNE leg even though the newer ADR describes endpoint-pair identities. Use the returned quote to assess your actual two-leg route rather than predicting its aggregate fee from one record or assuming a universal 1 bp floor.

Primary references: [dynamic fee ADR](https://dev.thorchain.org/architecture/adr-026-dynamic-l1-min-fee-per-thorname.html), [dynamic fee state](https://gateway.liquify.com/chain/thorchain_api/thorchain/dynamic_l1_fees), [fee selection rules](https://dev.thorchain.org/concepts/fees.html#dynamic-l1-minimum-fee). The implementation's fee selector was also inspected in [THORNode helpers.go](https://gitlab.com/thorchain/thornode/-/blob/develop/x/thorchain/helpers.go). Develop-branch code is supporting context; live mainnet quotes are the empirical evidence.

## Which zero-bps claims are real?

| Claim/path | What can be zero | What remains / restriction |
| --- | --- | --- |
| Direct THORNode interaction | User-side affiliate fee | Normal protocol liquidity fee, outbound fee, sender gas |
| THORSwap full staking discount | THORSwap exchange/service fee | Underlying liquidity/network costs and THOR capital exposure |
| THORSwap trades below $100 | THORSwap service fee under its published rules | Protocol and network costs; repeated tiny trades can multiply delivery costs |
| WASM arbitrage contract | Its configured minimum slip floor, currently 0 bps | Specific-contract origin check, natural pool slip, other execution costs, and current global WASM halt |
| Trade-asset deposits/withdrawals | Conversion slip for minting/redeeming the SAME asset | USDC→USDP trading still has a fee; native withdrawal/delivery is needed for Stable.com |
| Stable-to-stable anchor treatment | A reduced fee floor, not zero in current Mimir | `STABLESLIPMINBPS=2`; USDP was absent from the live TOR anchor set |

The standard USDP path cannot remove its liquidity fee by setting `affiliate_bps=0`, setting price protection to zero, splitting into arbitrarily small swaps, or renaming a token as a trade/secured asset.

### The special zero-floor contract

Live configuration reported `WASMARBSLIPMINBPS=0`. Source code applies this only when the sender matches the configured `WasmArbContract`, not whenever an arbitrary caller supplies a parameter. The configured address was `thor1n5a08r0zvmqca39ka2tgwlkjy9ugalutk7fjpzptfppqcccnat2ska5t4g`.

Crucially, live Mimir also reported `HALTWASMGLOBAL=1`. THORChain's Mimir documentation defines that flag as halting all CosmWasm contract execution. Thus this is not a currently verified usable route. Even if it were enabled, zero minimum does not force the actual slip-based fee to zero and does not make it a native USDP withdrawal service. Contract entry points, permissions, and end-to-end USDP acquisition were not verified.

The Stable Reserve also has zero-fee fill concepts in current protocol documentation, but effective `StableReserveEnabled` was 0 (constant 0, no override in the captured Mimir), and native USDP was not an eligible live TOR anchor. No usable USDP zero-fee route was established there either.

References: [Mimir flags](https://dev.thorchain.org/mimir.html), [fee selector](https://gitlab.com/thorchain/thornode/-/blob/develop/x/thorchain/helpers.go), captured live state in the evidence file.

## THORSwap notes that matter for this strategy

### Fee discounts

The published fee-tier chart was visually read from the image embedded in the [fee-discount page](https://docs.thorswap.finance/thorswap/thor/about/fee-discounts):

| THOR staked/equivalent holding, per published chart | THORSwap exchange fee |
| --- | ---: |
| Less than 3,000 | 0.50% |
| At least 3,000 | 0.42% |
| At least 30,000 | 0.25% |
| At least 300,000 | 0.17% |
| At least 1,500,000 | 0% |

The chart states vTHOR discounts are calculated dynamically and uTHOR counts 1:1 with THOR for discounts. A holding wallet must be connected. The chart is documentation, not a live eligibility test or an executable fee offer. Confirm the actual exchange-fee field before transacting.

The core fee page also documents free service fees below $100 and a 50% service-fee discount on trades at least $1 million. None eliminates protocol fees. Buying/staking THOR solely to remove an interface fee adds token-price exposure and is unnecessary to obtain a zero-affiliate direct quote.

[vTHOR](https://docs.thorswap.finance/thorswap/thor/about/vthor) represents staked THOR with compounding THOR rewards. [uTHOR](https://docs.thorswap.finance/thorswap/thor/about/uthor) represents staked THOR with claimable USDC rewards. USDC rewards do not turn the principal into USDC. The uTHOR page describes yTHOR as locked with no withdrawal of staked THOR; do not treat staking variants as economically identical.

### Referrals and RUNE Edition

[Referral documentation](https://docs.thorswap.finance/thorswap/referral-program) describes 10–20 bps referral commissions depending on holdings and says a portion can be configured as a rebate through their team. This is not a promise to waive all trading costs, and it does not establish that self-referrals are allowed or profitable.

[RUNE Edition](https://docs.thorswap.finance/thorswap/ecosystem/thorchain/thorswap-rune-edition) advertises a 50% discount on RUNE and TCY purchases. That is not a general native-USDP discount.

### Streaming and routing

[Streaming](https://docs.thorswap.finance/thorswap/thorswap/streaming-swaps) uses one inbound transaction and one final outbound transaction, avoiding the repeated L1 delivery fees of manually sending many swaps. More splitting can reduce natural price impact but cannot bypass the selected minimum fee. Auto-count with rapid spacing requests the network's optimization; it may select many more sub-swaps when the fee floor is lower, extending duration.

[Direct Swaps](https://docs.thorswap.finance/thorswap/thorswap/direct-swaps) describes a wallet-connection-free deposit-address interface using providers such as Chainflip, NEAR Intents, and Flashnet. Its name does not mean direct zero-affiliate THORNode access. Likewise, a THORSwap quote can use another liquidity provider; inspect the provider and route, not just the website branding.

The settings for price protection, fee amounts, minimum output, and ETA must be assessed together. A cheaper acquisition is not a locked USDC profit while the Stable.com conversion or final USDG/PYUSD sale remains unexecuted.

### Trade accounts

[Trade accounts](https://docs.thorchain.org/technical-documentation/thorchain-finance/trade-assets) can amortize native-chain delivery costs for repeated internal trading. Depositing and withdrawing the same underlying asset does not incur swap slip; trading different assets does. Live `TRADEACCOUNTSSLIPMINBPS` was 10, so this did not establish a USDC→USDP liquidity-fee escape. Your Stable.com leg requires usable native USDP rather than merely a THORChain account credit. Trade accounts also introduce their own custody/economic-security considerations described in those docs.

### Stale and contradictory material

Do not assume the fetched documentation is internally current just because it is still published:

- The [older streaming FAQ](https://docs.thorswap.finance/thorswap/how-tos/frequently-asked-questions/streaming-swaps) says there is no price protection, while the [customer-support version](https://docs.thorswap.finance/thorswap/customer-support/support-faqs/streaming-swaps) says sub-swaps failing the price limit are canceled/refunded. Current THORChain developer docs support price limits.
- Some old FAQs describe a fixed 3× outbound gas premium. Current protocol documentation describes a dynamic outbound fee. Use the actual quote's outbound amount.
- One streaming FAQ describes up to one year for native assets; captured current constants used 14,400-block streaming limits, illustrating why runtime settings matter.
- [Savers, synth, and lending material is archived](https://docs.thorswap.finance/thorswap/ecosystem/thorchain/archived). The support page says savers/loans were discontinued in January 2025. Old lending zero-interest claims are not zero-fee swap routes.

## Practical approach

For this bot, compare at least: a direct zero-affiliate rapid quote; eligible dynamic-affiliate quotes carrying their real non-zero affiliate fee; and fixed-count variants under an acceptable duration budget. Rank the FULL cycle by minimum native USDP received, expected final USDC after Stable.com and the exit sale, sender gas, and time at risk.

The actionable finding is a tested reduction below 20 bps through dynamic-affiliate quoting. The unresolved limitation is a verified ordinary native-USDP swap with zero protocol costs: none was found, and the documented zero-service-fee features do not provide that result.
