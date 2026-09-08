from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.engines.provider_http import (
    access_block_detail,
    rate_limit_detail,
    retry_after_seconds,
    safe_endpoint,
)


ENDPOINT = "https://meta.matcha.xyz/api/competitions"


def response(status=429, headers=None, body=""):
    return SimpleNamespace(status_code=status, headers=headers or {}, text=body)


class ProviderHttpTests(unittest.TestCase):
    def test_vercel_challenge_and_deny_override_status_classification(self):
        for status in (403, 429, 503):
            for mitigation in ("challenge", "DENY"):
                with self.subTest(status=status, mitigation=mitigation):
                    detail = access_block_detail(response(status, {
                        "X-Vercel-Mitigated": mitigation, "X-Vercel-Id": "sin1::abc-123",
                    }), ENDPOINT)
                    name = "Vercel firewall" if mitigation == "DENY" else "Vercel Security Checkpoint"
                    self.assertIn(f"access blocked by {name}", detail)
                    self.assertIn(f"HTTP {status}; x-vercel-mitigated={mitigation.lower()}", detail)
                    self.assertIn("request-id=sin1::abc-123", detail)

    def test_html_checkpoint_fallback_without_headers(self):
        for status in (403, 429, 503):
            detail = access_block_detail(response(
                status, body="<!doctype html><title>Vercel Security Checkpoint</title>",
            ), ENDPOINT)
            self.assertIn("access blocked by Vercel Security Checkpoint", detail)
            self.assertNotIn("<title>", detail)

    def test_success_and_plain_json_mentions_do_not_imply_challenge(self):
        self.assertIsNone(access_block_detail(response(200, {
            "x-vercel-mitigated": "challenge",
        }), ENDPOINT))
        self.assertIsNone(access_block_detail(response(
            429, body='{"message":"Vercel Security Checkpoint"}',
        ), ENDPOINT))

    def test_cloudflare_markers_and_generic_access_denials(self):
        for status in (403, 503):
            detail = access_block_detail(response(status, {"CF-RAY": "abc-SIN"}), ENDPOINT)
            self.assertIn(f"access blocked by Cloudflare (HTTP {status})", detail)
        for status in (401, 403):
            self.assertEqual(
                access_block_detail(response(status, body='{"error":"Forbidden"}'), ENDPOINT),
                f"{ENDPOINT} access denied (HTTP {status})",
            )
        for status in (429, 502, 503, 504):
            self.assertIsNone(access_block_detail(response(status), ENDPOINT))

    def test_genuine_rate_limit_preserves_retry_after_without_body(self):
        result = response(429, {"Retry-After": " 90 ", "x-request-id": "req-123"}, "secret body")
        self.assertIsNone(access_block_detail(result, ENDPOINT))
        self.assertEqual(rate_limit_detail(result, ENDPOINT), (
            f"{ENDPOINT} rate limited (HTTP 429); retry-after=90s; request-id=req-123"
        ))

    def test_retry_after_http_date_uses_server_clock(self):
        result = response(headers={
            "DATE": "Tue, 08 Sep 2026 03:15:00 GMT",
            "Retry-After": "Tue, 08 Sep 2026 03:17:05 GMT",
        })
        self.assertEqual(retry_after_seconds(result), 125)
        self.assertIn("retry-after=125s", rate_limit_detail(result, ENDPOINT))

    def test_retry_after_http_date_uses_local_clock_without_valid_server_date(self):
        result = response(headers={"retry-after": "Tue, 08 Sep 2026 03:17:05 GMT", "date": "invalid"})
        with patch("src.engines.provider_http.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 8, 3, 15, 0, 500000, tzinfo=timezone.utc)
            self.assertEqual(retry_after_seconds(result), 125)

    def test_invalid_retry_after_is_not_logged_and_past_dates_are_zero(self):
        for value in ("", "-1", "-1.5", "2.5", "NaN", "inf", "1e999", "9" * 400, "token=private"):
            with self.subTest(value=value):
                result = response(headers={"retry-after": value})
                self.assertIsNone(retry_after_seconds(result))
                self.assertEqual(rate_limit_detail(result, ENDPOINT), f"{ENDPOINT} rate limited (HTTP 429)")
        self.assertEqual(retry_after_seconds(response(headers={
            "date": "Tue, 08 Sep 2026 03:15:00 GMT",
            "retry-after": "Tue, 08 Sep 2026 03:14:00 GMT",
        })), 0)

    def test_url_credentials_and_challenge_secrets_are_redacted(self):
        url = "https://user:password@meta.matcha.xyz/api/competitions?api_key=keysecret#fragmentsecret"
        result = response(headers={
            "x-vercel-mitigated": "challenge",
            "x-vercel-challenge-token": "challengesecret",
            "set-cookie": "cookiesecret",
            "x-vercel-id": "id\nAuthorization: secret",
        }, body="<html>bodysecret</html>")
        detail = access_block_detail(result, url)
        self.assertEqual(safe_endpoint(url), ENDPOINT)
        self.assertNotIn("secret", detail)
        self.assertNotIn("password", detail)
        self.assertNotIn("<html>", detail)
        self.assertNotIn("request-id", detail)


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_metamatcha.py"
        spec = importlib.util.spec_from_file_location("diagnose_metamatcha", path)
        cls.diagnostic = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.diagnostic)

    def test_report_omits_cookie_challenge_token_and_untrusted_body(self):
        result = response(headers={
            "x-vercel-mitigated": "challenge", "x-vercel-id": "sin1::abc",
            "set-cookie": "private-cookie", "x-vercel-challenge-token": "private-token",
        }, body="<title>Vercel Security Checkpoint</title>private-body")
        record = self.diagnostic.response_record(result, ENDPOINT, "POST")
        self.assertEqual(record["title"], "Vercel Security Checkpoint")
        self.assertFalse(record["retry_after_present"])
        self.assertIsNone(record["retry_after_seconds"])
        self.assertNotIn("private", str(record))

    def test_diagnostic_stops_after_gas_denial_and_never_posts(self):
        session = Mock()
        session.get.return_value = response(headers={"x-vercel-mitigated": "challenge"})
        with patch("curl_cffi.requests.Session", return_value=session):
            report, status = self.diagnostic.diagnose("ethereum", 5)
        self.assertEqual(status, 1)
        self.assertEqual(len(report["requests"]), 1)
        session.post.assert_not_called()
        session.close.assert_called_once()

    def test_diagnostic_only_posts_one_competition_per_chain(self):
        for chain in ("ethereum", "solana"):
            with self.subTest(chain=chain):
                session = Mock()
                gas = response(200)
                gas.json = lambda: {"price": "1000000000"}
                session.get.return_value = gas
                session.post.return_value = response(headers={"x-vercel-mitigated": "challenge"})
                with patch("curl_cffi.requests.Session", return_value=session):
                    report, status = self.diagnostic.diagnose(chain, 5)
                self.assertEqual(status, 1)
                self.assertEqual(len(report["requests"]), 2)
                session.get.assert_called_once()
                session.post.assert_called_once()
                self.assertEqual(session.post.call_args.args[0], ENDPOINT)
                self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
                self.assertIn("access blocked by Vercel", report["requests"][1]["diagnostic"])


if __name__ == "__main__":
    unittest.main()
