"""Unit tests for EIP-1167 minimal proxy deployer and taker ban detection."""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from tempfile import TemporaryDirectory
import os

from src.deployers.deploy_proxy_executor import (
    build_proxy_initcode,
    OPERATOR_ADDRESS,
    MASTER_IMPLEMENTATION,
    update_env_file,
)
from src.engines.proxy_executor import is_taker_blocked


class TestProxyExecutor(unittest.TestCase):
    def test_build_proxy_initcode_structure(self):
        owner = "0x50dA32E628b45AbB1335924086Ca0013b9d4eC1C"
        impl = "0xD48Ab89581e77b103014DCC922279855a3b2a940"
        initcode = build_proxy_initcode(owner, impl)
        
        # Must be exactly 80 bytes (35 bytes init header + 45 bytes runtime code)
        self.assertEqual(len(initcode), 80)
        
        # PUSH20 owner in header
        owner_bytes = bytes.fromhex(owner.lower().replace("0x", ""))
        self.assertIn(owner_bytes, initcode[:35])
        
        # PUSH20 implementation in runtime code
        impl_bytes = bytes.fromhex(impl.lower().replace("0x", ""))
        self.assertIn(impl_bytes, initcode[35:])
        
        # Standard EIP-1167 prefix and suffix
        self.assertEqual(initcode[35:45], bytes.fromhex("363d3d373d3d3d363d73"))
        self.assertEqual(initcode[65:80], bytes.fromhex("5af43d82803e903d91602b57fd5bf3"))

    def test_update_env_file(self):
        with TemporaryDirectory() as tmpdir:
            test_env = Path(tmpdir) / ".env"
            test_env.write_text("ETH_ARB_STABLECOIN_EXECUTOR=0xOldAddress\nOTHER=1\n")
            with patch("src.deployers.deploy_proxy_executor.PROJECT_DIR", Path(tmpdir)):
                update_env_file("0xNewProxy123")
                content = test_env.read_text()
                self.assertIn("ETH_ARB_STABLECOIN_EXECUTOR=0xNewProxy123", content)
                self.assertNotIn("0xOldAddress", content)

    @patch("src.engines.proxy_executor.probe_taker_status")
    def test_is_taker_blocked_confirmed(self, mock_probe):
        # Target taker fails, clean taker succeeds -> Confirmed blocked
        def side_effect(taker):
            return taker == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
        mock_probe.side_effect = side_effect

        blocked = is_taker_blocked("0xTargetTaker", error_detail="MetaMatcha access denied (HTTP 403): Forbidden")
        self.assertTrue(blocked)

    @patch("src.engines.proxy_executor.probe_taker_status")
    def test_is_taker_blocked_false_on_cloudflare(self, mock_probe):
        # Both target and clean taker fail (meaning IP / Cloudflare issue, not taker ban)
        mock_probe.return_value = False

        blocked = is_taker_blocked("0xTargetTaker", error_detail="MetaMatcha access denied (HTTP 403): Forbidden")
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main()
