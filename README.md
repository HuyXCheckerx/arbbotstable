# Stablecoin Arbitrage Bot

Solana stablecoin arbitrage bot with a live operational dashboard. The bot monitors Stable.com pools, evaluates reverse routes on Jupiter, executes eligible trades, and maintains a persistent execution/P&L ledger.

## Dashboard

Run `python3 app.py`, then open `http://SERVER_IP:25284` or the port configured in `.env`.

The dashboard exposes:

- Total and session realized net P&L.
- Successful arbitrages and all attempted executions.
- Full USDC, USDG, PYUSD, and SOL wallet balances.
- Latest observed Stable.com pool balances.
- Current wallet value and SOL/USD estimate.
- Exact observed SOL decrease for each attempt and its execution-time USD estimate.
- Current execution stage, route, uptime, errors, and recent accounting records.

Machine-readable endpoints:

- `GET /api/state` — complete dashboard state.
- `GET /healthz` — returns HTTP 200 when bot state was updated recently, otherwise 503.

### P&L method

For each attempt:

```text
stablecoin change = (USDC + USDG + PYUSD after) - (USDC + USDG + PYUSD before)
SOL cost USD      = observed SOL decrease × average execution-time SOL/USD price
realized net P&L  = stablecoin change - SOL cost USD
```

Stablecoins are estimated at $1. The SOL decrease is measured directly from confirmed wallet lamport balances immediately before and after the complete attempt, so it includes base fees, priority fees, and native SOL charged during that attempt. External SOL transfers made from the same wallet during an attempt would also appear as consumption; use a dedicated bot wallet for clean accounting.

Accounting persists in `bot_state.json`. On the first upgraded run, the old `pnl.txt` value is retained separately as a **prior-method estimate**; it is not mixed into the new realized net P&L because it did not contain exact per-attempt SOL consumption. `pnl.txt` then remains as a backwards-compatible summary of the new method. Both files are intentionally excluded from Git so a server pull does not erase live state.

### Dynamic trade sizing

All arbitrage cycles are anchored in USDC. The scanner evaluates only four strategies:

```text
USDC -> USDG  on Stable.com -> USDC on Jupiter
USDC -> USDG  on Jupiter    -> USDC on Stable.com
USDC -> PYUSD on Stable.com -> USDC on Jupiter
USDC -> PYUSD on Jupiter    -> USDC on Stable.com
```

USDG↔PYUSD cycles and strategies that begin from existing USDG/PYUSD inventory are not considered. Execution measures the intermediate-token balance before and after the entry and exits only that delta, leaving pre-existing token balances untouched.

The scanner does not assume that the largest available trade is the most profitable. For every feasible direction it:

1. Quotes a bounded grid from `MIN_TRADE_SIZE_USD` to the maximum wallet/pool size.
2. Refines around the best coarse result.
3. Subtracts a conservative execution-cost estimate derived from recent observed SOL consumption.
4. Requires at least `MIN_NET_PROFIT_USD` after that estimated cost.
5. Revalidates the selected size twice before submitting the Stable.com leg.
6. Chooses the eligible size with the highest capital efficiency (net return per token committed). Absolute net dollars break ties; lower exposure breaks any remaining tie.

Defaults:

```text
MIN_TRADE_SIZE_USD=1000
MIN_NET_PROFIT_USD=0.10
MIN_NET_RETURN_BPS=0
DEFAULT_EXECUTION_COST_USD=0.005
EXECUTION_COST_SAFETY_MULTIPLIER=1.25
```

With these defaults, every size has the same $0.10 net-profit requirement. When the estimated cost is $0.00625, any candidate must show at least $0.10625 gross difference. Among all candidates that pass, the bot selects the one with the highest net return per token committed. For example, $0.14 net on 10,000 outranks $0.20 net on 20,000 because the 10,000 trade is more capital-efficient.

#### USDG reserve drain mode

For `USDC -> USDG on Stable.com -> USDC on Jupiter`, the scanner no longer chooses a minimum that merely crosses a refill threshold. It only considers near-full-drain candidates, so a profitable partial trade cannot leave the reserve funded and prevent Stable.com from replenishing it.

Sizing remains in raw six-decimal units. With a 5,000 USDG pool and the defaults below, the candidate ladder leaves at most $1 in the pool:

```text
USDG_DRAIN_DUST_RAW=1
USDG_MAX_REMAINDER_USD=1

4,999.000000 USDG -> leaves 1.000000
4,999.900000 USDG -> leaves 0.100000
4,999.990000 USDG -> leaves 0.010000
4,999.999999 USDG -> leaves 0.000001
```

The route is skipped when the wallet cannot drain the pool below the configured remainder. Every candidate must still meet the absolute `$0.10` net-profit floor. Other strategies retain their normal dynamic sizing.

## Local setup

```bash
cd /path/to/arbbot
cp .env.example .env
chmod 600 .env
python3 -m pip install --user -r requirements.txt
python3 app.py
```

Fill in at least `SOLANA_PRIVATE_KEY` in `.env`. Use a dedicated wallet and begin with limited funds.

## Put the project on GitHub for the first time

This directory currently sits inside a parent Git repository, so initialize a repository specifically inside the project before adding files:

```bash
cd /Users/perycent/Downloads/arbbot
git init
git branch -M main
git status --ignored
git add .
git commit -m "Initial arbitrage bot"
```

Confirm that `.env`, `.local`, `logs`, `bot_state.json`, and `pnl.txt` are not in the commit:

```bash
git status --ignored
if git ls-files | grep -Eq '(^|/)(\.env|bot_state\.json|pnl\.txt|logs/)'; then
  echo "STOP: a secret or runtime file is tracked"
  exit 1
fi
```

Create an empty repository on GitHub without adding a README or `.gitignore`. Then connect and push:

```bash
git remote add origin git@github.com:YOUR_GITHUB_USER/arbbot.git
git push -u origin main
```

Alternatively, with the GitHub CLI installed:

```bash
gh repo create arbbot --private --source=. --remote=origin --push
```

A private repository is recommended because this is financial trading infrastructure. The private key must still remain only in `.env`, never in GitHub.

## Install on a server

### Public repository

```bash
git clone https://github.com/YOUR_GITHUB_USER/arbbot.git
cd arbbot
cp .env.example .env
chmod 600 .env
# Edit .env with the server's real secrets.
bash server_start.sh
```

### Private repository

Use a read-only GitHub deploy key:

1. On the server, run `ssh-keygen -t ed25519 -C arbbot-server` and do not add a passphrase for unattended startup.
2. Copy the public key from `~/.ssh/id_ed25519.pub`.
3. In GitHub, open the repository's **Settings → Deploy keys → Add deploy key**.
4. Add the public key and leave **Allow write access** disabled.
5. Test with `ssh -T git@github.com`.
6. Clone with `git clone git@github.com:YOUR_GITHUB_USER/arbbot.git`.

Never copy a personal GitHub access token into the source code or `.env`.

## Normal update workflow

On the development computer:

```bash
git add .
git commit -m "Describe the change"
git push
```

On the server, restart `server_start.sh`. It performs `git pull --ff-only`, installs the declared dependencies, and then starts `app.py`:

```bash
cd /path/to/arbbot
bash server_start.sh
```

For Pterodactyl, set the startup command to:

```bash
bash server_start.sh
```

The container must include `git`, Python, pip, and outbound GitHub access. If Pterodactyl already performs package installation, set `SKIP_GIT_PULL=1` only when you intentionally do not want automatic pulls.

`git pull --ff-only` deliberately refuses to overwrite server-side source edits. Make code changes on the development machine, push them, then pull on the server. Keep server-only secrets and runtime data in the ignored files.

## Operational notes

- The dashboard listens on `0.0.0.0`. Put it behind authentication or a private network before exposing it publicly.
- Logs are written under `logs/` and should be rotated by the host.
- `bot_state.json` is written atomically so the dashboard never reads a partially written update.
- The two exchange legs are separate transactions. Use strict notional limits and supervise the bot until atomic execution or a bounded-loss unwind policy is implemented.
