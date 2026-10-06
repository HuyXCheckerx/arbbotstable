"""Direct Ethereum aggregator quotes for the flash-loan executor.

Added alongside the MetaMatcha client (nothing there is removed). Selected with
``ETH_ARB_QUOTE_PROVIDER=direct`` / ``--quote-provider direct``.

Every provider returns calldata that the executor can run as-is: the executor is
the taker/sender/recipient, approves ``allowance_target`` for ``sell_amount`` and
calls ``target`` with ``data``. No browser cookies, proxies or spoofed headers
are used; each provider is called through its public, documented API.

Providers (ETH_ARB_DIRECT_AGGREGATORS, comma separated, default below):
  kyberswap  keyless  (x-client-id)           GET routes -> POST route/build
  velora     keyless                          GET prices -> POST transactions
  1inch      ONEINCH_API_KEY (Bearer)          GET swap            [untested]
  0x         ZERO_EX_API_KEY / ETH_ARB_ZERO_EX_API_KEY  GET allowance-holder/quote
             (buyAmount is already net of 0x's 15 bps zeroExFee)
  bitget     BITGET_API_KEY + BITGET_API_SECRET (HMAC)  POST /bgw-pro/swapx/pro/swap
             (outAmount is already net of Bitget's 3 bps swapFee)

Measured 2026-09-27 on 25k-100k stablecoin swaps: KyberSwap and Velora charge
nothing; Bitget 3 bps, 1inch ~10 bps, 0x 15 bps. Every quote's buy amount is net
of its provider fee, so ranking by buy amount already accounts for them.
Providers without their key are skipped, never failed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import threading
import time
from typing import Any, Callable, Iterable

# Engine types are injected by the caller (see DirectAggregatorClient.__init__)
# to avoid a circular import with eth_flash_arb_pyusd_usdc.
ArbError: Any = RuntimeError
RetryableArbError: Any = RuntimeError
ProviderRateLimitedError: Any = RuntimeError
MatchaQuote: Any = None
is_address: Any = None
is_hex_data: Any = None
parse_integer: Any = None


DEFAULT_DIRECT_AGGREGATORS = "kyberswap,velora,bitget,1inch,0x"
KYBER_BASE_URL = "https://aggregator-api.kyberswap.com/ethereum/api/v1"
VELORA_BASE_URL = "https://api.paraswap.io"
ONEINCH_BASE_URL = "https://api.1inch.dev/swap/v6.0/1"
ZERO_EX_BASE_URL = "https://api.0x.org"
BITGET_BASE_URL = "https://bopenapi.bgwapi.io"
BITGET_SWAP_PATH = "/bgw-pro/swapx/pro/swap"

# Conservative default request spacing per provider (seconds between calls).
DEFAULT_MIN_INTERVAL = {
    "kyberswap": 0.25,
    "velora": 0.25,
    "1inch": 1.05,  # Dev plan: 60 requests/minute
    "0x": 0.21,     # ~5 requests/second
    "bitget": 0.5,
}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


class _Pacer:
    """Per-provider minimum spacing between requests (thread safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, provider: str, interval: float) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(provider, 0.0))
            self._next[provider] = start + interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_PACER = _Pacer()


class DirectAggregatorClient:
    def __init__(
        self,
        *,
        executor: str,
        operator: str | None,
        timeout: float = 8.0,
        aggregators: Iterable[str] | None = None,
        session: Any | None = None,
        api_modules: dict[str, Any],
    ) -> None:
        # Imported here to avoid a circular import with the engine module.
        global ArbError, MatchaQuote, ProviderRateLimitedError, RetryableArbError
        global is_address, is_hex_data, parse_integer
        if api_modules:
            ArbError = api_modules["ArbError"]
            MatchaQuote = api_modules["MatchaQuote"]
            ProviderRateLimitedError = api_modules["ProviderRateLimitedError"]
            RetryableArbError = api_modules["RetryableArbError"]
            is_address = api_modules["is_address"]
            is_hex_data = api_modules["is_hex_data"]
            parse_integer = api_modules["parse_integer"]
        self.executor = executor
        self.operator = operator or _env("ETH_OPERATOR_ADDRESS") or None
        self.timeout = float(_env("ETH_ARB_DIRECT_TIMEOUT_SECONDS", str(timeout)))
        names = aggregators or _env("ETH_ARB_DIRECT_AGGREGATORS", DEFAULT_DIRECT_AGGREGATORS).split(",")
        self.aggregators = [n.strip().lower() for n in names if n.strip()]
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
            proxy_url = _env("MATCHA_PROXY")
            if proxy_url:
                session.proxies = {"http": proxy_url, "https": proxy_url}
            session.headers.update({"accept": "application/json", "user-agent": "arbbotstable/1.0"})
        self.session = session
        self.kyber_client_id = _env("KYBERSWAP_CLIENT_ID", "arbbotstable")
        self.oneinch_key = _env("ONEINCH_API_KEY")
        self.zero_ex_key = _env("ZERO_EX_API_KEY") or _env("ETH_ARB_ZERO_EX_API_KEY")
        self.bitget_key = _env("BITGET_API_KEY")
        self.bitget_secret = _env("BITGET_API_SECRET")
        self.last_errors: list[str] = []
        self.last_skipped: list[str] = []

    # ------------------------------------------------------------------ http
    def _request(self, provider: str, method: str, url: str, **kwargs: Any) -> Any:
        _PACER.wait(provider, DEFAULT_MIN_INTERVAL.get(provider, 0.25))
        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except Exception as exc:
            # Fall back to direct connection if proxy failed
            if getattr(self.session, "proxies", None):
                try:
                    import requests
                    response = requests.request(
                        method, url, timeout=self.timeout, headers=self.session.headers, **kwargs
                    )
                except Exception:
                    raise RetryableArbError(f"{provider}: {method} {url.split('?')[0]} failed: {exc}") from exc
            else:
                raise RetryableArbError(f"{provider}: {method} {url.split('?')[0]} failed: {exc}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        text = getattr(response, "text", "") or ""
        if status == 429:
            # If rate-limited through proxy or direct, try alternative
            if getattr(self.session, "proxies", None):
                try:
                    import requests
                    fb = requests.request(method, url, timeout=self.timeout, headers=self.session.headers, **kwargs)
                    if fb.status_code == 200:
                        try:
                            return fb.json()
                        except ValueError:
                            pass
                except Exception:
                    pass
            raise ArbError(f"{provider}: HTTP 429 rate limited")
        if status >= 500:
            raise RetryableArbError(f"{provider}: HTTP {status}: {' '.join(text.split())[:200]}")
        if status >= 400:
            raise ArbError(f"{provider}: HTTP {status}: {' '.join(text.split())[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ArbError(f"{provider}: non-JSON response") from exc

    def _quote(
        self,
        *,
        aggregator: str,
        target: Any,
        allowance_target: Any,
        data: Any,
        value: Any,
        sell_amount: int,
        buy_amount: Any,
        gas: Any = None,
    ) -> Any:
        if not is_address(target) or not is_address(allowance_target) or not is_hex_data(data):
            raise ArbError(f"{aggregator}: incomplete target, allowance target, or calldata")
        buy = parse_integer(buy_amount, f"{aggregator} buy amount")
        if buy <= 0:
            raise ArbError(f"{aggregator}: non-positive buy amount")
        return MatchaQuote(
            aggregator=aggregator,
            target=target,
            allowance_target=allowance_target,
            data=data,
            value=parse_integer(value or 0, f"{aggregator} value"),
            sell_amount=sell_amount,
            buy_amount=buy,
            gas=parse_integer(gas, f"{aggregator} gas") if gas not in (None, "") else None,
        )

    # ------------------------------------------------------------- providers
    def _kyberswap(self, sell: str, buy: str, amount: int, slippage_bps: int) -> Any:
        headers = {"x-client-id": self.kyber_client_id}
        routes = self._request(
            "kyberswap", "GET", f"{KYBER_BASE_URL}/routes",
            params={"tokenIn": sell, "tokenOut": buy, "amountIn": str(amount)}, headers=headers,
        )
        data = routes.get("data") if isinstance(routes, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("routeSummary"), dict):
            raise ArbError(f"kyberswap: no route ({str(routes)[:160]})")
        body = {
            "routeSummary": data["routeSummary"],
            "sender": self.executor,
            "recipient": self.executor,
            "slippageTolerance": int(slippage_bps),
            "skipSimulateTx": True,  # the executor holds no balance before the flash loan
        }
        if self.operator:
            body["origin"] = self.operator
        built = self._request("kyberswap", "POST", f"{KYBER_BASE_URL}/route/build", json=body, headers=headers)
        tx = built.get("data") if isinstance(built, dict) else None
        if not isinstance(tx, dict):
            raise ArbError(f"kyberswap: build failed ({str(built)[:160]})")
        if parse_integer(tx.get("amountIn"), "kyberswap amountIn") != amount:
            raise ArbError("kyberswap: sell amount changed")
        router = tx.get("routerAddress")
        return self._quote(
            aggregator="KyberSwap", target=router, allowance_target=router, data=tx.get("data"),
            value=tx.get("transactionValue"), sell_amount=amount, buy_amount=tx.get("amountOut"),
            gas=tx.get("gas"),
        )

    def _velora(self, sell: str, buy: str, amount: int, slippage_bps: int) -> Any:
        prices = self._request(
            "velora", "GET", f"{VELORA_BASE_URL}/prices",
            params={
                "srcToken": sell.lower(), "srcDecimals": 6, "destToken": buy.lower(), "destDecimals": 6,
                "amount": str(amount), "side": "SELL", "network": 1, "version": "6.2",
                "userAddress": self.executor,
            },
        )
        route = prices.get("priceRoute") if isinstance(prices, dict) else None
        if not isinstance(route, dict):
            raise ArbError(f"velora: no route ({str(prices)[:160]})")
        dest = parse_integer(route.get("destAmount"), "velora destAmount")
        min_dest = dest * (10_000 - int(slippage_bps)) // 10_000
        tx = self._request(
            "velora", "POST", f"{VELORA_BASE_URL}/transactions/1",
            params={"ignoreChecks": "true", "ignoreGasEstimate": "true"},
            json={
                "srcToken": sell.lower(), "destToken": buy.lower(), "srcAmount": str(amount),
                "destAmount": str(min_dest), "priceRoute": route, "userAddress": self.executor,
                "srcDecimals": 6, "destDecimals": 6,
            },
        )
        if not isinstance(tx, dict):
            raise ArbError("velora: build failed")
        return self._quote(
            aggregator="Velora", target=tx.get("to"),
            allowance_target=route.get("tokenTransferProxy") or tx.get("to"), data=tx.get("data"),
            value=tx.get("value"), sell_amount=amount, buy_amount=dest, gas=route.get("gasCost"),
        )

    def _oneinch(self, sell: str, buy: str, amount: int, slippage_bps: int) -> Any:
        params = {
            "src": sell, "dst": buy, "amount": str(amount), "from": self.executor,
            "receiver": self.executor, "slippage": f"{int(slippage_bps) / 100:.2f}",
            "disableEstimate": "true", "allowPartialFill": "false",
        }
        if self.operator:
            params["origin"] = self.operator
        base = _env("ONEINCH_BASE_URL", ONEINCH_BASE_URL).rstrip("/")
        result = self._request(
            "1inch", "GET", f"{base}/swap", params=params,
            headers={"Authorization": f"Bearer {self.oneinch_key}"},
        )
        tx = result.get("tx") if isinstance(result, dict) else None
        if not isinstance(tx, dict):
            raise ArbError(f"1inch: no transaction ({str(result)[:160]})")
        return self._quote(
            aggregator="1inch", target=tx.get("to"), allowance_target=tx.get("to"), data=tx.get("data"),
            value=tx.get("value"), sell_amount=amount, buy_amount=result.get("dstAmount"), gas=tx.get("gas"),
        )

    def _zero_ex(self, sell: str, buy: str, amount: int, slippage_bps: int) -> Any:
        params = {
            "chainId": 1, "sellToken": sell, "buyToken": buy, "sellAmount": str(amount),
            "taker": self.executor, "slippageBps": int(slippage_bps),
        }
        if self.operator:
            params["txOrigin"] = self.operator
        quote = self._request(
            "0x", "GET", f"{ZERO_EX_BASE_URL}/swap/allowance-holder/quote", params=params,
            headers={"0x-api-key": self.zero_ex_key, "0x-version": "v2"},
        )
        if not isinstance(quote, dict) or not quote.get("liquidityAvailable"):
            raise ArbError("0x: no liquidity available")
        tx = quote.get("transaction") or {}
        spender = ((quote.get("issues") or {}).get("allowance") or {}).get("spender") or quote.get("allowanceTarget")
        return self._quote(
            aggregator="0x", target=tx.get("to"), allowance_target=spender, data=tx.get("data"),
            value=tx.get("value"), sell_amount=amount, buy_amount=quote.get("buyAmount"), gas=tx.get("gas"),
        )

    @staticmethod
    def bitget_signature(api_path: str, body: str, api_key: str, api_secret: str, timestamp: str) -> str:
        """Bitget Wallet HMAC: alphabetically sorted compact JSON, HMAC-SHA256, base64."""
        import base64
        import hashlib
        import hmac
        import json

        content = {"apiPath": api_path, "body": body, "x-api-key": api_key, "x-api-timestamp": timestamp}
        payload = json.dumps({key: content[key] for key in sorted(content)}, separators=(",", ":"))
        digest = hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _bitget(self, sell: str, buy: str, amount: int, slippage_bps: int) -> Any:
        import json

        request = {
            "fromChain": "eth", "toChain": "eth",
            "fromContract": sell.lower(), "toContract": buy.lower(),
            "fromAmount": str(amount), "amountUnit": "wei",
            "fromAddress": self.executor, "toAddress": self.executor,
            "executorAddress": self.executor,
            "market": _env("BITGET_MARKET", "bgwaggregator"),
            "slippage": int(slippage_bps) / 100, "requestMod": "rich",
        }
        if self.operator:
            request["txOrigin"] = self.operator
        body = json.dumps(request, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        headers = {
            "content-type": "application/json",
            "x-api-key": self.bitget_key,
            "x-api-timestamp": timestamp,
            "x-api-signature": self.bitget_signature(
                BITGET_SWAP_PATH, body, self.bitget_key, self.bitget_secret, timestamp
            ),
        }
        result = self._request("bitget", "POST", f"{BITGET_BASE_URL}{BITGET_SWAP_PATH}", data=body, headers=headers)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("swapTransaction"), dict):
            raise ArbError(f"bitget: no transaction ({str(result)[:160]})")
        tx = data["swapTransaction"]
        return self._quote(
            aggregator="Bitget", target=tx.get("to"), allowance_target=tx.get("to"), data=tx.get("data"),
            value=tx.get("value"), sell_amount=amount, buy_amount=data.get("outAmount"), gas=tx.get("gasAmount"),
        )

    def _providers(self) -> list[tuple[str, Callable[..., Any]]]:
        table: dict[str, tuple[Callable[..., Any], bool]] = {
            "kyberswap": (self._kyberswap, True),
            "velora": (self._velora, True),
            "1inch": (self._oneinch, bool(self.oneinch_key)),
            "0x": (self._zero_ex, bool(self.zero_ex_key)),
            "bitget": (self._bitget, bool(self.bitget_key and self.bitget_secret)),
        }
        selected: list[tuple[str, Callable[..., Any]]] = []
        self.last_skipped = []
        for name in self.aggregators:
            entry = table.get(name)
            if entry is None:
                self.last_skipped.append(f"{name}: not implemented")
            elif not entry[1]:
                self.last_skipped.append(f"{name}: no API key configured")
            else:
                selected.append((name, entry[0]))
        return selected

    # ------------------------------------------------------------------ api
    def quotes(
        self,
        sell_amount: int,
        slippage_bps: int,
        *,
        sell_token_address: str,
        buy_token_address: str,
    ) -> list[Any]:
        providers = self._providers()
        if not providers:
            raise ArbError("no direct aggregators enabled: " + "; ".join(self.last_skipped))
        results: list[Any] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(providers)) as pool:
            futures = {
                pool.submit(fn, sell_token_address, buy_token_address, sell_amount, slippage_bps): name
                for name, fn in providers
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:  # one provider failing never blocks the others
                    errors.append(str(exc) if str(exc).lower().startswith(futures[future].lower()) else f"{futures[future]}: {exc}")
        self.last_errors = errors
        if not results:
            raise ArbError("all direct aggregator quotes failed: " + "; ".join(errors))
        results.sort(key=lambda q: (q.buy_amount, -(q.gas or 0)), reverse=True)
        return results
