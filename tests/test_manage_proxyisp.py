"""Unit tests for ProxyISP management, automated residential proxy renewal, and sniper startup."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.manage_proxyisp import (
    VN_RESIDENTIAL_DAILY_PRICE_VND,
    buy_residential_proxy,
    disable_proxy_autorenew,
    ensure_active_proxy,
    format_proxy_url,
    format_remaining_time,
    get_proxies_status,
    is_proxy_working,
    parse_expiry,
    setup_sniper_proxy,
    update_env_proxy,
    wait_for_new_proxy,
)


class TestManageProxyISP(unittest.TestCase):
    def test_parse_expiry(self):
        dt = parse_expiry("2026-10-22T17:56:42.739000")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 22)
        self.assertEqual(dt.tzinfo, timezone.utc)

        # UTC format with Z
        dt_z = parse_expiry("2026-10-22T17:56:42Z")
        self.assertIsNotNone(dt_z)
        self.assertEqual(dt_z.tzinfo, timezone.utc)

        # Invalid string
        self.assertIsNone(parse_expiry("invalid-date"))
        self.assertIsNone(parse_expiry(None))

    def test_format_remaining_time(self):
        self.assertEqual(format_remaining_time(-10), "EXPIRED")
        self.assertEqual(format_remaining_time(0), "EXPIRED")
        self.assertEqual(format_remaining_time(120), "2m")
        self.assertEqual(format_remaining_time(3700), "1h 1m")
        self.assertEqual(format_remaining_time(90000), "1d 1h 0m")

    def test_format_proxy_url(self):
        p1 = {"username": "user", "password": "pass", "host": "1.2.3.4", "port": 8080}
        self.assertEqual(format_proxy_url(p1), "http://user:pass@1.2.3.4:8080")

        p2 = {"host": "1.2.3.4", "port": 8080}
        self.assertEqual(format_proxy_url(p2), "http://1.2.3.4:8080")

        p3 = {}
        self.assertEqual(format_proxy_url(p3), "")

    @patch("scripts.manage_proxyisp.get_balance")
    @patch("scripts.manage_proxyisp.get_proxies")
    def test_get_proxies_status_all_expired(self, mock_proxies, mock_balance):
        mock_balance.return_value = 10000.0
        mock_proxies.return_value = [
            {
                "id": "p1",
                "name": "Proxy 1",
                "host": "1.1.1.1",
                "port": 8000,
                "expiresAt": "2026-09-01T00:00:00",
                "status": "active",
            },
            {
                "id": "p2",
                "name": "Proxy 2",
                "host": "2.2.2.2",
                "port": 8000,
                "expiresAt": "2026-09-10T00:00:00",
                "status": "expired",
            },
        ]

        now = datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc)
        status = get_proxies_status("fake_key", now=now)

        self.assertEqual(status["total_proxies"], 2)
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["expired_count"], 2)
        self.assertTrue(status["all_expired"])

    @patch("scripts.manage_proxyisp.get_balance")
    @patch("scripts.manage_proxyisp.get_proxies")
    def test_get_proxies_status_some_active(self, mock_proxies, mock_balance):
        mock_balance.return_value = 10000.0
        mock_proxies.return_value = [
            {
                "id": "p1",
                "name": "Proxy 1",
                "host": "1.1.1.1",
                "port": 8000,
                "expiresAt": "2026-09-01T00:00:00",
                "status": "expired",
            },
            {
                "id": "p2",
                "name": "Proxy 2",
                "host": "2.2.2.2",
                "port": 8000,
                "expiresAt": "2026-10-01T00:00:00",
                "status": "active",
            },
        ]

        now = datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc)
        status = get_proxies_status("fake_key", now=now)

        self.assertEqual(status["total_proxies"], 2)
        self.assertEqual(status["active_count"], 1)
        self.assertEqual(status["expired_count"], 1)
        self.assertFalse(status["all_expired"])
        self.assertGreater(status["time_until_all_expire_seconds"], 0)

    @patch("scripts.manage_proxyisp.get_proxies")
    def test_wait_for_new_proxy(self, mock_proxies):
        old_proxy = {"id": "old1", "host": "1.1.1.1", "port": 8000}
        new_proxy = {"id": "new2", "host": "2.2.2.2", "port": 9000, "username": "u", "password": "p"}

        mock_proxies.side_effect = [
            [old_proxy],
            [old_proxy, new_proxy],
        ]

        result = wait_for_new_proxy("fake_key", known_proxy_ids={"old1"}, timeout=5.0, poll_interval=0.01)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("id"), "new2")

    def test_update_env_proxy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "ETH_RPC_URL=https://rpc.example.com\n"
                "MATCHA_PROXY=http://old:pass@1.1.1.1:8000\n"
                "OTHER_VAR=123\n",
                encoding="utf-8",
            )

            success = update_env_proxy("http://new:pass@2.2.2.2:9000", env_path=env_file)
            self.assertTrue(success)

            content = env_file.read_text(encoding="utf-8")
            self.assertIn("MATCHA_PROXY=http://new:pass@2.2.2.2:9000", content)
            self.assertNotIn("1.1.1.1:8000", content)
            self.assertIn("OTHER_VAR=123", content)

    @patch("scripts.manage_proxyisp.get_proxies_status")
    def test_ensure_active_proxy_no_buy_when_active(self, mock_status):
        mock_status.return_value = {
            "balance_vnd": 10000.0,
            "total_proxies": 2,
            "active_count": 2,
            "expired_count": 0,
            "all_expired": False,
            "time_until_all_expire_str": "10d",
            "proxies": [],
        }

        res = ensure_active_proxy("fake_key", force_buy=False)
        self.assertEqual(res["action"], "active_proxies_remain")
        self.assertIn("No purchase required", res["message"])

    @patch("scripts.manage_proxyisp.test_proxy")
    @patch("scripts.manage_proxyisp.update_env_proxy")
    @patch("scripts.manage_proxyisp.wait_for_new_proxy")
    @patch("scripts.manage_proxyisp.buy_residential_proxy")
    @patch("scripts.manage_proxyisp.get_proxies_status")
    def test_ensure_active_proxy_buys_when_all_expired(
        self, mock_status, mock_buy, mock_wait, mock_update, mock_test
    ):
        mock_status.return_value = {
            "balance_vnd": 5000.0,
            "total_proxies": 1,
            "active_count": 0,
            "expired_count": 1,
            "all_expired": True,
            "time_until_all_expire_str": "EXPIRED",
            "proxies": [{"id": "p_old"}],
        }
        mock_buy.return_value = {"order_number": "ORD-123", "status": "completed"}
        mock_wait.return_value = {
            "id": "p_new",
            "name": "New Proxy",
            "host": "9.9.9.9",
            "port": 8888,
            "username": "usr",
            "password": "pwd",
            "expiresAt": "2026-09-25T00:00:00",
        }
        mock_update.return_value = True
        mock_test.return_value = {"ip_ok": True, "ip": "9.9.9.9", "gas_ok": True, "comp_ok": True, "detail": ""}

        res = ensure_active_proxy("fake_key", force_buy=False, days=1)

        self.assertEqual(res["action"], "purchased_and_configured")
        self.assertEqual(res["new_proxy_url"], "http://usr:pwd@9.9.9.9:8888")
        mock_buy.assert_called_once_with("fake_key", days=1, auto_renew=False)
        mock_wait.assert_called_once()
        mock_update.assert_called_once()

    @patch("scripts.manage_proxyisp.update_env_proxy")
    @patch("scripts.manage_proxyisp.is_proxy_working")
    @patch("scripts.manage_proxyisp.get_proxies")
    @patch("scripts.manage_proxyisp.buy_residential_proxy")
    def test_setup_sniper_proxy_selects_working_proxy(
        self, mock_buy, mock_get_proxies, mock_is_working, mock_update_env
    ):
        mock_get_proxies.return_value = [
            {"id": "p1", "name": "Proxy 1", "host": "1.1.1.1", "port": 8000, "username": "u1", "password": "pw1"},
            {"id": "p2", "name": "Proxy 2", "host": "2.2.2.2", "port": 8000, "username": "u2", "password": "pw2"},
        ]
        # First proxy fails, second proxy works
        mock_is_working.side_effect = [False, True]
        mock_update_env.return_value = True

        selected = setup_sniper_proxy("fake_key")

        self.assertEqual(selected, "http://u2:pw2@2.2.2.2:8000")
        mock_buy.assert_not_called()
        mock_update_env.assert_called_once_with("http://u2:pw2@2.2.2.2:8000", env_path=unittest.mock.ANY)

    @patch("scripts.manage_proxyisp.disable_proxy_autorenew")
    @patch("scripts.manage_proxyisp.update_env_proxy")
    @patch("scripts.manage_proxyisp.wait_for_new_proxy")
    @patch("scripts.manage_proxyisp.buy_residential_proxy")
    @patch("scripts.manage_proxyisp.get_balance")
    @patch("scripts.manage_proxyisp.is_proxy_working")
    @patch("scripts.manage_proxyisp.get_proxies")
    def test_setup_sniper_proxy_buys_when_none_work(
        self,
        mock_get_proxies,
        mock_is_working,
        mock_balance,
        mock_buy,
        mock_wait,
        mock_update_env,
        mock_disable_autorenew,
    ):
        mock_get_proxies.return_value = [
            {"id": "p1", "name": "Proxy 1", "host": "1.1.1.1", "port": 8000, "username": "u1", "password": "pw1"},
        ]
        mock_is_working.return_value = False
        mock_balance.return_value = 10000.0
        mock_buy.return_value = {"order_number": "ORD-NEW-1", "status": "completed"}
        mock_wait.return_value = {
            "id": "p_brand_new",
            "name": "New Residential Proxy",
            "host": "8.8.8.8",
            "port": 9999,
            "username": "fresh_u",
            "password": "fresh_p",
            "expiresAt": "2026-09-25T10:00:00",
            "auto_renew": False,
        }
        mock_update_env.return_value = True

        selected = setup_sniper_proxy("fake_key")

        self.assertEqual(selected, "http://fresh_u:fresh_p@8.8.8.8:9999")
        # Verify bought with auto_renew=False
        mock_buy.assert_called_once_with("fake_key", days=1, auto_renew=False)
        mock_wait.assert_called_once()
        mock_disable_autorenew.assert_called_once_with("fake_key", "p_brand_new")
        mock_update_env.assert_called_once_with("http://fresh_u:fresh_p@8.8.8.8:9999", env_path=unittest.mock.ANY)

    @patch("scripts.manage_proxyisp.api_request")
    def test_disable_proxy_autorenew(self, mock_api_request):
        mock_api_request.return_value = {"success": True}
        result = disable_proxy_autorenew("fake_key", "proxy_123")
        self.assertTrue(result)
        mock_api_request.assert_called()


if __name__ == "__main__":
    unittest.main()
