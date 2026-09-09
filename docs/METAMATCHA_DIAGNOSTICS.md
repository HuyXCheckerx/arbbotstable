# MetaMatcha access diagnosis — 2026-09-08

The PC's HTTP 429 responses are Vercel Security Checkpoint challenges. They
are being served instead of MetaMatcha competition JSON. The response alone
does not reveal which firewall rule or detection signal triggered the challenge.

## Evidence from this PC

| Request | Result |
| --- | --- |
| `GET https://meta.matcha.xyz/api/gas?chainId=1` | HTTP 200, JSON gas price |
| `POST https://meta.matcha.xyz/api/competitions`, Ethereum | HTTP 429, HTML `Vercel Security Checkpoint` |
| `POST https://meta.matcha.xyz/api/competitions`, Solana | HTTP 429, same checkpoint, also using the application's `https://meta.matcha.xyz` origin |

The failed responses contain `server: Vercel` and
`x-vercel-mitigated: challenge`. They do not contain `Retry-After`.
The bot's own checks at 10:30 local time reproduced the same failure:

- Ethereum request ID: `hkg1::1788838246-3tzBgGjQVnmT84dTjciSkpoZZjN8kHHi`
- Solana request ID: `hkg1::1788838247-yjHJIym0yV5A9KCCuut0qLS4mbm8pyGe`

Vercel documents that browser challenges can prevent standalone APIs and
unrecognized automated clients from accessing a site:
[Vercel Attack Mode documentation](https://vercel.com/docs/vercel-firewall/attack-mode).
This does **not** establish that Matcha enabled Attack Mode specifically;
other firewall rules can also issue challenges. MetaMatcha's operator can
inspect the request IDs and its firewall logs to determine the precise rule.

The earlier hosted VM's plain JSON HTTP 403 is a separate observation. The
PC evidence does not prove that the VM denial had the same cause.

### Browser comparison

A normal browser session on this PC loaded MetaMatcha and returned simulated
results for a test input of 100 USDC on Ethereum, with 99.983688 USDG displayed
as the best quote. No wallet was connected, no transaction was signed, and the
test tab was closed afterward to stop automatic quote refreshes.

A separate HTTP-only test using curl_cffi 0.11.4's default `chrome` profile and
the application's `https://meta.matcha.xyz` origin still received HTTP 429 with
`x-vercel-mitigated: challenge` from Solana competition creation. Changing the
supported browser profile alone did not restore HTTP-client access.

This confirms that browser quote access and the bot's HTTP access differ on
this PC. A browser-based quote adapter is a possible next implementation to
test, but it is not implemented or verified for wallet-specific executable
quotes. The displayed browser quote alone cannot establish that such an
adapter will meet the bot's transaction-validation requirements.

## Why the old logs were misleading

Both HTTP clients treated every 429 as a temporary service/rate failure.
The sniper shortened the error, discarding the endpoint and HTML checkpoint
evidence. Ethereum could repeat an entire route up to three times; Solana
could repeat its helper three times with only 250/500 ms between attempts.

With default aggregator settings, a successful Ethereum quote attempt requests
gas, creates a competition, then asks ten aggregators: twelve HTTP requests.
Solana creates a competition and asks two aggregators: three requests.
Both chain workers run independently. These settings can amplify traffic, but
the observed failures occur at competition creation, before aggregator fan-out.
The evidence does not establish that request volume triggered the checkpoint.

## Code changes

- Detect Vercel mitigation headers and checkpoint HTML before classifying 429.
- Preserve the endpoint, status and safe request ID in the operator message.
- Treat challenges as access denials using the configured provider cooldown.
- Stop immediate route/helper retries for genuine 429 rate limits as well.
- Parse numeric and HTTP-date `Retry-After` headers and honor them as a minimum
  scanner wait, even when longer than the usual transient backoff maximum.
- Keep valid executable quotes from successful competitors when a sibling fails.
- Keep cookies, API keys, challenge tokens and HTML bodies out of diagnostics.

These changes improve diagnosis and retry behavior. They do not grant access
through a provider's browser challenge.

## Restore quotes

To keep MetaMatcha, ask its operator for supported automated API access and
provide the failed endpoint and request IDs. Opening the site in a browser can
help check human access, but it does not establish that the bot's independent
HTTP session is allowed. Changing the request origin did not resolve this PC's
challenge.

The bot also supports alternative DEX providers on Solana:

- Solana: `SOL_FLASH_ARB_DEX_PROVIDER=jupiter`, with `JUP_API_KEY` or
  `JUP_API_KEYS`. The configured credentials returned HTTP 200 with a direct
  USDC-to-USDG quote during diagnosis. This only verifies quote access; it does
  not establish a profitable, fully simulated atomic trade.

Provider selection was not changed automatically. After changing configuration,
stop the existing sniper and validate with `python pyusd_usdg_sniper.py --once`
before restarting live.

## Collect a fresh report on the PC or VM

```bash
python scripts/diagnose_metamatcha.py --chain ethereum --output metamatcha-diagnosis.json
```

Use `--chain solana` to probe Solana competition creation. Each run performs
one Ethereum gas/control GET and at most one competition POST, then stops.
It makes no quote execution requests, reads no wallet keys or `.env`, does
not sign or broadcast, and never retries a denied request. A nonzero exit
status means a probe failed; the JSON report contains the available diagnosis.
