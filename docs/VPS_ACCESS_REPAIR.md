# Windows VPS access repair — 2026-09-11

## Update: proxy removal requested — 2026-09-12

The user requested removal of outbound proxies. Local MetaMatcha clients,
browser cookie collection, and diagnostic scripts now connect directly and
ignore inherited HTTP proxy settings. Proxy credentials and rotation calls
were removed from source and the environment template. The local `.env`
contains none of the removed proxy settings.

All 62 focused tests pass locally and on the VPS. Direct Mac gas and competition
requests return HTTP 200 even with deliberately invalid proxy environment
variables. The nine-file removal patch was deployed to the VPS and both proxy
settings were removed from its `.env`. Backups are in
`C:\Users\Administrator\Desktop\arbbotstable\.cache\remove-proxy-20260912-123306`.

The old process (PID 472) exited cooperatively after releasing Windows console
text-selection mode, which had paused console output. The bot was restarted
at 14:02:41 VPS time on September 12 with its existing command:
`python sniper.py --pairs PYUSD/USDG --live --confirm-live EXECUTE_PROFIT_SNIPER`.
Fresh Ethereum gas and Solana competition requests still returned HTTP 403,
and both provider routes entered their existing 3600-second cooldown. Proxy
removal and restart are complete; direct VPS provider access remains blocked.

The notes below describe the earlier investigation. The proxy restoration
plan and unsent support draft are superseded by the removal request.

## Verified failure

### Fresh comparison after restart — September 12

The user's Mac log from 14:24–14:31 shows continuous quotes and two reported
Solana confirmations at 14:29:57 and 14:30:55. It also contains the removed
`ProxyManager` log messages, demonstrating that the running Mac process still
has the older modules loaded. The log alone therefore is not an identical-code
comparison, and a confirmed transaction log alone does not establish realized
profit.

To isolate API access, the updated `scripts/diagnose_metamatcha.py` was run in
fresh processes on both machines with the same `chrome124` impersonation,
explicit direct connections, headers, and no cookies, `.env`, or wallet keys.
Both machines have `curl_cffi 0.16.3`.

| Probe | Mac | VPS |
| --- | --- | --- |
| UTC time | 07:35:17 | 07:37:03 |
| Gas GET | HTTP 200 JSON | HTTP 403 JSON |
| Competition POST | HTTP 200 JSON | Skipped after failed gas probe |
| `x-vercel-mitigated` | Absent | `deny` |
| Edge request ID | `hkg1::iad1::hrzfv-1789198517753-a92f92f49274` | `hkg1::fnlxj-1789198623346-31b23ddcf47e` |

Raw sanitized reports are saved locally in `.cache/mac-direct-current.json`
and on the VPS in `.cache/vps-direct-current.json`.

The confirmed failure is Vercel's deny action before the MetaMatcha application,
not a swap calculation, wallet configuration, missing browser cookie, or a
local timeout. Vercel documents this behavior at
https://vercel.com/docs/vercel-firewall/firewall-concepts.
The VPS source IP/network classification is the leading explanation for the
difference; the response does not disclose the matching rule. OS-specific
transport differences and other firewall signals are not exhaustively ruled
out. Matcha/Vercel firewall logs are needed to distinguish a specific IP block,
network rule, persistent mitigation, or another traffic-classification rule.

The next concrete repair is provider-side review of the denied VPS request or
integration with a supported API whose credentials permit server automation.
Increasing RAM or repeating restarts will not change this observed deny.

Suggested Matcha support request (not sent): Please review Vercel firewall
request `hkg1::fnlxj-1789198623346-31b23ddcf47e`, received September 12, 2026 at
07:37:03 UTC from VPS `160.191.165.232`, for
`GET https://meta.matcha.xyz/api/gas?chainId=1`. The response is HTTP 403 with
`x-vercel-mitigated: deny`. An identical direct, cookie-free diagnostic succeeds
from our Mac. Please identify the matched rule and advise whether this VPS can
be allowed or which supported API should be used for server automation.

The Windows VPS at `160.191.165.232` reaches MetaMatcha directly but receives
HTTP 403 with `x-vercel-mitigated: deny`. A fresh test with the same
`curl_cffi` Chrome 124 profile on the Mac returns HTTP 200. The Mac also
successfully creates a diagnostic Ethereum competition without trading.

The proxy account is active through October 10, 2026. Its dashboard lists
HTTP `160.250.166.37:10452`, SOCKS5 `160.250.166.37:11452`, and the existing
allowlisted client IP `160.191.165.232`.

Both proxy protocols reset connections from that VPS, even when requesting
`https://api.ipify.org`. Re-saving the unchanged allowlist and rotating once
through the provider dashboard changed the displayed exit IP but did not
restore connectivity. The final tests report curl 56 for HTTP and curl 97
for SOCKS5, both with connection-reset messages. This failure occurs before
MetaMatcha can respond; the exact proxy-side cause requires provider investigation.

## Repairs applied

- Corrected the VPS `.env` rotation URL to the exact dashboard value. Its
  previous value differed from the account's URL. Verified the match by hash
  without logging the credential.
- Patched the active Ethereum engine and Solana helper so explicit firewall
  denials and ordinary rate limits use the existing error/cooldown handling
  without triggering another browser refresh or proxy rotation.
- Replaced the cookie collector's false success message with a collection
  count and an explicit statement that API access remains unverified.
- Removed the cookie manager's hardcoded rotation credential; rotation now
  requires the operator's configuration. The local environment template no
  longer distributes that credential or a preselected third-party proxy.

Backups on the VPS are in
`C:\Users\Administrator\Desktop\arbbotstable\.cache\provider-fix-20260911-231250`.
The backup contains the four changed engine/helper files and the previous `.env`.
Existing unrelated VPS changes were preserved. The live process was not restarted;
already-loaded modules need a restart to pick up the changes.

All 62 focused Python tests passed both locally and on the VPS. Submission
messages in those tests use mocked transactions. No diagnostic signed or
broadcast a trade. Proxy connectivity remains unresolved.

## Next verification

After ProxyISP restores access, test the configured proxy from the VPS against
an IP-check endpoint, then MetaMatcha gas and competition creation. A gas-only
success is insufficient. Run a quote-only route check before resuming/restarting
live operation. Do not shorten the provider cooldown to work around a denial.

## Support message draft — not sent

Subject: Active residential proxy resets HTTP and SOCKS5 connections from allowlisted VPS

Hello ProxyISP support,

Please investigate residential proxy order ORD-20260910-0009. It is active
through October 10, 2026. The dashboard lists HTTP 160.250.166.37:10452 and
SOCKS5 160.250.166.37:11452. The allowlisted client is my VPS 160.191.165.232,
and a direct public-IP check confirms that source address.

Both proxy endpoints immediately reset connections, including a basic request
to https://api.ipify.org. HTTP reports curl 56 (receive failure: connection was
reset); SOCKS5 reports curl 97 (send failure: connection was reset).

I re-saved the same allowlist and used the dashboard Rotate control. The
displayed exit IP changed, but both protocols still fail. Please check that the
allowlist is applied on the proxy node, that both ports map to this active order,
and that the node's forwarding service is healthy. Please provide the corrected
endpoint/configuration or restore the existing service.
