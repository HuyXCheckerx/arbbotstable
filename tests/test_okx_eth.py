import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
for sub in ("", "engines"):
    path = str(SRC_DIR / sub) if sub else str(SRC_DIR)
    while path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

import eth_flash_arb_pyusd_usdc as engine
from crosschain_sniper import Route, failure_category
from direct_aggregators import (
    DirectAggregatorClient,
    OKX_DEFAULT_ROUTER,
    OKX_DEFAULT_SPENDER,
)

EXECUTOR = "0xD48Ab89581e77b103014DCC922279855a3b2a940"
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


def okx_swap_payload(
    amount_out="100008326",
    router=OKX_DEFAULT_ROUTER,
    data="0x0d5f0e3b0000000000000000",
    gas="250000",
):
    return {
        "code": "0",
        "msg": "",
        "data": [
            {
                "routerResult": {
                    "chainIndex": "1",
                    "toTokenAmount": amount_out,
                    "fromTokenAmount": "100000000",
                },
                "tx": {
                    "to": router,
                    "from": EXECUTOR,
                    "data": data,
                    "gas": gas,
                    "value": "0",
                },
            }
        ],
    }


class OKXQuoteClientTests(unittest.TestCase):
    def client(self, handler, aggregators=("okx",)):
        session = MagicMock()
        session.request.side_effect = handler
        session.headers = {"accept": "application/json"}
        return DirectAggregatorClient(
            executor=EXECUTOR,
            operator=None,
            aggregators=aggregators,
            session=session,
            api_modules=MODULES,
        ), session

    def test_okx_quote_and_swap_calldata_structure(self):
        def handler(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertTrue(url.startswith("https://www.okx.com/api/v6/dex/aggregator/swap"))
            params = kwargs.get("params") or {}
            headers = kwargs.get("headers") or {}
            self.assertIn("OK-ACCESS-KEY", headers)
            self.assertIn("OK-ACCESS-SIGN", headers)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = okx_swap_payload()
            return resp

        client, _ = self.client(handler)
        quotes = client.quotes(
            100_000_000,
            10,
            sell_token_address=engine.PYUSD,
            buy_token_address=engine.USDC,
        )
        self.assertEqual(len(quotes), 1)
        quote = quotes[0]
        self.assertEqual(quote.aggregator, "OKX")
        self.assertEqual(quote.target, OKX_DEFAULT_ROUTER)
        self.assertEqual(quote.allowance_target, OKX_DEFAULT_SPENDER)
        self.assertEqual(quote.sell_amount, 100_000_000)
        self.assertEqual(quote.buy_amount, 100_008_326)
        self.assertEqual(quote.value, 0)
        self.assertTrue(quote.data.startswith("0x0d5f0e3b"))

    def test_okx_failure_category_and_route_name(self):
        route = Route("ethereum", "PYUSD/USDC", "dex-first")
        with patch.dict(os.environ, {"ETH_ARB_QUOTE_PROVIDER": "okx"}):
            self.assertEqual(route.dex_name, "OKX")

        cat = failure_category("okx: swap failed HTTP 429 rate limited")
        self.assertEqual(cat, "transient-okx")


if __name__ == "__main__":
    unittest.main()
