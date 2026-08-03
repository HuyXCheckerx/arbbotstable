# Polygon and BNB Chain flash arbitrage

This version executes one atomic route on Polygon PoS or BNB Smart Chain:

1. Borrow USDC with a flash loan.
2. Swap USDC to USDT using executable Matcha calldata.
3. Swap USDT back to USDC using a Stable.com signed single-chain order.
4. Repay principal plus any provider premium.
5. Revert the entire transaction unless the configured USDC profit remains.

The implementation is intentionally USDC/USDT-only. Polygon uses native USDC
and USDT with 6 decimals. BSC uses Binance-Peg USDC and USDT with 18 decimals,
so the runner never assumes a universal stablecoin precision.

## Flash-loan providers

- Polygon uses Morpho at `0x1bF0c2541F820E775182832f06c0B7Fc27A25f67`.
  Its flash-loan fee is zero, and the runner caps the requested amount at the
  Morpho contract's live USDC balance.
- BSC uses the Aave V3 Pool at
  `0x6807dc923806fE8Fd134338EABCA509979a7e0cB`. Morpho currently has no usable
  BSC USDC liquidity. The runner reads Aave's `FLASHLOAN_PREMIUM_TOTAL()` every
  run, caps size at the reserve aToken's USDC balance, and subtracts the premium
  before both the on-chain and net-profit gates.

The shared executor is
`contracts/MultichainMatchaStableArb.sol`. Its constructor permanently binds a
deployment to one chain, lender, USDC, USDT, and Stable.com pool. Deploy one
instance on Polygon and a separate instance on BSC.

## Configure

Copy the Polygon and BSC blocks from `.env.example` into `.env` and fill in:

- `POLYGON_RPC_URL`, `POLYGON_OPERATOR_PRIVATE_KEY`, and
  `POLYGON_OPERATOR_ADDRESS`
- `BSC_RPC_URL`, `BSC_OPERATOR_PRIVATE_KEY`, and `BSC_OPERATOR_ADDRESS`

Use dedicated private RPC endpoints and a dedicated operator wallet. The runner
reads all normal settings from `.env`; command-line values are optional
overrides. Never commit `.env`.

The checked-in defaults use PublicNode's free privacy-first shared endpoints.
They are suitable for setup and light use, but they are not dedicated nodes and
provide no private-node SLA. Replace them with authenticated endpoints for a
continuously running production bot.

## Deploy

Estimate Polygon deployment without broadcasting:

```sh
.venv-eth/bin/python deploy_multichain_executor.py --chain polygon
```

Deploy Polygon for real:

```sh
.venv-eth/bin/python deploy_multichain_executor.py \
  --chain polygon \
  --send \
  --confirm-mainnet DEPLOY_POLYGON_EXECUTOR
```

Set the printed address as `POLYGON_EXECUTOR_ADDRESS` in `.env`.

Estimate or deploy BSC by replacing `polygon` with `bsc`; the real deployment
confirmation is `DEPLOY_BSC_EXECUTOR`. Set the result as
`BSC_EXECUTOR_ADDRESS`.

The deployment compiler enables the Solidity optimizer with 200 runs to reduce
deployment size and typical execution gas.

## Dry run and execute

Polygon dry run:

```sh
.venv-eth/bin/python multichain_flash_arb.py --chain polygon
```

BSC dry run:

```sh
.venv-eth/bin/python multichain_flash_arb.py --chain bsc
```

For a live run, double-click the matching macOS launcher or run:

```sh
./run_polygon_arb_live.command
./run_bsc_arb_live.command
```

Before signing, the runner obtains fresh Matcha and Stable.com instructions,
resizes to both flash liquidity and Stable.com capacity, performs an exact
mainnet `eth_call`, estimates gas, prices POL/BNB in USDC, and enforces the net
profit floor. A failed leg reverts the entire atomic transaction, but gas spent
on a reverted transaction is not refunded.

## Operational limitations

Matcha's `meta.matcha.xyz/api/*` endpoints and Stable.com's swap endpoints are
website APIs rather than stable public developer contracts. Either service can
change its payload or block non-browser traffic. In particular, Matcha may
return Cloudflare HTTP 403 from a terminal even while its website works. The
runner stops without signing in that situation; it does not attempt to bypass
the service's access controls.

Quotes and Stable.com orders are short-lived. Always dry-run first and do not
reuse saved calldata. Flash liquidity, Stable.com capacity, Aave premium, gas,
and profitability can all change before the transaction lands.
