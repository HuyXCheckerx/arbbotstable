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


class BrowserQuoteTests(unittest.TestCase):
    def test_identity_excludes_keys_and_unrelated_hosts(self):
        from scripts.defillama_browser import quote_identity
        self.assertIsNone(quote_identity('https://example.com/dexAggregatorQuote?protocol=1inch'))
        value = quote_identity(API + '?protocol=1inch&api_key=secret&amount=100')
        self.assertEqual(value['protocol'], '1inch')
        self.assertNotIn('secret', str(value))

    def test_browser_response_never_exports_provider_payload(self):
        from scripts.defillama_browser import quote_result
        value = quote_result('1inch', 200, {}, {'amountReturned': '100123456', 'url': '?api_key=secret'})
        self.assertEqual(value['amount_out'], '100.123456')
        self.assertNotIn('secret', str(value))
        self.assertEqual(quote_result('1inch', 403, {'cf-mitigated': 'challenge'}, {})['status'], 'error')
        self.assertEqual(quote_result('1inch', 200, {}, {'amountReturned': 'NaN'})['status'], 'no_quote')

    def test_session_refresh_preserves_previous_quote_and_filters_pair(self):
        from scripts.defillama_browser import BrowserQuotes
        from urllib.parse import urlencode
        browser = BrowserQuotes(tokens=TOKENS, sell='USDG', buy='PYUSD', amount='100',
            slippage='0.1', protocols=['1inch'], state_path='/unused')
        browser.page = Mock()
        request = Mock(url=API + '?' + urlencode({'protocol': '1inch', 'from': TOKENS['USDG'],
                                                'to': TOKENS['PYUSD'], 'amount': '100000000'}))
        browser.request(request)
        response = Mock(url=request.url, status=200, headers={})
        response.json.return_value = {'amountReturned': '100123456'}
        browser.response(response)
        first = browser.snapshot(1)[0]
        browser.request(request)
        second = browser.snapshot(1)[0]
        self.assertEqual(first['amount_out'], second['amount_out'])
        self.assertEqual(first['updated_at'], second['updated_at'])
        self.assertTrue(second['refreshing'])
        browser.failed(request)
        self.assertEqual(browser.snapshot(1)[0]['status'], 'error')
        self.assertIsNone(browser.identity(request.url.replace('100000000', '200000000')))


    def test_watch_uses_one_context_and_defaults_to_visible_browser(self):
        from scripts.get_defillama_quotes import main
        from unittest.mock import patch
        import contextlib
        import io
        with patch('scripts.defillama_browser.BrowserQuotes') as factory:
            session = factory.return_value.__enter__.return_value
            session.snapshot.side_effect = [[{'protocol': '1inch', 'status': 'quoted',
                                             'amount_out': '99.98'}], KeyboardInterrupt()]
            with contextlib.redirect_stdout(io.StringIO()):
                result = main(['--watch', '--timeout', '1'])
            self.assertEqual(result, 130)
            factory.assert_called_once()
            self.assertTrue(factory.call_args.kwargs['headful'])
            self.assertNotIn('key', factory.call_args.kwargs)
            self.assertEqual(session.snapshot.call_count, 2)
            factory.return_value.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
