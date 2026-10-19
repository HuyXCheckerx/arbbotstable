# Paxos USDP sourcing research — 19 October 2026

Research performed during approximately 08:33–08:46 Asia/Ho_Chi_Minh. Quotes and books below are observations, not current executable offers or guaranteed fills. No wallets were connected and no transactions were signed or submitted.

## Conclusion

THORChain is worth monitoring as an independent USDP liquidity source. Neither the supplied screenshot nor the direct 10,000 USDC quote obtained during this research bought USDP below 1 USDC per token. A profitable full cycle remains possible if the subsequent USDG/PYUSD sale exceeds acquisition costs and all remaining fees.

The screenshot is the official THORChain Swap interface, built by Unstoppable Wallet. THORSwap is a separate aggregator interface. Its published standard exchange fee is 0.5% for swaps above $100, subject to documented discounts: [THORSwap fees](https://docs.thorswap.finance/thorswap/thorswap/fees). On 10,000 units, 0.5% is approximately 50 units before other costs. Inspect the actual quote rather than assuming a frontend has no affiliate fee.

## Token identity

The Ethereum Paxos USDP contract is `0x8E870D67F660D95d5be530380D0eC0bd388289E1`, with 18 decimals. Paxos describes native USDP on Ethereum and Solana: [Paxos USDP overview](https://support.paxos.com/articles/3639591263-pax-dollar-usdp), [Paxos reserve report identifying the Ethereum contract](https://paxos.com/wp-content/uploads/2024/03/USDP-Redemption_Assets_Report-fully-executed.pdf).

Match the chain AND contract. Unit Protocol USDP, Parallel USDp, PulseChain fork copies, and exchange-wrapped representations are not interchangeable with native Ethereum Paxos USDP. A matching ticker, or even the same address on a forked chain, does not establish redeemability through Stable.com.

## Quote observations

| Source | Input | Expected output | Effective input/output cost | Qualification |
| --- | ---: | ---: | ---: | --- |
| User screenshot | 10,000 USDC | 9,998.39 USDP | 1.00016103 USDC/USDP | Screenshot also displays an inconsistent 1.00208 USDP/USDC rate |
| Direct THORNode quote through Liquify | 10,000 USDC | 9,996.66634511 USDP | 1.00033348 USDC/USDP | Streaming interval 1, quantity 0; zero affiliate fee; estimated total 1,002 seconds |
| Velora/ParaSwap price API | 10,000 USDC | 9,994.179073001226394987 USDP | Approximately 1.00058243 USDC/USDP | Indicative SELL route at Ethereum block 26,223,401; Uniswap V4 and CurveV1Factory liquidity; observed 08:41–08:42 |

THORNode fee fields, denominated in USDP: liquidity 20.04940672, outbound 2.41125122, affiliate 0, total 22.46065794. These deductions are already reflected in expected output: do not subtract them again. Sender gas/approval and the later conversion/sale legs are additional.

THORNode showed the USDP pool as Available with trading_halted=false. The asset side held approximately 61,074.71083161 USDP, versus approximately 2,330,419.57595655 USDC in the Ethereum USDC pool. A 10,000-unit trade is substantial relative to the USDP side. Streaming reduces price impact but introduces time between acquisition and exit. The quote selected a maximum streaming quantity of 164 and reported 978 seconds of streaming, 1,002 seconds total. These settings are not the settings in the user's screenshot.

Primary endpoints used:

- [THORNode pools](https://gateway.liquify.com/chain/thorchain_api/thorchain/pools)
- [THORNode 10,000 USDC to USDP streaming quote](https://gateway.liquify.com/chain/thorchain_api/thorchain/quote/swap?from_asset=ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48&to_asset=ETH.USDP-0X8E870D67F660D95D5BE530380D0EC0BD388289E1&amount=1000000000000&streaming_interval=1&streaming_quantity=0)
- [Velora 10,000 USDC to USDP SELL price](https://api.paraswap.io/prices?srcToken=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48&destToken=0x8E870D67F660D95d5be530380D0eC0bd388289E1&srcDecimals=6&destDecimals=18&amount=10000000000&side=SELL&network=1&version=6.2)
- [THORChain integration guide](https://dev.thorchain.org/swap-guide/quickstart-guide.html) — THORNode amounts use eight decimal places, regardless of native token decimals.

Links to quote endpoints return NEW observations when opened; they do not preserve these historical responses. No signed or taker-specific executable quote was obtained.

## On-chain services and liquidity

| Service | Evidence and role | Priority for this strategy |
| --- | --- | --- |
| [Official THORChain Swap](https://swap.thorchain.org/) | Direct native USDP pool verified through THORNode | Monitor; compare net output, fee, minimum received, and completion time |
| [THORSwap](https://app.thorswap.finance/) | Alternative aggregator interface; published standard 0.5% service fee | Compare only after actual affiliate/service fee is known |
| [Velora](https://app.velora.xyz/) | Public price API returned a USDC-to-USDP route | Strong candidate for same-chain acquisition and automation |
| [Uniswap](https://app.uniswap.org/explore/tokens/ethereum/0x8e870d67f660d95d5be530380d0ec0bd388289e1) | Uniswap V4 appeared in the live Velora route | Monitor direct USDP/USDC and other verified pools |
| [Curve](https://www.curve.finance/dex/ethereum/swap) | CurveV1Factory appeared in the live Velora route; [official code also identifies the legacy Paxos pool](https://github.com/curvefi/curve-js/blob/master/src/constants/pools/ethereum.ts) | Monitor canonical-token pools; old pool names can refer to different USDP tokens |
| [Rujira](https://rujira.network/articles/how-to-deposit-your-assets-onto-rujira) | Official supported-asset documentation lists USDP | Candidate; live USDP order book and full withdrawal economics were not verified |
| [U.CASH](https://u.cash/swap/usdp) | Advertises non-custodial Ethereum Paxos USDP routing | Listed support only; executable route and price not tested |

Other aggregators such as LlamaSwap, 1inch, 0x/Matcha, KyberSwap, Odos, and CoW Swap can be comparison channels if they return a route for the canonical token. They often share underlying liquidity and should not be counted as independent cheap USDP supply. KyberSwap's direct API returned HTTP 403 in this environment; this does not establish asset unavailability. Individual live USDP quotes from the other named aggregators were not obtained in this research.

## Centralized order books checked

These are cheapest asks, not last traded prices. Quote currencies differ. USDT and USD must be converted to the strategy's USDC basis before comparison. Account eligibility, exact user trading fees, and USDP withdrawal availability/fees were not authenticated or verified.

| Venue/pair | Cheapest ask observed | Finding |
| --- | ---: | --- |
| [MEXC USDP/USDT](https://api.mexc.com/api/v3/depth?symbol=USDPUSDT&limit=100) | 1.0035 USDT | Above parity; visible asks implied about 9,951.52 USDP for 10,000 USDT before fees |
| [Gate USDP/USDT](https://api.gateio.ws/api/v4/spot/order_book?currency_pair=USDP_USDT&limit=100) | 1.0021 USDT | Above parity; approximately 2,902.81 USDP across the first two levels at 1.0021/1.0022, then a material gap |
| [HTX USDP/USDT](https://api.huobi.pro/market/depth?symbol=usdpusdt&type=step0) | 1.0008 USDT | Above parity; only approximately 60.93 USDP at the best ask; thin near-parity asks |
| [Coinbase PAX/USD](https://api.exchange.coinbase.com/products/PAX-USD/book?level=2) | 1.0064 USD | Above parity; public product metadata reports online, limit_only=true, trading_disabled=false |
| [Binance USDP/USDT metadata](https://api.binance.com/api/v3/exchangeInfo?symbol=USDPUSDT) | None | Status BREAK; separate order book request returned empty bids/asks |
| [KuCoin USDP-USDT metadata](https://api.kucoin.com/api/v2/symbols/USDP-USDT) | None | API returned code 900001: Trading pair USDP-USDT does not exist |
| [Kraken USDP/USD pair query](https://api.kraken.com/0/public/AssetPairs?pair=USDPUSD) | None | Unknown asset pair, despite a generic Kraken buy-USDP marketing page; no executable market verified |

Discovery directories also surfaced CoinUp.io and Mercado Bitcoin. These were not independently verified as viable sourcing routes. Older directory listings for Binance/KuCoin contradicted the live APIs above. Cheap last trades are particularly misleading when current asks are higher or thin.

Limit bids on an eligible exchange may acquire discounted USDP when sellers arrive, but that is a waiting strategy with uncertain fills. It requires a working native USDP withdrawal route to Stable.com and enough discount to cover trading, withdrawal, and capital costs.

## Instant-swap services found

The following first-party pages explicitly identify Pax Dollar/USDP. This verifies advertised support, NOT a live 10,000 USDC-to-USDP quote, buy-direction availability, custody model, reliability, or profitable pricing. A generated pair page can persist while its provider is unavailable.

| Service | First-party evidence |
| --- | --- |
| ChangeNOW | [USDP currency page, including Ethereum contract](https://changenow.io/currencies/usdp) |
| Changelly | [USDP pairs](https://changelly.com/exchange-pairs/usdp), [BTC to USDP acquisition page](https://changelly.com/exchange/btc/usdp) |
| SimpleSwap | [USDP/USDC page](https://simpleswap.io/id/crypto-to-crypto/usdp-usdc); advertised USDC-to-USDP links also appeared in its pair pages |
| StealthEX | [Pax Dollar coin page](https://stealthex.io/coin/pax-dollar/), [explicit Pax Dollar pair page](https://stealthex.io/exchange-pairs/pax-dollar-to-solana-binance-smart-chain/) |
| ChangeHero | [Pax Dollar pair page](https://changehero.io/es/swap/usdp/btc) |
| Godex | [Pax Dollar/USDT page](https://godex.io/exchange/usdp-to-usdt) |
| EasyBit | [Supported live-price list includes USDP Pax Dollar](https://easybit.com/en/live-prices) |
| Swapzone | [Pax Dollar provider comparison page](https://swapzone.io/exchange/pax-dollar) |
| SwapSpace | [Explicit Pax Dollar comparison page](https://swapspace.co/exchange/usdp) |

Use Swapzone/SwapSpace to discover provider offers, then compare exact Ethereum USDP received for the same input amount. These can show overlapping providers. Service spreads and delivery charges can absorb a small arbitrage edge, so advertised support alone does not make a service suitable. Check whether a quoted output already includes delivery/network fees and whether the offer is fixed or floating. Account/AML reviews and settlement delays can also matter for a small, time-sensitive spread.

LetsExchange was searched, but a guessed USDP pair URL redirected to the homepage and no direct USDP result was verified; excluded from the confirmed list. Exolix access was blocked by robots rules; support unverified. No exhaustive claim about every service on the internet is possible.

## Profit threshold

Assume the user's stated Stable.com conversion of USDP to USDG or PYUSD is exactly 1:1 in token units at the intended size. Stable.com liquidity, size limits, execution fees, and the final sell quote must still be checked for each attempt.

Let `Q` be USDP actually received, `r` the USDC returned per USDG/PYUSD after the final sale's embedded fees, and `C` the remaining costs expressed in USDC. Then:

`net_profit = Q * r - initial_USDC - C`

`break_even_r = (initial_USDC + C) / Q`

For the screenshot's Q=9,998.39:

| Final USDG/PYUSD selling rate | Gross USDC surplus before remaining costs |
| --- | ---: |
| 1.0000 USDC | -1.61 |
| 1.0005 USDC | +3.39 |
| 1.0010 USDC | +8.39 |
| 1.0020 USDC | +18.39 |

With 10 USDC of remaining costs, break-even is approximately 1.00116119 USDC per exit token. Use minimum received values rather than optimistic expected outputs when defining an execution threshold. Do not subtract any fee twice.

A THORChain swap settles through THORChain and then sends the native token out. Even ETH-USDC to ETH-USDP is not an atomic single-Ethereum-transaction leg, so it cannot simply be inserted into a conventional Ethereum flash-loan cycle. It requires capital or inventory and exposes the later legs to changing quotes. [THORChain swap flow](https://dev.thorchain.org/swap-guide/quickstart-guide.html), [streaming swap mechanics](https://docs.thorchain.org/technical-documentation/thorchain-finance/continuous-liquidity-pools).

Practical monitoring order: canonical Ethereum USDP routes on Velora/Uniswap/Curve and other functioning aggregators; direct THORChain with explicit fees and settlement time; eligible exchange limit books with verified withdrawals; instant services only when their complete delivered quote beats these alternatives. Rank by final USDC returned after the whole cycle, not by token dollar-price labels.
