import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
# Always move these to the front: another test may have put the repository
# root first, where same-named root launch wrappers would shadow the engines.
for sub in ("", "engines"):
    path = str(SRC_DIR / sub) if sub else str(SRC_DIR)
    while path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

import eth_flash_arb_pyusd_usdc as engine  # noqa: E402
from crosschain_sniper import Route, dex_market_key, failure_category  # noqa: E402
from defillama_eth import ROUTERS, DefiLlamaQuoteClient  # noqa: E402

EXECUTOR = "0xB00f38246ea6870c2e3ed6DBa9d542a9a3fb6920"
OTHER = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
ONEINCH = ROUTERS["1inch"][0]
KYBER = ROUTERS["KyberSwap"][0]
ODOS = ROUTERS["Odos"][0]
KEY = "my-defillama-key-123"
MODULES = {
    "ArbError": engine.ArbError,
    "MatchaQuote": engine.MatchaQuote,
    "ProviderAccessBlockedError": engine.ProviderAccessBlockedError,
    "ProviderRateLimitedError": engine.ProviderRateLimitedError,
    "RetryableArbError": engine.RetryableArbError,
    "is_address": engine.is_address,
    "is_hex_data": engine.is_hex_data,
    "parse_integer": engine.parse_integer,
}


def response(status, payload=None, *, headers=None, text=None):
    item = MagicMock()
    item.status_code = status
    item.headers = headers or {"content-type": "application/json"}
    item.json.return_value = payload
    item.text = text if text is not None else str(payload)
    return item


def oneinch_payload(amount_out="1001000000", sender=EXECUTOR, target=ONEINCH):
    return {
        "amountReturned": amount_out,
        "estimatedGas": 210000,
        "tokenApprovalAddress": ONEINCH,
        "rawQuote": {"dstAmount": amount_out,
                     "tx": {"from": sender, "to": target, "data": "0x12aa3caf0000", "value": "0"}},
    }


def kyber_payload(amount_in="1000000000", amount_out="1002000000"):
    return {
        "amountReturned": amount_out,
        "estimatedGas": "180000",
        "tokenApprovalAddress": KYBER,
        "rawQuote": {"amountIn": amount_in, "amountOut": amount_out, "routerAddress": KYBER,
                     "data": "0xe21fd0e90000", "transactionValue": "0"},
    }


class DefiLlamaQuoteClientTests(unittest.TestCase):
    def client(self, handler, protocols, **kwargs):
        session = MagicMock()
        session.post.side_effect = handler
        return DefiLlamaQuoteClient(
            executor=EXECUTOR, protocols=protocols, session=session, api_modules=MODULES,
            token_symbols={engine.PYUSD: "PYUSD", engine.USDC: "USDC"}, **kwargs,
        ), session

    def quotes(self, client, amount=1_000_000_000):
        return client.quotes(amount, 10, sell_token_address=engine.PYUSD, buy_token_address=engine.USDC)

    def test_oneinch_route_is_built_for_the_executor(self):
        def handler(url, *, params, json, **kwargs):
            self.assertEqual(url, "https://swap-api.defillama.com/dexAggregatorQuote")
            self.assertEqual(params["protocol"], "1inch")
            self.assertEqual(params["chain"], "ethereum")
            self.assertEqual(params["from"], engine.PYUSD.lower())
            self.assertEqual(params["to"], engine.USDC.lower())
            self.assertEqual(params["amount"], "1000000000")
            self.assertEqual(params["api_key"], KEY)
            self.assertEqual(json["userAddress"], EXECUTOR)
            self.assertEqual(json["slippage"], "0.1")
            self.assertEqual(json["fromToken"], {"address": engine.PYUSD.lower(), "decimals": 6, "symbol": "PYUSD"})
            self.assertFalse(kwargs["allow_redirects"])
            return response(200, oneinch_payload())

        with patch.dict(os.environ, {"DEFILLAMA_API_KEY": KEY}):
            client, _ = self.client(handler, ["1inch"])
        [quote] = self.quotes(client)
        self.assertEqual(quote.aggregator, "1inch")
        self.assertEqual(quote.target, ONEINCH)
        self.assertEqual(quote.allowance_target, ONEINCH)
        self.assertEqual(quote.data, "0x12aa3caf0000")
        self.assertEqual((quote.value, quote.sell_amount, quote.buy_amount, quote.gas),
                         (0, 1_000_000_000, 1_001_000_000, 210000))

    def test_api_key_is_omitted_when_not_configured(self):
        def handler(url, *, params, **kwargs):
            self.assertNotIn("api_key", params)
            return response(200, oneinch_payload())

        with patch.dict(os.environ, {"DEFILLAMA_API_KEY": ""}):
            client, _ = self.client(handler, ["1inch"])
        self.assertEqual(len(self.quotes(client)), 1)

    def test_flat_kyberswap_and_nested_odos_routes_rank_by_output(self):
        def handler(url, *, params, **kwargs):
            if params["protocol"] == "KyberSwap":
                return response(200, kyber_payload())
            return response(200, {
                "amountReturned": "1003000000", "estimatedGas": 0, "tokenApprovalAddress": ODOS,
                "rawQuote": {"transaction": {"from": EXECUTOR, "to": ODOS, "data": "0x83bd37f90000", "value": "0"}},
            })

        client, _ = self.client(handler, ["KyberSwap", "Odos"])
        best, second = self.quotes(client)
        self.assertEqual((best.aggregator, best.target, best.gas), ("Odos", ODOS, None))
        self.assertEqual((second.aggregator, second.target, second.gas), ("KyberSwap", KYBER, 180000))

    def test_route_to_an_unpinned_router_is_rejected(self):
        client, _ = self.client(lambda *a, **k: response(200, oneinch_payload(target=OTHER)), ["1inch"])
        with self.assertRaisesRegex(engine.ArbError, "expected the published router"):
            self.quotes(client)

    def test_calldata_built_for_another_sender_is_rejected(self):
        client, _ = self.client(lambda *a, **k: response(200, oneinch_payload(sender=OTHER)), ["1inch"])
        with self.assertRaisesRegex(engine.ArbError, "not the executor"):
            self.quotes(client)

    def test_changed_sell_amount_and_native_value_are_rejected(self):
        client, _ = self.client(lambda *a, **k: response(200, kyber_payload(amount_in="999")), ["KyberSwap"])
        with self.assertRaisesRegex(engine.ArbError, "sell amount changed"):
            self.quotes(client)
        payload = kyber_payload()
        payload["rawQuote"]["transactionValue"] = "5"
        client, _ = self.client(lambda *a, **k: response(200, payload), ["KyberSwap"])
        with self.assertRaisesRegex(engine.ArbError, "requires 5 wei of ETH"):
            self.quotes(client)

    def test_cloudflare_challenge_is_reported_once_as_an_access_block(self):
        challenge = response(403, headers={"content-type": "text/html", "cf-mitigated": "challenge",
                                           "cf-ray": "9a1b2c3d4e5f6789-SIN"},
                             text="<!DOCTYPE html><title>Just a moment...</title>")
        with patch.dict(os.environ, {"DEFILLAMA_API_KEY": KEY}):
            client, session = self.client(lambda *a, **k: challenge, ["1inch", "KyberSwap"])
        with self.assertRaises(engine.ProviderAccessBlockedError) as caught:
            self.quotes(client)
        message = str(caught.exception)
        self.assertIn("Cloudflare bot challenge (HTTP 403; cf-ray=9a1b2c3d4e5f6789-SIN)", message)
        self.assertIn("not bypassed", message)
        self.assertNotIn(KEY, message)
        self.assertEqual(session.post.call_count, 2)

    def test_rejected_key_is_an_access_block(self):
        with patch.dict(os.environ, {"DEFILLAMA_API_KEY": KEY}):
            client, _ = self.client(lambda *a, **k: response(401, {"message": "bad key"}), ["1inch"])
        with self.assertRaisesRegex(engine.ProviderAccessBlockedError, "DEFILLAMA_API_KEY was rejected"):
            self.quotes(client)

    def test_transport_errors_never_leak_the_api_key(self):
        def handler(*args, **kwargs):
            raise ConnectionError(f"Max retries exceeded with url: /dexAggregatorQuote?api_key={KEY} (Read timed out.)")

        with patch.dict(os.environ, {"DEFILLAMA_API_KEY": KEY}):
            client, _ = self.client(handler, ["1inch"])
        with self.assertRaises(engine.RetryableArbError) as caught:
            self.quotes(client)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIn("timed out", str(caught.exception))

    def test_rate_limit_keeps_retry_after(self):
        limited = response(429, headers={"retry-after": "30"}, text="slow down")
        client, _ = self.client(lambda *a, **k: limited, ["1inch"])
        with self.assertRaises(engine.ProviderRateLimitedError) as caught:
            self.quotes(client)
        self.assertEqual(caught.exception.retry_after_seconds, 30.0)
        self.assertIn("retry-after=30s", str(caught.exception))

    def test_one_failing_adapter_does_not_block_the_others(self):
        def handler(url, *, params, **kwargs):
            if params["protocol"] == "1inch":
                return response(500, {"message": "upstream"})
            return response(200, kyber_payload())

        client, _ = self.client(handler, ["1inch", "KyberSwap"])
        [quote] = self.quotes(client)
        self.assertEqual(quote.aggregator, "KyberSwap")
        self.assertTrue(any("1inch: DefiLlama HTTP 500" in e for e in client.last_errors))

    def test_all_server_errors_are_retryable(self):
        client, _ = self.client(lambda *a, **k: response(502, text="bad gateway"), ["1inch", "KyberSwap"])
        with self.assertRaisesRegex(engine.RetryableArbError, "all DefiLlama quotes failed"):
            self.quotes(client)

    def test_adapter_error_payload_is_not_retryable(self):
        client, _ = self.client(lambda *a, **k: response(200, {"error": "no route"}), ["1inch"])
        with self.assertRaises(engine.ArbError) as caught:
            self.quotes(client)
        self.assertNotIsInstance(caught.exception, engine.RetryableArbError)
        self.assertIn("1inch: no route", str(caught.exception))

    def test_protocols_without_a_pinned_router_are_skipped(self):
        client, session = self.client(lambda *a, **k: response(200, oneinch_payload()), ["CowSwap", "1inch"])
        self.assertEqual(len(self.quotes(client)), 1)
        self.assertEqual(client.last_skipped, ["CowSwap: no pinned Ethereum router"])
        self.assertEqual(session.post.call_count, 1)
        client, _ = self.client(lambda *a, **k: None, ["CowSwap"])
        with self.assertRaisesRegex(engine.ArbError, "no DefiLlama protocols enabled"):
            self.quotes(client)


class DefiLlamaEngineIntegrationTests(unittest.TestCase):
    def test_matcha_client_uses_only_defillama_when_selected(self):
        http = MagicMock()
        client = engine.MatchaClient(http, quote_provider="defillama")
        quote = engine.MatchaQuote("1inch", ONEINCH, ONEINCH, "0x12aa3caf0000", 0, 1000, 1001)
        with patch("defillama_eth.DefiLlamaQuoteClient") as factory:
            factory.return_value.executor = EXECUTOR
            factory.return_value.quotes.return_value = [quote]
            responses = client.quotes(EXECUTOR, 1000, 10, engine.MATCHA_AGGREGATORS,
                                      sell_token_address=engine.PYUSD, buy_token_address=engine.USDC)
            client.quotes(EXECUTOR, 1000, 10, engine.MATCHA_AGGREGATORS)
        self.assertEqual(responses, [("1inch", quote)])
        self.assertEqual(factory.call_count, 1)  # reused for the same executor
        self.assertEqual(factory.call_args.kwargs["token_symbols"][engine.PYUSD], "PYUSD")
        self.assertEqual(http.method_calls, [])  # MetaMatcha is never contacted
        self.assertIs(engine.select_best_matcha_quote(responses, 1000), quote)

    def test_defillama_errors_propagate_without_fallback(self):
        client = engine.MatchaClient(MagicMock(), quote_provider="defillama")
        with patch("defillama_eth.DefiLlamaQuoteClient") as factory, \
                patch.object(client, "_direct_quotes") as direct, \
                patch.object(client, "_matcha_quotes") as matcha:
            factory.return_value.executor = EXECUTOR
            factory.return_value.quotes.side_effect = engine.ProviderAccessBlockedError("blocked")
            with self.assertRaises(engine.ProviderAccessBlockedError):
                client.quotes(EXECUTOR, 1000, 10, engine.MATCHA_AGGREGATORS)
        direct.assert_not_called()
        matcha.assert_not_called()

    def test_parser_accepts_defillama_provider(self):
        with patch.dict(os.environ, {"ETH_ARB_QUOTE_PROVIDER": "defillama"}):
            self.assertEqual(engine.parser().parse_args([]).quote_provider, "defillama")


class DefiLlamaSniperTests(unittest.TestCase):
    def test_access_blocks_never_route_to_matcha_proxy_rotation(self):
        for detail in (
            "ERROR: DefiLlama https://swap-api.defillama.com/dexAggregatorQuote answered with a "
            "Cloudflare bot challenge (HTTP 403; cf-ray=abc-SIN)",
            "Matcha/0x v2: DefiLlama https://swap-api.defillama.com/dexAggregatorQuote denied access (HTTP 401)",
        ):
            with self.subTest(detail=detail):
                self.assertEqual(failure_category(detail), "access-blocked-defillama")

    def test_server_errors_pause_defillama_not_matcha(self):
        detail = "all DefiLlama quotes failed: Matcha/0x v2: DefiLlama HTTP 502: bad gateway"
        self.assertEqual(failure_category(detail), "transient-defillama")

    @patch.dict(os.environ, {"ETH_ARB_QUOTE_PROVIDER": "defillama"})
    def test_routes_are_labelled_and_keyed_by_defillama(self):
        route = Route("ethereum", "USDC/USDG")
        self.assertEqual(route.dex_name, "DefiLlama")
        self.assertEqual(dex_market_key(route), "defillama:ethereum:USDG/USDC")


if __name__ == "__main__":
    unittest.main()
