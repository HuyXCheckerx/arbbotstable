import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
for sub in ("core", "recovery", "engines", "web", "deployers"):
    subpath = str(SRC_DIR / sub)
    while subpath in sys.path:
        sys.path.remove(subpath)
    sys.path.insert(0, subpath)
if str(SRC_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SRC_DIR.parent))

import eth_flash_arb_pyusd_usdc as pyusd_arb
from src.engines import crosschain_sniper as sniper


class LocalEvmQuoteTests(unittest.TestCase):
    def test_local_quote_prices_from_the_local_server_without_calldata(self):
        http = Mock()
        http.get.return_value = {
            "in_atomic": "100000000000",
            "out_atomic": "99991702412",
            "leg_gas": 411119,
            "base_gas": 60000,
            "block": 26223265,
            "legs": [
                {"hop": 1, "label": "Uniswap v4 0.0005% PYUSD/USDS", "pool": "0xe63e32b2ae40601662", "share_bps": 10000},
                {"hop": 2, "label": "Uniswap v4 dyn hooked USDC/USDG", "pool": "0xeda62d2906d0edf26d", "share_bps": 7992},
                {"hop": 2, "label": "Maverick v2 USDC/USDG", "pool": "0xf49250c3b5ea73d29b", "share_bps": 2008},
            ],
        }
        client = pyusd_arb.MatchaClient(http, quote_provider="local")
        with patch.dict(os.environ, {"ETH_LOCAL_QUOTE_URL": "http://127.0.0.1:8795/"}):
            responses = client.quotes(
                "0x1111111111111111111111111111111111111111",
                100_000_000_000,
                5,
                ["0x"],
                sell_token_address=pyusd_arb.PYUSD,
                buy_token_address=pyusd_arb.USDG,
            )
        url = http.get.call_args.args[0]
        self.assertEqual(url, "http://127.0.0.1:8795/quote?input=PYUSD&output=USDG&amount=100000.000000")
        (name, quote), = responses
        self.assertEqual(name, pyusd_arb.LOCAL_AGGREGATOR)
        self.assertEqual(quote.buy_amount, 99_991_702_412)
        self.assertEqual(quote.sell_amount, 100_000_000_000)
        self.assertEqual(quote.gas, 471_119)
        self.assertEqual(quote.data, "0x")
        self.assertEqual(quote.target, pyusd_arb.ZERO_ADDRESS)
        self.assertEqual(
            quote.route,
            "Uniswap v4 0.0005% PYUSD/USDS [0xe63e32b2] 100% > Uniswap v4 dyn hooked USDC/USDG [0xeda62d29] 79.92% "
            "+ Maverick v2 USDC/USDG [0xf49250c3] 20.08% @ block 26223265",
        )
        # select_best accepts it, so the profit floor is checked on the local price.
        self.assertIs(pyusd_arb.select_best_matcha_quote(responses, 100_000_000_000), quote)

    def test_local_quote_rejects_errors_and_changed_inputs(self):
        http = Mock()
        client = pyusd_arb.MatchaClient(http, quote_provider="local")
        http.get.return_value = {"error": "direct pools cannot absorb 1000000 USDT"}
        with self.assertRaisesRegex(pyusd_arb.ArbError, "cannot absorb"):
            client.quotes("0x1", 1_000_000, 5, ["0x"], sell_token_address=pyusd_arb.USDT, buy_token_address=pyusd_arb.USDC)
        http.get.return_value = {"in_atomic": "999999", "out_atomic": "1000000"}
        with self.assertRaisesRegex(pyusd_arb.ArbError, "changed the requested input"):
            client.quotes("0x1", 1_000_000, 5, ["0x"], sell_token_address=pyusd_arb.USDT, buy_token_address=pyusd_arb.USDC)
        http.get.side_effect = pyusd_arb.ArbError("http://127.0.0.1:8795/quote returned HTTP 422: cannot absorb")
        with self.assertRaisesRegex(pyusd_arb.ArbError, "^No routes found by the local quoter") as caught:
            client.quotes("0x1", 1_000_000, 5, ["0x"], sell_token_address=pyusd_arb.USDT, buy_token_address=pyusd_arb.USDC)
        self.assertEqual(sniper.failure_category(str(caught.exception)), "no-route")
        http.get.side_effect = pyusd_arb.RetryableArbError(
            'http://127.0.0.1:8795/quote returned HTTP 503: {"error":"mirrored state is not current"}'
        )
        with self.assertRaisesRegex(pyusd_arb.RetryableArbError, "^Local quoter state is not current") as stale:
            client.quotes("0x1", 1_000_000, 5, ["0x"], sell_token_address=pyusd_arb.USDT, buy_token_address=pyusd_arb.USDC)
        self.assertEqual(sniper.failure_category(str(stale.exception)), "transient-local")
        route = sniper.Route("ethereum", "PYUSD/USDG")
        self.assertEqual(
            sniper.readable_failure(route, str(stale.exception), "transient-local"),
            "Ethereum local quoter is temporarily unavailable (HTTP 503); its mirrored state is not current",
        )
        self.assertEqual(sniper.failure_category("Local quote returned HTTP 503: stale"), "transient-local")
        http.get.side_effect = None
        with self.assertRaisesRegex(pyusd_arb.ArbError, "no symbol"):
            client.quotes("0x1", 1_000_000, 5, ["0x"], sell_token_address="0x" + "ab" * 20, buy_token_address=pyusd_arb.USDC)

    def test_parser_accepts_local(self):
        args = pyusd_arb.parser().parse_args(["--quote-provider", "local"])
        self.assertEqual(args.quote_provider, "local")
        self.assertEqual(pyusd_arb.QUOTE_PROVIDER_LABELS["local"], "Local")


class SniperLocalProviderTests(unittest.TestCase):
    def test_routes_label_and_invoke_the_local_providers(self):
        with patch.dict(os.environ, {"SOL_FLASH_ARB_DEX_PROVIDER": "local", "ETH_ARB_QUOTE_PROVIDER": "local"}):
            solana = sniper.Route("solana", "PYUSD/USDG")
            ethereum = sniper.Route("ethereum", "PYUSD/USDG")
            self.assertEqual(solana.dex_name, "Local")
            self.assertEqual(ethereum.dex_name, "Local")
            self.assertEqual(sniper.dex_provider_key(solana), "local:solana")
            sol_invocation = sniper.build_route_invocation(solana, live=False, execution_floor=Decimal("0.01"))
            eth_invocation = sniper.build_route_invocation(ethereum, live=False, execution_floor=Decimal("0.01"))
        self.assertIn("--dex-provider", sol_invocation.command)
        self.assertEqual(sol_invocation.command[sol_invocation.command.index("--dex-provider") + 1], "local")
        self.assertNotIn("--send", sol_invocation.command)
        self.assertEqual(sol_invocation.environment["SOL_FLASH_ARB_ONLY_DIRECT_ROUTES"], "false")
        self.assertEqual(eth_invocation.command[eth_invocation.command.index("--quote-provider") + 1], "local")
        self.assertNotIn("--send", eth_invocation.command)


if __name__ == "__main__":
    unittest.main()
