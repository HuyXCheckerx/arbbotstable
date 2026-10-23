# THORChain USDC → USDP calculation audit

Checked on 23 October 2026 using public, read-only THORNode queries. No server,
daemon, signing operation, approval, or deposit was started by this audit.

The immediate daemon's decision to wait is supported by the fresh snapshot.
The simulator did contain implementation differences; these have been corrected.
The older log's exact 60.53 USDC fill cannot be independently reconstructed from
those two lines because they did not include a pool block height. New logs include
pool and quote heights.

## Fresh snapshot

All files in `latest/` report block **28,361,962** in the THORNode response header.
Quotes were fetched at **07:33:18 UTC / 14:33:18 Vietnam time**. These are snapshots,
not promises about the pool state when a later deposit arrives.

| Input to the model | Value |
|---|---:|
| Requested deposit | 99,964 USDC |
| Memo target | 99,983.9928 USDP (+2 bps) |
| USDC pool asset depth | 2,317,648.62095630 USDC |
| USDP pool asset depth | 60,773.32888146 USDP |
| Pre-trade pool spot | 1.0041120828 USDP/USDC |
| Active symbiosis minimum fees | USDC→RUNE 2 bps; RUNE→USDP 5 bps |
| Affiliate fee | 1 bp, skimmed at outbound settlement |
| Rapid quote outbound fee estimate | 2.38016826 USDP |
| Refund fee estimate | 0.249734 USDC |
| Wallet gas assumption for comparison | $0.38, matching the supplied log |

With no outside trades between slices:

| Configuration for that same deposit | Slice count | Filled before first miss | Modeled net after fees and $0.38 gas |
|---|---:|---:|---:|
| Rapid, network's automatic quantity | 3,303 | 90.79382382 USDC (3 successful slices) | −$2.84934940 |
| Largest fill found across every allowed rapid quantity | 909 | 109.97139714 USDC (1 successful slice) | −$2.99599853 |
| Resting, one attempt every 20 blocks | 720 | 0 USDC; first slice misses | No deposit should be made; hypothetical refund + gas loss $0.629734 |

The rapid capacity search enumerates quantities 1–3,303, holding the deposit,
memo target, starting pools and outbound-fee estimate fixed. It measures maximum
filled input for this setup, not maximum profit or maximum capacity under every
possible deposit size. Choosing the largest fill actually lowers the net result.
The amount search used by the daemon samples deposit candidates; its log now says
**Best tested deposit** to avoid implying a global optimum.

For the automatic rapid stream, the modeled settlement is 88.57420842 USDP plus
99,872.95644218 USDC refunded. The successful swaps produce only $0.16055286 of
token surplus after the affiliate skim but before the fixed outbound fee. This
does not cover delivery, refund and wallet gas costs.

The resting stream's first slice is approximately 138.84 USDC. At these reserves
it needs a spot near 1.00508011 to pass its first check, assuming only the USDP
pool moves along the price curve used by the threshold solver. It should keep
waiting at this snapshot. Later arbitrage can allow additional fills; adverse
trades can reduce capacity. The model does not forecast either.

## Implementation corrections

Compared with the mainnet v3.20.3 source:

- Apply `MsgSwap.NextSize`, which distributes remainder units among early
  successful slices. The previous implementation used legacy last-slice sizing.
- Match integer share rounding, fee rounding and pool decimal truncation,
  including the USDC pool's precision when its emission is RUNE.
- Remove liquidity-fee RUNE to the Reserve after each leg. Leaving it in pool
  depth changes the next slice's price.
- Apply the advanced queue's affiliate skim to settled output, rather than
  reducing each slice's pool input.
- Resolve default and dynamic minimum fees from constants and Mimir, including
  the dynamic master switch, whitelist state and TOR-anchor override. A quote's
  average liquidity fee is not its per-leg minimum. Failed live fee lookups now
  block deposit modeling instead of authorizing from an inferred average.
- Recalculate resized candidates' maximum streaming quantity using the network
  sizing rule and duration cap.

The proportional outbound-fee check was already correct. Each slice checks its
gross output less `outbound_fee / quantity` against its share of the remaining
memo target. Earlier output reduces that remaining target. Consequently +2 bps
is a memo target, not an independent fixed +2 bps guarantee on every later slice
or on the final wallet result. Dashboard notices now explain this.

## Upstream quote inconsistency, reproduced separately

At the first pinned audit block, **28,361,755**, the same route's automatic quote
reports 100,065.25711566 USDP expected output and a 2.37317259 USDP outbound fee.
Its initial, full-size single-swap simulation instead produces an outbound fee
of 1.37382816 USDP.

`queryQuoteSwap` replaces the displayed fees with the streaming simulation's fees
but retains the original full-size simulation's `outboundFeeAmount` when forming
`expected_amount_out`. Replaying the sampled slice, pool decimal rounding and
affiliate skim reproduces the quoted output **exactly** when that retained fee
is used. Using the displayed streaming fee lowers the result by **0.99934443
USDP**. The daemon uses the displayed outbound fee for its separate capacity
model, so this inflated full-fill headline does not authorize an immediate trade.

The upstream quote also samples a small swap, scales its output by quantity and
restores pool depths during its simulated streaming execution. It therefore
cannot be treated as the amount the current unrebalanced pools can fill.

## Validation and limits

`reference.go` independently ports the relevant integer calculations using Go's
`math/big`. Python and Go agree exactly on four vectors: rapid, maximum-capacity
quantity, first-slice rejection, and a multi-fill case with 31 successful slices.
Frozen public data and these vectors are in
`tests/fixtures/thor_snapshot_28361755.json`.

Replay the saved current snapshot offline with:

```sh
.venv/bin/python3 research/thor-simulation-audit-2026-10-23/replay.py
```

This is a pool replay, not a complete consensus-node emulator. Outbound/refund
fees are fixed at snapshot estimates; real fees and exchange rates can change.
The $0.38 gas assumption is illustrative, not a fresh wallet gas quote; any
required ERC20 approval costs extra. USD results value both stablecoins at $1
and exclude a later sale/redemption. The operational daemon's pool and quote
reads can be from different blocks, now identified in its logs.

## Primary references

- [Streaming swaps](https://dev.thorchain.org/swap-guide/streaming-swaps.html)
- [Fees and minimum-fee selection](https://dev.thorchain.org/concepts/fees.html)
- [Swap calculation and pool updates, v3.20.3](https://gitlab.com/thorchain/thornode/-/blob/v3.20.3/x/thorchain/swap_current.go)
- [Advanced slice sizing](https://gitlab.com/thorchain/thornode/-/blob/v3.20.3/x/thorchain/types/msg_swap.go)
- [Outbound affiliate skim](https://gitlab.com/thorchain/thornode/-/blob/v3.20.3/x/thorchain/manager_txout_current.go)
- [Quote sampling and expected-output calculation](https://gitlab.com/thorchain/thornode/-/blob/v3.20.3/x/thorchain/querier_quotes.go)
- [Quote simulation's pool restoration](https://gitlab.com/thorchain/thornode/-/blob/v3.20.3/x/thorchain/querier.go)

The gateway reports consensus version 3.20.3 and querier version 3.20.2. The
3.20.2 and 3.20.3 swap, quote and dynamic-fee files were compared and are identical
for this audit. Downloaded tagged source and raw responses remain beside this report.
