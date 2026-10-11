"""Ethereum route quotes from DefiLlama's LlamaSwap meta-aggregator.

A separate quote engine for the Ethereum DEX leg, selected with
``ETH_ARB_QUOTE_PROVIDER=defillama`` / ``--quote-provider defillama``. It never
falls back to MetaMatcha or the direct aggregator client: when DefiLlama refuses
a request, the route pauses with that reason.

swap.defillama.com sends one request per aggregator adapter to DefiLlama's
quote relay (``swap-api.defillama.com/dexAggregatorQuote``) with the taker as
``userAddress``. The relay runs that adapter server side and returns
``amountReturned``, ``estimatedGas``, ``tokenApprovalAddress`` and the
aggregator's own transaction in ``rawQuote``. Here the taker is the executor,
so the calldata is built for the flash-loan contract.

The relay is not a documented public API. On 2026-10-11 it answered plain
server requests with a Cloudflare bot challenge (HTTP 403, cf-mitigated:
challenge). This engine reports that as an access block and never tries to get
past it: no browser, cookies, TLS impersonation, rotating proxies or borrowed
frontend keys.

The relay picks both the call target and the spender, so each route must use
the Ethereum router its aggregator publishes before the executor approves or
calls anything.

Settings:
  DEFILLAMA_API_KEY                  your own DefiLlama key, sent as ``api_key``
  ETH_ARB_DEFILLAMA_PROTOCOLS        LlamaSwap adapter names, comma separated
  ETH_ARB_DEFILLAMA_TIMEOUT_SECONDS  per request timeout (default 12)
  ETH_ARB_DEFILLAMA_QUOTE_URL        relay endpoint override
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from typing import Any, Iterable

try:
    from .provider_http import rate_limit_detail, response_headers, safe_endpoint
except ImportError:  # Direct script execution.
    from provider_http import rate_limit_detail, response_headers, safe_endpoint


DEFILLAMA_QUOTE_URL = "https://swap-api.defillama.com/dexAggregatorQuote"
DEFAULT_PROTOCOLS = "1inch,KyberSwap,ParaSwap,Odos,Matcha/0x v2"

# LlamaSwap adapter name -> (call target, token spender) on Ethereum mainnet,
# as hardcoded in LlamaSwap's own adapters.
ROUTERS = {
    "1inch": ("0x111111125421cA6dc452d289314280a0f8842A65",) * 2,  # AggregationRouterV6
    "KyberSwap": ("0x6131B5fae19EA4f9D964eAc0408E4408b66337b5",) * 2,  # MetaAggregationRouterV2
    "ParaSwap": ("0x6A000F20005980200259B80c5102003040001068",) * 2,  # Augustus v6.2
    "Odos": ("0xCf5540fFFCdC3d510B18bFcA6d2b9987b0772559",) * 2,  # Odos router v2
    "Matcha/0x v2": ("0x0000000000001fF3684f28c67538d4D072C22734",) * 2,  # 0x AllowanceHolder
}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


class DefiLlamaQuoteClient:
    def __init__(
        self,
        *,
        executor: str,
        api_modules: dict[str, Any],
        protocols: Iterable[str] | None = None,
        token_symbols: dict[str, str] | None = None,
        decimals: int = 6,
        timeout: float = 12.0,
        session: Any | None = None,
    ) -> None:
        # Engine types are injected to avoid a circular import with the
        # engine module, which usually runs as __main__.
        self.ArbError = api_modules["ArbError"]
        self.MatchaQuote = api_modules["MatchaQuote"]
        self.ProviderAccessBlockedError = api_modules["ProviderAccessBlockedError"]
        self.ProviderRateLimitedError = api_modules["ProviderRateLimitedError"]
        self.RetryableArbError = api_modules["RetryableArbError"]
        self.is_address = api_modules["is_address"]
        self.is_hex_data = api_modules["is_hex_data"]
        self.parse_integer = api_modules["parse_integer"]
        self.executor = executor
        names = protocols or _env("ETH_ARB_DEFILLAMA_PROTOCOLS", DEFAULT_PROTOCOLS).split(",")
        self.protocols = list(dict.fromkeys(n.strip() for n in names if n.strip()))
        self.token_symbols = {k.lower(): v for k, v in (token_symbols or {}).items()}
        self.decimals = decimals
        self.timeout = float(_env("ETH_ARB_DEFILLAMA_TIMEOUT_SECONDS", str(timeout)))
        self.url = _env("ETH_ARB_DEFILLAMA_QUOTE_URL", DEFILLAMA_QUOTE_URL)
        self.api_key = _env("DEFILLAMA_API_KEY")
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
            session.headers.update({"accept": "application/json", "user-agent": "arbbotstable/1.0"})
        self.session = session
        self.last_errors: list[str] = []
        self.last_skipped: list[str] = []

    # ------------------------------------------------------------------ http
    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "<redacted>") if self.api_key else text

    def _request(self, protocol: str, sell: str, buy: str, amount: int, slippage_bps: int) -> Any:
        params = {"protocol": protocol, "chain": "ethereum", "from": sell.lower(),
                  "to": buy.lower(), "amount": str(amount)}
        if self.api_key:
            params["api_key"] = self.api_key
        body = {
            "userAddress": self.executor,
            "slippage": str(int(slippage_bps) / 100),  # percent, as LlamaSwap sends it
            "amountOut": "0",
            "fromToken": {"address": sell.lower(), "decimals": self.decimals,
                          "symbol": self.token_symbols.get(sell.lower(), "")},
            "toToken": {"address": buy.lower(), "decimals": self.decimals,
                        "symbol": self.token_symbols.get(buy.lower(), "")},
        }
        endpoint = safe_endpoint(self.url)
        try:
            response = self.session.post(self.url, params=params, json=body,
                                         timeout=self.timeout, allow_redirects=False)
        except Exception as exc:
            # Transport errors can echo the full URL, including api_key.
            raise self.RetryableArbError(
                f"{protocol}: DefiLlama {endpoint} request failed: {self._redact(str(exc))[:200]}"
            ) from None
        status = int(getattr(response, "status_code", 0) or 0)
        headers = response_headers(response)
        text = getattr(response, "text", "") or ""
        ray = headers.get("cf-ray", "")
        reference = f"; cf-ray={ray[:64]}" if ray.replace("-", "").isalnum() else ""
        challenged = headers.get("cf-mitigated", "").lower() == "challenge" or (
            status in (403, 503) and "just a moment" in text[:2000].lower()
        )
        if challenged:
            raise self.ProviderAccessBlockedError(
                f"DefiLlama {endpoint} answered with a Cloudflare bot challenge "
                f"(HTTP {status}{reference}); server requests are not accepted and "
                "the challenge is not bypassed"
            )
        if status in (401, 403):
            hint = "DEFILLAMA_API_KEY was rejected" if self.api_key else "no DEFILLAMA_API_KEY is set"
            raise self.ProviderAccessBlockedError(
                f"DefiLlama {endpoint} denied access (HTTP {status}{reference}); {hint}"
            )
        if status == 429:
            raise self.ProviderRateLimitedError(response, self.url)
        excerpt = self._redact(" ".join(text.split()))[:200]
        if status >= 500:
            raise self.RetryableArbError(f"{protocol}: DefiLlama HTTP {status}: {excerpt}")
        if status >= 400:
            raise self.ArbError(f"{protocol}: DefiLlama HTTP {status}: {excerpt}")
        try:
            return response.json()
        except ValueError:
            raise self.ArbError(f"{protocol}: DefiLlama returned non-JSON content") from None

    # ----------------------------------------------------------------- parse
    def _route(self, protocol: str, payload: Any, sell_amount: int) -> Any:
        ArbError = self.ArbError
        if not isinstance(payload, dict):
            raise ArbError(f"{protocol}: unexpected DefiLlama response")
        raw = payload.get("rawQuote")
        if not isinstance(raw, dict) or not raw:
            reason = payload.get("error") or payload.get("message") or "no transaction in rawQuote"
            raise ArbError(f"{protocol}: {self._redact(str(reason))[:160]}")
        # 1inch nests the transaction in rawQuote.tx, Odos and 0x v2 in
        # rawQuote.transaction; KyberSwap and ParaSwap return it flat.
        tx = next((raw[k] for k in ("tx", "transaction") if isinstance(raw.get(k), dict)), raw)
        target = tx.get("to") or tx.get("routerAddress")
        spender = payload.get("tokenApprovalAddress")
        data = tx.get("data")
        if not self.is_address(target) or not self.is_address(spender) or not self.is_hex_data(data):
            raise ArbError(f"{protocol}: incomplete target, allowance target, or calldata")
        expected_target, expected_spender = ROUTERS[protocol]
        if target.lower() != expected_target.lower() or spender.lower() != expected_spender.lower():
            raise ArbError(
                f"{protocol}: relay returned target {target} / spender {spender}; "
                f"expected the published router {expected_target}"
            )
        sender = tx.get("from")
        if sender and str(sender).lower() != self.executor.lower():
            raise ArbError(f"{protocol}: calldata was built for {sender}, not the executor")
        value = self.parse_integer(tx.get("value") or tx.get("transactionValue") or 0, f"{protocol} value")
        if value:
            raise ArbError(f"{protocol}: token swap unexpectedly requires {value} wei of ETH")
        amount_in = payload.get("amountIn") or raw.get("amountIn")
        if amount_in not in (None, "", 0, "0") and self.parse_integer(amount_in, f"{protocol} amountIn") != sell_amount:
            raise ArbError(f"{protocol}: sell amount changed")
        buy = self.parse_integer(payload.get("amountReturned"), f"{protocol} amountReturned")
        if buy <= 0:
            raise ArbError(f"{protocol}: non-positive buy amount")
        try:
            gas = self.parse_integer(payload.get("estimatedGas"), f"{protocol} gas") or None
        except ArbError:
            gas = None
        return self.MatchaQuote(
            aggregator=protocol,
            target=target,
            allowance_target=spender,
            data=data,
            value=0,
            sell_amount=sell_amount,
            buy_amount=buy,
            gas=gas,
        )

    def _quote(self, protocol: str, sell: str, buy: str, amount: int, slippage_bps: int) -> Any:
        return self._route(protocol, self._request(protocol, sell, buy, amount, slippage_bps), amount)

    # ------------------------------------------------------------------ api
    def quotes(
        self,
        sell_amount: int,
        slippage_bps: int,
        *,
        sell_token_address: str,
        buy_token_address: str,
    ) -> list[Any]:
        selected = [p for p in self.protocols if p in ROUTERS]
        self.last_skipped = [f"{p}: no pinned Ethereum router" for p in self.protocols if p not in ROUTERS]
        if not selected:
            raise self.ArbError("no DefiLlama protocols enabled: " + "; ".join(self.last_skipped))
        results: list[Any] = []
        errors: list[str] = []
        access_blocks: list[Exception] = []
        rate_limits: list[Exception] = []
        retryable: list[Exception] = []
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            futures = {
                pool.submit(self._quote, name, sell_token_address, buy_token_address,
                            sell_amount, slippage_bps): name
                for name in selected
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except self.ProviderAccessBlockedError as exc:
                    access_blocks.append(exc)
                except self.ProviderRateLimitedError as exc:
                    rate_limits.append(exc)
                except Exception as exc:  # one adapter failing never blocks the others
                    if isinstance(exc, self.RetryableArbError):
                        retryable.append(exc)
                    errors.append(str(exc) if str(exc).startswith(futures[future]) else f"{futures[future]}: {exc}")
        self.last_errors = errors + [str(e) for e in access_blocks + rate_limits]
        if not results:
            # The relay blocks or throttles every adapter alike, so report that once.
            if access_blocks:
                raise access_blocks[0]
            if rate_limits:
                raise max(rate_limits, key=lambda e: getattr(e, "retry_after_seconds", None) or 0)
            message = "all DefiLlama quotes failed: " + "; ".join(errors)
            if retryable and len(retryable) == len(errors):
                raise self.RetryableArbError(message)
            raise self.ArbError(message)
        results.sort(key=lambda q: (q.buy_amount, -(q.gas or 0)), reverse=True)
        return results
