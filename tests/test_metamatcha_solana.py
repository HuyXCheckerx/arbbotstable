from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import unittest


HELPER_PATH = Path(__file__).parents[1] / "src" / "engines" / "metamatcha_solana.py"
SPEC = importlib.util.spec_from_file_location("metamatcha_solana", HELPER_PATH)
assert SPEC and SPEC.loader
metamatcha = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metamatcha)


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


class MetaMatchaSolanaTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
