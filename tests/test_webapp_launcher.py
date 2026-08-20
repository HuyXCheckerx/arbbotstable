import os
import unittest
from pathlib import Path
from unittest.mock import patch

import webapp


class WebappLauncherTests(unittest.TestCase):
    def test_browser_url_maps_wildcard_hosts_to_loopback(self):
        self.assertEqual(webapp.browser_url("0.0.0.0", 25284), "http://127.0.0.1:25284")
        self.assertEqual(webapp.browser_url("::", 25284), "http://127.0.0.1:25284")

    def test_launcher_defaults_to_safe_local_access(self):
        with patch.dict(os.environ, {}, clear=True):
            args = webapp.parser().parse_args([])

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 25284)
        self.assertFalse(args.no_open)

    def test_windows_one_click_launcher_targets_root_webapp(self):
        launcher = Path(webapp.__file__).with_name("start_webapp.cmd")

        self.assertTrue(launcher.is_file())
        self.assertIn("webapp.py", launcher.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
