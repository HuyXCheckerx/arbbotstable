from decimal import Decimal
import unittest
from unittest.mock import Mock

from scripts.get_defillama_quotes import get_quote, raw_amount, redact, TOKENS, API


class DefillamaQuoteTests(unittest.TestCase):
    def call(self, response=None, error=None):
        session = Mock()
        session.post.return_value = response
        session.post.side_effect = error
        result = get_quote(session, key="test-secret", protocol="1inch", sell="USDG", buy="PYUSD",
                           amount_raw="100000000", taker="0x" + "1" * 40, slippage=Decimal("0.1"))
        return result, session

    def response(self, status=200, data=None, text="{}", headers=None):
        response = Mock(status_code=status, text=text, headers=headers or {})
        response.json.return_value = data
        return response

    def test_success_and_request_contract(self):
        result, session = self.call(self.response(data={"amountReturned": "100123456", "estimatedGas": 120000}))
        self.assertEqual(result["amount_out"], "100.123456")
        self.assertEqual(result["status"], "quoted")
        args, kwargs = session.post.call_args
        self.assertEqual(args, (API,))
        self.assertEqual(kwargs["params"]["from"], TOKENS["USDG"])
        self.assertEqual(kwargs["params"]["api_key"], "test-secret")
        self.assertEqual(kwargs["json"]["slippage"], 0.1)
        self.assertFalse(kwargs["allow_redirects"])

    def test_cloudflare_challenge_is_not_reported_as_invalid_key_or_no_liquidity(self):
        result, _ = self.call(self.response(status=403, text="<title>Just a moment...</title>",
                                          headers={"cf-mitigated": "challenge"}))
        self.assertEqual(result["status"], "error")
        self.assertIn("API key acceptance is unverified", result["error"])

    def test_transport_and_server_errors_do_not_leak_api_key(self):
        result, _ = self.call(error=RuntimeError("url?api_key=test-secret"))
        self.assertNotIn("test-secret", str(result))
        result, _ = self.call(self.response(status=401, data={"error": "Invalid key test-secret"}))
        self.assertEqual(result["http_status"], 401)
        self.assertNotIn("test-secret", str(result))
        self.assertEqual(redact({"test-secret": ["test-secret"]}, "test-secret"), {"<redacted>": ["<redacted>"]})

    def test_null_zero_and_error_responses_are_not_quotes(self):
        for data in (None, {}, {"error": "No route"}, {"amountReturned": "0"}, {"amountReturned": "NaN"}):
            result, _ = self.call(self.response(data=data))
            self.assertEqual(result["status"], "no_quote")

    def test_exact_token_units_and_invalid_amounts(self):
        self.assertEqual(raw_amount("100.123456"), "100123456")
        self.assertEqual(raw_amount("0.000001"), "1")
        for amount in ("0", "-1", "0.0000001", "NaN", "Infinity", "bad", "1e20"):
            with self.assertRaises(ValueError):
                raw_amount(amount)


if __name__ == "__main__":
    unittest.main()
