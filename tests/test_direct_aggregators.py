import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
for sub in ("", "engines"):
    path = str(SRC_DIR / sub) if sub else str(SRC_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)

import eth_flash_arb_pyusd_usdc as engine  # noqa: E402
from direct_aggregators import DirectAggregatorClient  # noqa: E402

EXECUTOR = "0xB00f38246ea6870c2e3ed6DBa9d542a9a3fb6920"
OPERATOR = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
KYBER_ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
AUGUSTUS = "0x6A000F20005980200259B80c5102003040001068"
MODULES = {
    "ArbError": engine.ArbError,
    "MatchaQuote": engine.MatchaQuote,
    "ProviderRateLimitedError": engine.ProviderRateLimitedError,
    "RetryableArbError": engine.RetryableArbError,
    "is_address": engine.is_address,
    "is_hex_data": engine.is_hex_data,
    "parse_integer": engine.parse_integer,
}


def response(status, payload):
    item = MagicMock()
    item.status_code = status
    item.json.return_value = payload
    item.text = str(payload)
    return item


class DirectAggregatorTests(unittest.TestCase):
    def client(self, handler, aggregators):
        session = MagicMock()
        session.request.side_effect = handler
        return DirectAggregatorClient(
            executor=EXECUTOR, operator=OPERATOR, aggregators=aggregators,
            session=session, api_modules=MODULES,
        ), session

    def test_kyberswap_builds_executor_route(self):
        def handler(method, url, **kwargs):
            if url.endswith("/routes"):
                return response(200, {"data": {"routeSummary": {"amountOut": "99"}, "routerAddress": KYBER_ROUTER}})
            self.assertEqual(kwargs["json"]["sender"], EXECUTOR)
            self.assertEqual(kwargs["json"]["recipient"], EXECUTOR)
            self.assertEqual(kwargs["json"]["origin"], OPERATOR)
            self.assertTrue(kwargs["json"]["skipSimulateTx"])
            return response(200, {"data": {"amountIn": "1000", "amountOut": "1001", "routerAddress": KYBER_ROUTER,
                                           "data": "0xabcdef1234", "transactionValue": "0", "gas": "500000"}})
        client, _ = self.client(handler, ["kyberswap"])
        (quote,) = client.quotes(1000, 0, sell_token_address=engine.PYUSD, buy_token_address=engine.USDG)
        self.assertEqual((quote.aggregator, quote.target, quote.allowance_target), ("KyberSwap", KYBER_ROUTER, KYBER_ROUTER))
        self.assertEqual((quote.sell_amount, quote.buy_amount, quote.gas), (1000, 1001, 500000))

    def test_velora_uses_token_transfer_proxy_and_min_dest(self):
        def handler(method, url, **kwargs):
            if url.endswith("/prices"):
                return response(200, {"priceRoute": {"destAmount": "10000", "tokenTransferProxy": AUGUSTUS, "gasCost": "300000"}})
            self.assertEqual(kwargs["json"]["destAmount"], "9990")  # 10 bps slippage
            self.assertEqual(kwargs["json"]["userAddress"], EXECUTOR)
            return response(200, {"to": AUGUSTUS, "data": "0x12345678ab", "value": "0"})
        client, _ = self.client(handler, ["velora"])
        (quote,) = client.quotes(10000, 10, sell_token_address=engine.PYUSD, buy_token_address=engine.USDG)
        self.assertEqual((quote.aggregator, quote.buy_amount, quote.allowance_target), ("Velora", 10000, AUGUSTUS))

    def test_best_quote_first_and_failures_isolated(self):
        def handler(method, url, **kwargs):
            if "kyberswap" in url:
                return response(403, {"message": "blocked"})
            if url.endswith("/prices"):
                return response(200, {"priceRoute": {"destAmount": "1002", "tokenTransferProxy": AUGUSTUS}})
            return response(200, {"to": AUGUSTUS, "data": "0x12345678ab", "value": "0"})
        client, _ = self.client(handler, ["kyberswap", "velora"])
        quotes = client.quotes(1000, 0, sell_token_address=engine.PYUSD, buy_token_address=engine.USDG)
        self.assertEqual([q.aggregator for q in quotes], ["Velora"])
        self.assertTrue(any("kyberswap" in e for e in client.last_errors))

    def test_keyless_providers_without_keys_are_skipped(self):
        client, session = self.client(lambda *a, **k: None, ["1inch", "0x"])
        client.oneinch_key = client.zero_ex_key = ""
        with self.assertRaises(engine.ArbError):
            client.quotes(1000, 0, sell_token_address=engine.PYUSD, buy_token_address=engine.USDG)
        session.request.assert_not_called()
        self.assertEqual(len(client.last_skipped), 2)

    def test_zero_ex_passes_tx_origin_and_net_buy_amount(self):
        def handler(method, url, **kwargs):
            self.assertEqual(kwargs["params"]["txOrigin"], OPERATOR)
            self.assertEqual(kwargs["headers"]["0x-version"], "v2")
            return response(200, {"liquidityAvailable": True, "buyAmount": "985",
                                  "transaction": {"to": "0x0000000000001fF3684f28c67538d4D072C22734", "data": "0x2213bc0b", "value": "0", "gas": "400000"},
                                  "issues": {"allowance": {"spender": "0x0000000000001fF3684f28c67538d4D072C22734"}}})
        client, _ = self.client(handler, ["0x"])
        client.zero_ex_key = "test"
        (quote,) = client.quotes(1000, 0, sell_token_address=engine.PYUSD, buy_token_address=engine.USDG)
        self.assertEqual((quote.aggregator, quote.buy_amount), ("0x", 985))

    def test_bitget_signed_request_and_net_out_amount(self):
        router = "0xBc1D9760bd6ca468CA9fB5Ff2CFbEAC35d86c973"
        def handler(method, url, **kwargs):
            headers = kwargs["headers"]
            expected = DirectAggregatorClient.bitget_signature(
                "/bgw-pro/swapx/pro/swap", kwargs["data"], "k", "s", headers["x-api-timestamp"])
            self.assertEqual(headers["x-api-signature"], expected)
            self.assertIn('"executorAddress":"%s"' % EXECUTOR, kwargs["data"])
            return response(200, {"status": 0, "data": {"outAmount": "997", "swapTransaction": {
                "to": router, "data": "0xd984396a00", "value": "0", "gasAmount": "900000"}}})
        client, _ = self.client(handler, ["bitget"])
        client.bitget_key, client.bitget_secret = "k", "s"
        (quote,) = client.quotes(1000, 0, sell_token_address=engine.PYUSD, buy_token_address=engine.USDG)
        self.assertEqual((quote.aggregator, quote.buy_amount, quote.allowance_target), ("Bitget", 997, router))

    def test_bitget_signature_matches_documented_format(self):
        import base64, hashlib, hmac
        payload = '{"apiPath":"/p","body":"{}","x-api-key":"k","x-api-timestamp":"1"}'
        expected = base64.b64encode(hmac.new(b"s", payload.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(DirectAggregatorClient.bitget_signature("/p", "{}", "k", "s", "1"), expected)

    def test_engine_selects_prebuilt_quotes(self):
        quote = engine.MatchaQuote("Velora", AUGUSTUS, AUGUSTUS, "0x12", 0, 1000, 1005)
        other = engine.MatchaQuote("KyberSwap", KYBER_ROUTER, KYBER_ROUTER, "0x12", 0, 1000, 1001)
        best = engine.select_best_matcha_quote([("KyberSwap", other), ("Velora", quote)], 1000)
        self.assertEqual(best.aggregator, "Velora")
        with self.assertRaises(engine.ArbError):
            engine.select_best_matcha_quote([("Velora", quote)], 999)


if __name__ == "__main__":
    unittest.main()
