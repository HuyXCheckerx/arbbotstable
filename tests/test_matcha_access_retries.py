"""Exercise the real HTTP retry branches without network or browser launches."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.engines import eth_flash_arb_pyusd_usdc as eth
from src.engines import metamatcha_solana as sol
from src.engines import matcha_cookie_manager as cookies
from src.engines.provider_http import is_browser_challenge


URL = "https://meta.matcha.xyz/api/competitions"


def response(status, headers=None, body=""):
    return SimpleNamespace(status_code=status, headers=headers or {}, text=body)


class StaticSession:
    # A real fake is deliberate: the clients skip retry logic for Mock objects.
    proxies = None

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.result

    post = get


class MatchaAccessRetriesTests(unittest.TestCase):
    def test_sessions_ignore_inherited_proxy_settings(self):
        proxy_env = {key: "http://127.0.0.1:1" for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        )}
        proxy_env["MATCHA_PROXY"] = ""
        with patch.dict("os.environ", proxy_env, clear=False), \
                patch.object(cookies, "inject_matcha_cookies"), \
                patch.object(eth.cffi_requests, "Session") as factory:
            eth.HttpJsonClient(1, "test")
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertEqual(factory.call_args.kwargs["proxies"], {"http": "", "https": ""})
        with patch.dict("os.environ", proxy_env, clear=False), \
                patch.object(sol, "_SHARED_SESSION", None), \
                patch.object(sol, "inject_matcha_cookies"), \
                patch.object(sol.requests, "Session") as factory:
            sol._session()
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertEqual(factory.call_args.kwargs["proxies"], {"http": "", "https": ""})
        with patch.dict("os.environ", proxy_env, clear=False), \
                patch.object(cookies, "inject_matcha_cookies"), \
                patch.object(eth, "cffi_requests", None), \
                patch.object(eth.requests, "Session", return_value=Mock()) as factory:
            client = eth.HttpJsonClient(1, "test")
            self.assertFalse(client.session.trust_env)

    def test_sessions_use_matcha_proxy_when_explicitly_set(self):
        proxy_env = {"MATCHA_PROXY": "http://user:pass@1.2.3.4:8080"}
        expected_proxies = {"http": "http://user:pass@1.2.3.4:8080", "https": "http://user:pass@1.2.3.4:8080"}
        with patch.dict("os.environ", proxy_env), \
                patch.object(cookies, "inject_matcha_cookies"), \
                patch.object(eth.cffi_requests, "Session") as factory:
            eth.HttpJsonClient(1, "test")
            self.assertEqual(factory.call_args.kwargs["proxies"], expected_proxies)
        with patch.dict("os.environ", proxy_env), \
                patch.object(sol, "_SHARED_SESSION", None), \
                patch.object(sol, "inject_matcha_cookies"), \
                patch.object(sol.requests, "Session") as factory:
            sol._session()
            self.assertEqual(factory.call_args.kwargs["proxies"], expected_proxies)

    def test_denials_and_rate_limits_do_not_launch_browser(self):
        cases = [
            response(403, {"x-vercel-mitigated": "deny"}),
            response(401, body='{"error":"Unauthorized"}'),
            response(403, body='{"error":"Forbidden"}'),
            response(429, {"Retry-After": "600"}, "Too many requests"),
        ]
        for result in cases:
            for method in ("get", "post", "solana"):
                with self.subTest(status=result.status_code, method=method):
                    session = StaticSession(result)
                    with patch.object(cookies, "inject_matcha_cookies") as refresh, \
                            patch.object(sol, "inject_matcha_cookies") as sol_refresh:
                        if method == "solana":
                            with patch.object(sol, "_session", lambda **kwargs: session):
                                with self.assertRaises((sol.ProviderAccessBlockedError, sol.ProviderRateLimitedError)) as caught:
                                    sol._post_json(URL, {}, 1)
                        else:
                            client = eth.HttpJsonClient.__new__(eth.HttpJsonClient)
                            client.session = session
                            client.timeout = 1
                            with self.assertRaises((eth.ProviderAccessBlockedError, eth.ProviderRateLimitedError)) as caught:
                                if method == "get":
                                    client.get(URL)
                                else:
                                    client.post(URL, {})
                        self.assertEqual(session.calls, 1)
                        refresh.assert_not_called()
                        sol_refresh.assert_not_called()
                        if result.status_code == 429:
                            self.assertEqual(caught.exception.retry_after_seconds, 600)

    def test_browser_challenge_requires_positive_evidence(self):
        self.assertTrue(is_browser_challenge(response(429, {"x-vercel-mitigated": "challenge"})))
        html = "<html><title>Vercel Security Checkpoint</title></html>"
        self.assertTrue(is_browser_challenge(response(403, body=html)))
        self.assertFalse(is_browser_challenge(response(403, {"x-vercel-mitigated": "deny"}, html)))
        self.assertFalse(is_browser_challenge(response(200, body=html)))
        self.assertFalse(is_browser_challenge(response(429, body='{"error":"Vercel Security Checkpoint"}')))
        self.assertTrue(is_browser_challenge(response(503, body='<html><script src="/cdn-cgi/challenge-platform/x"></script></html>')))
        self.assertFalse(is_browser_challenge(response(503, {"CF-RAY": "abc"}, "Unavailable")))


if __name__ == "__main__":
    unittest.main()
