# Ethereum stablecoin flash arbitrage

The profit sniper uses `eth_flash_arb_pyusd_usdc.py` and the upgraded
`MorphoMatchaStableArbUsdc` executor. For every ordered pair of USDC, USDG, and
PYUSD it atomically runs:

```text
flash-loan token A
  -> A to B on MetaMatcha, then B to A on Stable.com
     OR
  -> A to B on Stable.com, then B to A on MetaMatcha
  -> repay A and keep only the guarded A profit
```

`A/B` means flash-loan A and use B as the counter-token. Thus `PYUSD/USDC`
always borrows and repays PYUSD, and both venue orders are checked. The inverse
`USDC/PYUSD` is a separate USDC-loan route. Funding selection prefers zero-fee
Morpho and can fall back to Aave v3 when the selected token lacks Morpho
liquidity. The older
`eth_flash_arb.py` USDT route remains available as a separate legacy prototype;
it is not part of the 24-variant sniper rotation.

## Files

- `contracts/MorphoMatchaStableArbUsdc.sol` is the current on-chain atomic executor.
- `eth_flash_arb_pyusd_usdc.py` requests both quotes, requests a Stable.com signed order,
  constructs the executor call, performs `eth_call`, estimates gas, and emits an
  unsigned transaction plan.
- `contracts/MorphoMatchaStableArb.sol` and `eth_flash_arb.py` implement the
  legacy USDT-intermediate route.
- `requirements-eth.txt` contains the additional Python dependencies.

## Important limitations

This is a prototype, not production-ready trading infrastructure:

1. Matcha Meta's `/api/competitions` and `/api/quotes` routes are private web-app
   endpoints, not a documented public API. Their schema, aggregator names, and
   Cloudflare policy may change without notice.
2. Stable.com's order endpoints can also return HTTP 403 outside a browser
   session. The script treats any such response as a hard failure; it does not
   attempt to bypass Cloudflare or extract browser credentials.
3. A Stable.com maintainer signature is bound to the on-chain caller. Both
   `addressFrom` and `addressTo` must therefore be the deployed executor—not the
   operator wallet. The script enforces that request layout.
4. Matcha's quoted output must be at least the exact Stable order input. Any
   shortfall reverts atomically. A quote that looks profitable can still become
   stale before inclusion.
5. `ETH_ARB_AMOUNT` is a maximum. If Stable's status response reports a smaller
   USDT input-pool `balance`, the script proportionally reduces the flash loan
   and obtains completely fresh Matcha and Stable quotes. Pool capacity can
   still move between quoting and execution.
6. Gross token profit is not net profit. Ethereum gas, builder/MEV conditions,
   and failed-revert gas must be included. Every run fetches Binance's public
   ETHUSDC price, applies an upward safety buffer, and uses it for the maximum-gas
   profitability check. A failed price request stops the run.
7. The contract and private API integration have not been audited. Test with a
   mainnet fork before using real funds.

## Setup

```bash
python3 -m venv .venv-eth
source .venv-eth/bin/activate
python3 -m pip install -r requirements-eth.txt
```

Compile and deploy `MorphoMatchaStableArb.sol` on Ethereum mainnet with the
operator address passed to its constructor. Verify the deployed bytecode and
owner before requesting orders.

Set configuration in the ignored `.env` file without putting the private key in
a command line:

```dotenv
ETH_RPC_URL=https://YOUR_ETHEREUM_RPC
ETH_OPERATOR_PRIVATE_KEY=0xDEDICATED_OPERATOR_PRIVATE_KEY
ETH_OPERATOR_ADDRESS=0xEXECUTOR_OWNER
ETH_EXECUTOR_ADDRESS=0xDEPLOYED_EXECUTOR
ETH_ARB_AMOUNT=50000
ETH_ARB_LOAN_TOKEN=USDC
ETH_ARB_STABLE_CAPACITY_BUFFER=1
ETH_ARB_SLIPPAGE_BPS=5
ETH_ARB_MIN_PROFIT=1
ETH_ARB_MIN_NET_PROFIT=1
ETH_ARB_ETH_USD=
ETH_ARB_ETH_PRICE_URL=https://data-api.binance.vision/api/v3/ticker/price?symbol=ETHUSDC
ETH_ARB_ETH_PRICE_BUFFER_BPS=100
ETH_ARB_HTTP_TIMEOUT_SECONDS=20
ETH_ARB_QUOTE_ATTEMPTS=3
ETH_ARB_RPC_TIMEOUT_SECONDS=90
ETH_ARB_GAS_LIMIT_MULTIPLIER=1.20
ETH_ARB_MAX_FEE_GWEI=
ETH_ARB_AGGREGATORS=0x,Lightning,1inch,Barter,Bebop,Bitget,KyberSwap,OKX,ParaSwap,Enso
ETH_ARB_MATCHA_BASE_URL=https://meta.matcha.xyz
ETH_ARB_STABLE_BASE_URL=https://api-defi.stable.com
ETH_ARB_OUTPUT_PATH=/tmp/eth-arb-plan.json
```

Estimate the one-time deployment without signing or broadcasting:

```bash
.venv-eth/bin/python deploy_eth_executor.py
```

The legacy deployed executor remains usable for USDC. Borrowing PYUSD requires
deploying the updated contract and replacing `ETH_EXECUTOR_ADDRESS` with the new
address.

The Ethereum scripts load these values from `.env` automatically.

Construct and simulate the route configured in `.env`:

```bash
python3 eth_flash_arb.py
```

Set `ETH_ARB_LOAN_TOKEN=PYUSD` to use the PYUSD -> USDT -> PYUSD route. USDC,
USDT, and PYUSD all use six decimal places on Ethereum. The capacity buffer
avoids requesting the exact pool maximum, which Stable's order-creation backend
rejects even when its status endpoint quotes that amount.

This is dry-run mode. It fetches fresh calldata and a sender-bound Stable order,
runs the complete executor call with `eth_call`, estimates gas, and prints the
unsigned transaction. It never signs or broadcasts.

Broadcast support is deliberately double-gated. Only after reviewing a fresh
successful mainnet-fork simulation should the operator set
`ETH_OPERATOR_PRIVATE_KEY` and invoke both flags:

```bash
python3 eth_flash_arb.py \
  --loan-token PYUSD \
  --amount 50000 \
  --min-profit 1 \
  --min-net-profit 1 \
  --send \
  --confirm-mainnet EXECUTE_ATOMIC_ARB
```

On macOS, the guarded launcher selects either live route and asks for a
route-specific typed confirmation before invoking the same simulation and
profit gates:

```bash
./run_eth_arb_live.command USDC
./run_eth_arb_live.command PYUSD
```

The private key is read only from the environment and is not included in output.
Use a dedicated operator wallet. The transaction is broadcast only when the
atomic simulation succeeds and predicted profit after the configured maximum gas
cost remains above `--min-net-profit`. After broadcast, the engine waits up to
`ETH_ARB_RECEIPT_TIMEOUT_SECONDS` for a receipt and reports success only when its
status is successful. An unconfirmed submission is left explicitly ambiguous so
the sniper stops rather than risking a duplicate transaction.

## Legacy USDT contract safeguards

- Ethereum mainnet, Morpho, USDC/PYUSD loan tokens, the USDT intermediate, and
  Stable's swap contract are fixed.
- Only the owner can start a route.
- Only Morpho can invoke the callback, and only during a pending loan.
- Matcha receives an exact, temporary approval for the selected loan token;
  Stable receives an exact, temporary USDT approval.
- Stable's call is typed and restricted to USDT -> USDC/PYUSD with the executor
  as recipient.
- The loan principal plus `minProfit` must exist before callback completion.
- Morpho's principal is repaid before loan-token profits or USDT dust go to the
  owner.
- Any failed condition reverts both swaps and the loan atomically.
