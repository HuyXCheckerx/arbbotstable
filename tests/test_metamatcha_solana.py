from __future__ import annotations

import base64
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


from src.engines import metamatcha_solana as metamatcha


def response(buy_amount: int, *, taker: str = "wallet", success: bool = True):
    return {
        "direct": {
            "quote": {
                "sellAmount": "100",
                "buyAmount": str(buy_amount),
                "taker": taker,
                "transaction": base64.b64encode(b"solana-transaction").decode(),
            },
            "simulation": {"result": "success" if success else "failed"},
        }
    }


class MetaMatchaServeTests(unittest.TestCase):
    def serve(self, lines):
        import io
        import json
        import os

        stdout = io.StringIO()
        saved_environment = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved_environment)))
        with patch.object(metamatcha.sys, "stdin", io.StringIO("".join(line + "\n" for line in lines))), \
                patch.object(metamatcha.sys, "stdout", stdout):
            self.assertEqual(metamatcha.serve(), 0)
        prefix = metamatcha.SERVE_RESPONSE_PREFIX
        return [json.loads(line[len(prefix):]) for line in stdout.getvalue().splitlines()
                if line.startswith(prefix)], stdout.getvalue()

    def test_answers_each_request_with_its_environment_and_a_fresh_session(self):
        import json
        import os

        seen = []

        def fake_fetch(request):
            seen.append((request["amount"], os.environ.get("MATCHA_PROXY"), metamatcha._SHARED_SESSION))
            metamatcha._SHARED_SESSION = object()
            print("dependency chatter")
            if request["amount"] == "13":
                raise RuntimeError("HTTP 403: access denied")
            return {"provider": "MetaMatcha", "inAmount": request["amount"]}

        with patch.object(metamatcha, "fetch_quote", side_effect=fake_fetch):
            responses, raw = self.serve([
                json.dumps({"id": 1, "env": {"MATCHA_PROXY": "http://a"}, "request": {"amount": "5"}}),
                "",
                json.dumps({"id": 2, "env": {"MATCHA_PROXY": "http://b"}, "request": {"amount": "13"}}),
                json.dumps({"id": 3, "request": "not an object"}),
            ])

        self.assertEqual(seen, [("5", "http://a", None), ("13", "http://b", None)])
        self.assertEqual(responses, [
            {"id": 1, "ok": True, "result": {"provider": "MetaMatcha", "inAmount": "5"}},
            {"id": 2, "ok": False, "error": "MetaMatcha quote failed: HTTP 403: access denied"},
            {"id": 3, "ok": False,
             "error": "MetaMatcha quote failed: request must be a JSON object"},
        ])
        self.assertNotIn("dependency chatter", raw)

    def test_serving_helper_leaves_cookie_solving_to_the_sniper(self):
        from src.engines import matcha_cookie_manager

        original = matcha_cookie_manager.trigger_background_solve
        self.addCleanup(setattr, matcha_cookie_manager, "trigger_background_solve", original)
        with patch.object(matcha_cookie_manager, "start_background_cookie_solver") as start:
            self.serve([])
            matcha_cookie_manager.trigger_background_solve()
        start.assert_not_called()


class MetaMatchaSolanaTests(unittest.TestCase):
    def test_json_access_denials_keep_status_and_provider_context(self):
        for status in (401, 403):
            with self.subTest(status=status):
                session = Mock()
                session.post.return_value = SimpleNamespace(
                    status_code=status,
                    text='{"error":{"code":"403","message":"Forbidden"}}',
                )
                with patch.object(metamatcha, "_session", return_value=session):
                    with self.assertRaisesRegex(
                        metamatcha.ProviderAccessBlockedError,
                        rf"MetaMatcha .* access denied \(HTTP {status}\)",
                    ):
                        metamatcha._post_json(
                            "https://meta.matcha.xyz/api/competitions", {}, 2
                        )
                session.post.assert_called_once()

    def test_vercel_429_stops_before_quote_fanout_and_preserves_diagnosis(self):
        session = Mock()
        session.post.return_value = SimpleNamespace(
            status_code=429,
            text="<html><title>Vercel Security Checkpoint</title></html>",
            headers={"x-vercel-mitigated": "challenge", "x-vercel-id": "hkg1::test"},
        )
        with patch.object(metamatcha, "_session", return_value=session):
            with self.assertRaisesRegex(
                metamatcha.ProviderAccessBlockedError,
                r"/api/competitions access blocked by Vercel Security Checkpoint \(HTTP 429",
            ) as caught:
                metamatcha.fetch_quote(self.quote_request())
        session.post.assert_called_once()
        self.assertIn("request-id=hkg1::test", str(caught.exception))
        self.assertNotIn("<html>", str(caught.exception))

    def test_plain_rate_limit_preserves_longest_competitor_wait(self):
        def post(url, payload, timeout):
            if url.endswith("/api/competitions"):
                return {"id": "test-competition"}
            wait = "60" if payload["aggregator"] == "0x" else "900"
            raise metamatcha.ProviderRateLimitedError(
                SimpleNamespace(status_code=429, text="Too many requests", headers={"Retry-After": wait}),
                url,
            )

        with patch.object(metamatcha, "_post_json", side_effect=post):
            with self.assertRaises(metamatcha.ProviderRateLimitedError) as caught:
                metamatcha.fetch_quote(self.quote_request())
        self.assertEqual(caught.exception.retry_after_seconds, 900)
        self.assertIn("retry-after=900s", str(caught.exception))

    def test_competition_denial_does_not_request_competitor_quotes(self):
        denied = metamatcha.ProviderAccessBlockedError("MetaMatcha access denied (HTTP 403)")
        with patch.object(metamatcha, "_post_json", side_effect=denied) as post:
            with self.assertRaises(metamatcha.ProviderAccessBlockedError):
                metamatcha.fetch_quote(self.quote_request())
        post.assert_called_once()

    @staticmethod
    def quote_request():
        return {
            "inputMint": "input", "outputMint": "output", "taker": "wallet",
            "amount": "100", "aggregators": ["0x", "OKX"],
        }

    def test_quote_denial_is_preserved_when_no_executable_quote_exists(self):
        def post(url, payload, timeout):
            if url.endswith("/api/competitions"):
                return {"id": "test-competition"}
            if payload["aggregator"] == "0x":
                raise metamatcha.ProviderAccessBlockedError("MetaMatcha access denied (HTTP 403)")
            return response(101, success=False)

        with patch.object(metamatcha, "_post_json", side_effect=post):
            with self.assertRaises(metamatcha.ProviderAccessBlockedError):
                metamatcha.fetch_quote(self.quote_request())

    def test_access_denial_does_not_discard_an_executable_competitor(self):
        def post(url, payload, timeout):
            if url.endswith("/api/competitions"):
                return {"id": "test-competition"}
            if payload["aggregator"] == "0x":
                raise metamatcha.ProviderAccessBlockedError("MetaMatcha access denied (HTTP 403)")
            return response(101)

        with patch.object(metamatcha, "_post_json", side_effect=post):
            quote = metamatcha.fetch_quote(self.quote_request())
        self.assertEqual(quote["aggregator"], "OKX")
        self.assertEqual(quote["outAmount"], "101")

    def test_selects_highest_successfully_simulated_executable_quote(self):
        aggregator, quote, _ = metamatcha.select_best_quote(
            {
                "0x": response(99),
                "OKX": response(101),
                "failed": response(1_000, success=False),
            },
            sell_amount=100,
            taker="wallet",
        )

        self.assertEqual(aggregator, "OKX")
        self.assertEqual(quote["buyAmount"], "101")

    def test_rejects_a_quote_for_a_different_wallet(self):
        with self.assertRaisesRegex(RuntimeError, "taker changed"):
            metamatcha.select_best_quote(
                {"0x": response(101, taker="other-wallet")},
                sell_amount=100,
                taker="wallet",
            )

    def test_fetch_quote_returns_all_candidates_sorted_by_buy_amount(self):
        def post(url, payload, timeout):
            if url.endswith("/api/competitions"):
                return {"id": "test-competition"}
            if payload["aggregator"] == "0x":
                return response(105)
            if payload["aggregator"] == "OKX":
                return response(110)
            return response(100)

        with patch.object(metamatcha, "_post_json", side_effect=post):
            quote = metamatcha.fetch_quote(self.quote_request())

        self.assertEqual(quote["aggregator"], "OKX")
        self.assertEqual(quote["outAmount"], "110")
        self.assertIn("candidates", quote)
        self.assertEqual(len(quote["candidates"]), 2)
        self.assertEqual(quote["candidates"][0]["aggregator"], "OKX")
        self.assertEqual(quote["candidates"][0]["outAmount"], "110")
        self.assertEqual(quote["candidates"][1]["aggregator"], "0x")
        self.assertEqual(quote["candidates"][1]["outAmount"], "105")
        import json
        serialized = json.dumps(quote)
        self.assertIn('"candidates":', serialized)


if __name__ == "__main__":
    unittest.main()

