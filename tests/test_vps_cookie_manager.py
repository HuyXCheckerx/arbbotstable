"""Unit tests for VPS cookie detection and automated clearance initialization."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.engines import matcha_cookie_manager as cm


class VpsCookieManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_cache = Path(self.temp_dir.name) / ".matcha_cookies.json"
        self.test_lock = Path(self.temp_dir.name) / ".matcha_cookies.lock"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_vps_environment_variables(self):
        with patch.dict("os.environ", {"IS_VPS": "true"}, clear=False):
            self.assertTrue(cm.is_vps())
        with patch.dict("os.environ", {"RUNNING_ON_VM": "1"}, clear=False):
            self.assertTrue(cm.is_vps())
        with patch.dict("os.environ", {"IS_VPS": "false", "RUNNING_ON_VM": "", "USER": "tester"}, clear=False):
            with patch("sys.platform", "darwin"):
                self.assertFalse(cm.is_vps())

    def test_is_vps_usernames(self):
        for user in ("Administrator", "root", "ubuntu", "ec2-user"):
            with patch.dict("os.environ", {"USER": user, "IS_VPS": ""}, clear=False):
                self.assertTrue(cm.is_vps())

    def test_is_cookie_file_missing_or_empty(self):
        # Non-existent
        self.assertTrue(cm.is_cookie_file_missing_or_empty(self.test_cache))

        # 0 bytes
        self.test_cache.write_text("", encoding="utf-8")
        self.assertTrue(cm.is_cookie_file_missing_or_empty(self.test_cache))

        # Empty cookies list
        self.test_cache.write_text(json.dumps({"timestamp": 123, "cookies": []}), encoding="utf-8")
        self.assertTrue(cm.is_cookie_file_missing_or_empty(self.test_cache))

        # Missing clearance token
        self.test_cache.write_text(json.dumps({"timestamp": 123, "cookies": [{"name": "foo", "value": "bar"}]}), encoding="utf-8")
        self.assertTrue(cm.is_cookie_file_missing_or_empty(self.test_cache))

        # Valid clearance token
        self.test_cache.write_text(
            json.dumps({"timestamp": 123, "cookies": [{"name": "_vcrcs", "value": "token_val"}]}),
            encoding="utf-8",
        )
        self.assertFalse(cm.is_cookie_file_missing_or_empty(self.test_cache))

    def test_ensure_vps_cookies_triggers_solve_when_on_vps_and_missing(self):
        mock_cookies = [{"name": "_vcrcs", "value": "val123", "domain": "meta.matcha.xyz"}]
        with patch.object(cm, "CACHE_FILE", self.test_cache), \
             patch.object(cm, "LOCK_FILE", self.test_lock), \
             patch.object(cm, "is_vps", return_value=True), \
             patch.object(cm, "_solve_challenge", return_value=mock_cookies) as mock_solve:
            
            cookies = cm.ensure_vps_cookies()
            self.assertEqual(len(cookies), 1)
            self.assertEqual(cookies[0]["name"], "_vcrcs")
            mock_solve.assert_called_once()
            self.assertTrue(self.test_cache.exists())

    def test_ensure_vps_cookies_skips_solve_when_not_on_vps(self):
        with patch.object(cm, "CACHE_FILE", self.test_cache), \
             patch.object(cm, "is_vps", return_value=False), \
             patch.object(cm, "_solve_challenge") as mock_solve:
            
            cookies = cm.ensure_vps_cookies(force=False)
            self.assertEqual(cookies, [])
            mock_solve.assert_not_called()

    def test_ensure_vps_cookies_skips_solve_when_cookies_already_valid(self):
        existing = [{"name": "_vcrcs", "value": "existing_val", "domain": "meta.matcha.xyz"}]
        self.test_cache.write_text(
            json.dumps({"timestamp": 9999999999.0, "cookies": existing}),
            encoding="utf-8",
        )
        with patch.object(cm, "CACHE_FILE", self.test_cache), \
             patch.object(cm, "is_vps", return_value=True), \
             patch.object(cm, "_solve_challenge") as mock_solve:
            
            cookies = cm.ensure_vps_cookies(force=False)
            self.assertEqual(len(cookies), 1)
            self.assertEqual(cookies[0]["value"], "existing_val")
            mock_solve.assert_not_called()

    def test_missing_vps_cookies_do_not_block_quote_workers(self):
        with patch.object(cm, "CACHE_FILE", self.test_cache), \
             patch.object(cm, "is_vps", return_value=True), \
             patch.object(cm, "trigger_background_solve") as background, \
             patch.object(cm, "_solve_challenge") as solve:
            self.assertEqual(cm.get_valid_cookies(non_blocking=True), [])
            background.assert_called_once()
            solve.assert_not_called()

    def test_explicit_blocking_cookie_request_still_waits(self):
        cookies = [{"name": "_vcrcs", "value": "test", "domain": "meta.matcha.xyz"}]
        with patch.object(cm, "CACHE_FILE", self.test_cache), \
             patch.object(cm, "LOCK_FILE", self.test_lock), \
             patch.object(cm, "_solve_challenge", return_value=cookies) as solve, \
             patch.object(cm, "trigger_background_solve") as background:
            self.assertEqual(cm.get_valid_cookies(non_blocking=False), cookies)
            solve.assert_called_once()
            background.assert_not_called()


if __name__ == "__main__":
    unittest.main()
